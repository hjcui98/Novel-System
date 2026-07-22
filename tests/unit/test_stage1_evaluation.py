from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from novel_agent.domain.benchmark import BenchmarkBundle, BenchmarkTrack, Stage1BenchmarkResult
from novel_agent.domain.ids import RunId, StableId, TaskId
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelRole,
    RetrievalInferenceCallRecord,
    RetrievalInferenceOperation,
    RetrievalInferenceStatus,
    RetrievalInferenceUsage,
)
from novel_agent.domain.runtime import EvaluationDecision
from novel_agent.services.stage1_benchmark import Stage1BenchmarkRunner
from novel_agent.services.stage1_evaluation import build_stage1_evaluation_records
from tests.fixtures.stage1_synthetic import make_synthetic_bundle

NOW = datetime(2026, 7, 21, tzinfo=UTC)


def _audited_result() -> tuple[BenchmarkBundle, Stage1BenchmarkResult]:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    run_id = RunId("run.stage1.evaluation")
    result = Stage1BenchmarkRunner().run(bundle, case.case_id, BenchmarkTrack.ORACLE)
    call = RetrievalInferenceCallRecord(
        call_id=StableId("model-call.stage1.evaluation"),
        run_id=run_id,
        task_id=TaskId("task.stage1.evaluation"),
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.EVALUATION,
        trace_id="trace.stage1.evaluation",
        endpoint="http://127.0.0.1:8081/v1/embeddings",
        model="BAAI/bge-m3",
        revision="a" * 40,
        runtime_fingerprint="b" * 64,
        operation=RetrievalInferenceOperation.EMBEDDING,
        usage=RetrievalInferenceUsage(
            input_items=2,
            input_characters=20,
            output_items=2,
            cost_usd=Decimal("0"),
        ),
        latency_ms=12,
        started_at=NOW,
        completed_at=NOW,
        status=RetrievalInferenceStatus.SUCCEEDED,
    )
    return bundle, result.model_copy(update={"run_id": run_id, "retrieval_model_calls": (call,)})


def test_stage1_result_builds_reproducible_model_evaluation_entries() -> None:
    bundle, result = _audited_result()
    config, entries = build_stage1_evaluation_records(result, bundle.content_hash, created_at=NOW)

    assert config.model_required is True
    assert config.model_role is ModelRole.BATCH_TEST
    assert config.dataset_hash == bundle.content_hash
    assert len(entries) == 16
    assert len({entry.evaluation_id for entry in entries}) == 16
    assert all(entry.run_id == result.run_id for entry in entries)
    assert all(entry.model_cost_usd == 0 and entry.model_latency_ms == 12 for entry in entries)
    assert any(metric.name == "future_leakage_rate" for metric in entries[0].metrics)
    assert {entry.decision for entry in entries} <= {
        EvaluationDecision.SELECTED,
        EvaluationDecision.REJECTED,
    }


def test_stage1_evaluation_rejects_missing_or_invalid_model_audit() -> None:
    bundle, result = _audited_result()
    with pytest.raises(ValueError, match="requires an audited run"):
        build_stage1_evaluation_records(
            result.model_copy(update={"retrieval_model_calls": ()}),
            bundle.content_hash,
            created_at=NOW,
        )
    failed_call = result.retrieval_model_calls[0].model_copy(
        update={"status": RetrievalInferenceStatus.FAILED, "error_type": "TimeoutError"}
    )
    with pytest.raises(ValueError, match="invalid model audit"):
        build_stage1_evaluation_records(
            result.model_copy(update={"retrieval_model_calls": (failed_call,)}),
            bundle.content_hash,
            created_at=NOW,
        )
