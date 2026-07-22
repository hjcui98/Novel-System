from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine

from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.domain.evaluation import BenchmarkRunConfig, EvaluationParameter
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, StableId
from novel_agent.domain.model_calls import ModelRole
from novel_agent.domain.runtime import (
    EvaluationDecision,
    EvaluationEntry,
    EvaluationMetric,
)
from novel_agent.services.evaluation import (
    EvaluationConfigError,
    EvaluationConflictError,
    EvaluationHarness,
    EvaluationLedgerRepository,
    config_fingerprint,
)


@pytest.fixture
def ledger() -> Iterator[EvaluationLedgerRepository]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield EvaluationLedgerRepository(build_session_factory(engine))
    engine.dispose()


def config(
    *,
    seed: int = 7,
    parameters: tuple[EvaluationParameter, ...] = (),
    model_required: bool = False,
    model_role: ModelRole | None = None,
) -> BenchmarkRunConfig:
    return BenchmarkRunConfig(
        config_id=StableId("evaluation.config.1"),
        benchmark_id="synthetic-stage0",
        dataset_hash=ArtifactId("sha256:" + "a" * 64),
        run_id=RunId("run.evaluation"),
        code_version="test-revision",
        random_seed=seed,
        parameters=parameters,
        model_required=model_required,
        model_role=model_role,
    )


def entry(
    identity: str,
    *,
    with_model: bool = False,
    run_id: RunId | None = None,
) -> EvaluationEntry:
    return EvaluationEntry(
        evaluation_id=StableId(identity),
        run_id=run_id or RunId("run.evaluation"),
        candidate_id=StableId(f"candidate.{identity}"),
        commit_id=CommitId("sha256:" + "b" * 64),
        evaluator="deterministic-evaluator",
        evaluator_version="0.1.0",
        model_role=ModelRole.BATCH_TEST if with_model else None,
        model_endpoint="batch-endpoint" if with_model else None,
        model_version="batch-v1" if with_model else None,
        model_cost_usd=Decimal("0.01") if with_model else None,
        model_latency_ms=12 if with_model else None,
        rubric_version="0.1.0",
        metrics=(EvaluationMetric(name="score", value=1.0, unit="ratio"),),
        failure_codes=(),
        decision=EvaluationDecision.SELECTED,
        created_at=datetime(2026, 7, 20, tzinfo=UTC),
    )


def test_benchmark_config_enforces_model_role_isolation_and_unique_parameters() -> None:
    batch = config(model_required=True, model_role=ModelRole.BATCH_TEST)
    assert batch.model_role is ModelRole.BATCH_TEST

    with pytest.raises(ValidationError, match="batch_test_model"):
        config(model_required=True, model_role=ModelRole.IMPLEMENTATION)
    with pytest.raises(ValidationError, match="cannot declare"):
        config(model_role=ModelRole.BATCH_TEST)
    duplicate = (
        EvaluationParameter(name="top_k", value=10),
        EvaluationParameter(name="top_k", value=20),
    )
    with pytest.raises(ValidationError, match="must be unique"):
        config(parameters=duplicate)


def test_evaluation_entry_requires_complete_model_audit_metadata() -> None:
    without_model = entry("evaluation.no-model").model_dump()
    without_model["model_endpoint"] = "orphan-endpoint"
    with pytest.raises(ValidationError, match="requires model_role"):
        EvaluationEntry.model_validate(without_model)

    complete = entry("evaluation.with-model", with_model=True).model_dump()
    for field in (
        "model_endpoint",
        "model_version",
        "model_cost_usd",
        "model_latency_ms",
    ):
        incomplete = {**complete, field: None}
        with pytest.raises(ValidationError, match="requires complete"):
            EvaluationEntry.model_validate(incomplete)


def test_config_fingerprint_normalizes_parameter_order() -> None:
    first = config(
        parameters=(
            EvaluationParameter(name="top_k", value=20),
            EvaluationParameter(name="seed_policy", value="fixed"),
        )
    )
    reordered = config(parameters=tuple(reversed(first.parameters)))

    assert config_fingerprint(first) == config_fingerprint(reordered)
    assert config_fingerprint(first) != config_fingerprint(
        config(seed=8, parameters=first.parameters)
    )


def test_evaluation_ledger_is_append_only_and_idempotent(
    ledger: EvaluationLedgerRepository,
) -> None:
    run_config = config()
    evaluation = entry("evaluation.1")

    assert ledger.append(run_config, evaluation) == evaluation
    assert ledger.append(run_config, evaluation) == evaluation
    assert ledger.list_run(run_config) == (evaluation,)

    changed = evaluation.model_copy(update={"evaluator_version": "different"})
    with pytest.raises(EvaluationConflictError, match="another entry"):
        ledger.append(run_config, changed)
    with pytest.raises(EvaluationConflictError, match="another entry"):
        ledger.append(config(seed=8), evaluation)


def test_evaluation_ledger_rejects_run_mismatch(
    ledger: EvaluationLedgerRepository,
) -> None:
    with pytest.raises(EvaluationConfigError, match="run_id"):
        ledger.append(config(), entry("evaluation.other", run_id=RunId("run.other")))
    with pytest.raises(EvaluationConfigError, match="model role"):
        ledger.append(config(), entry("evaluation.unexpected-model", with_model=True))
    implementation_values = entry("evaluation.implementation-model", with_model=True).model_dump()
    implementation_values["model_role"] = ModelRole.IMPLEMENTATION
    implementation_entry = EvaluationEntry.model_validate(implementation_values)
    with pytest.raises(EvaluationConfigError, match="model role"):
        ledger.append(
            config(model_required=True, model_role=ModelRole.BATCH_TEST),
            implementation_entry,
        )


def test_harness_exports_sorted_entries_and_configuration_to_parquet(
    ledger: EvaluationLedgerRepository, tmp_path: Path
) -> None:
    run_config = config(model_required=True, model_role=ModelRole.BATCH_TEST)
    second = entry("evaluation.2", with_model=True)
    first = entry("evaluation.1")
    output = tmp_path / "evaluation" / "entries.parquet"
    harness = EvaluationHarness(ledger)

    assert harness.record_and_export(run_config, (second, first), output) == output

    table = pq.read_table(output)
    assert table.column("evaluation_id").to_pylist() == ["evaluation.1", "evaluation.2"]
    assert table.column("model_role").to_pylist() == [None, "batch_test_model"]
    assert table.column("model_cost_usd").to_pylist() == [None, "0.01"]
    metadata = table.schema.metadata
    assert metadata is not None
    assert metadata[b"novel_agent.config_fingerprint"].decode() == config_fingerprint(run_config)
    assert b'"model_required":true' in metadata[b"novel_agent.config"]
