from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.domain.ids import RunId, StableId, TaskId
from novel_agent.domain.memory_benchmark import (
    BenchmarkInformationProfile,
    ContextAssemblyStatus,
    GoldMatchStatus,
    MemoryBenchmarkCaseArmReport,
)
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.benchmark_importer import content_id
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.memory_benchmark_evaluation import (
    MemoryBenchmarkEvaluator,
    ModelSemanticSupportVerifier,
    SemanticGoldJudgment,
    SemanticSupport,
    SemanticVerificationBatch,
)
from novel_agent.services.memory_benchmark_metric_contracts import GoldMetricContractBuilder
from novel_agent.services.memory_benchmark_reporting import MemoryBenchmarkReporter
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from tests.fixtures.stage2_memory_benchmark import (
    frozen_evaluation_inputs,
    resolved_public_comparison,
)


def _metric_bundle(
    tmp_path: Path,
    gold_items: tuple[Any, ...],
) -> tuple[ArtifactRepository, dict[str, Any]]:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "metric-objects"))
    manifest_id = StableId("evaluator-manifest.golden")
    builder = GoldMetricContractBuilder(repository)
    _manifest, manifest_ref = builder.build_manifest(
        gold_items=gold_items,
        evaluator_manifest_id=manifest_id,
    )
    return repository, {
        "evaluator_manifest_id": manifest_id,
        "evaluator_manifest_ref": manifest_ref,
        "evaluator_manifest_hash": manifest_ref.artifact_id,
        "gold_metric_descriptors": builder.build(
            gold_items=gold_items,
            evaluator_manifest_id=manifest_id,
            evaluator_manifest_hash=manifest_ref.artifact_id,
        ),
    }


@pytest.mark.parametrize(
    ("support", "expected"),
    (
        (SemanticSupport.SUPPORTS, GoldMatchStatus.HIT),
        (SemanticSupport.PARTIAL, GoldMatchStatus.PARTIAL),
        (SemanticSupport.CONTRADICTS, GoldMatchStatus.CONTRADICTS),
        (SemanticSupport.NONE, GoldMatchStatus.MISS),
    ),
)
def test_all_traceable_gold_statuses_are_stable(
    support: SemanticSupport,
    expected: GoldMatchStatus,
    tmp_path: Path,
) -> None:
    gold, package, ledger, receipt = frozen_evaluation_inputs()
    _repository, metric_args = _metric_bundle(tmp_path, (gold,))
    report = MemoryBenchmarkEvaluator(semantic_verifier=lambda _gold, _items: support).evaluate(
        package=package,
        evidence_ledger=ledger,
        gold_items=(gold,),
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        freeze_receipt=receipt,
        **metric_args,
    )
    assert report.comparisons[0].status is expected
    assert report.comparisons[0].explanation


def test_deterministic_semantic_floor_distinguishes_support_partial_and_polarity() -> None:
    gold, package, _ledger, _receipt = frozen_evaluation_inputs()
    item = package.current_world_state[0]
    positive = gold.model_copy(update={"fact": "alpha beta gamma delta"})
    assert (
        MemoryBenchmarkEvaluator._deterministic_semantic_verifier(
            positive, (item.model_copy(update={"claim": "alpha beta gamma delta"}),)
        )
        is SemanticSupport.SUPPORTS
    )
    assert (
        MemoryBenchmarkEvaluator._deterministic_semantic_verifier(
            positive, (item.model_copy(update={"claim": "alpha beta"}),)
        )
        is SemanticSupport.PARTIAL
    )
    assert (
        MemoryBenchmarkEvaluator._deterministic_semantic_verifier(
            positive, (item.model_copy(update={"claim": "not alpha beta gamma delta"}),)
        )
        is SemanticSupport.CONTRADICTS
    )
    assert (
        MemoryBenchmarkEvaluator._deterministic_semantic_verifier(positive, ())
        is SemanticSupport.NONE
    )
    assert MemoryBenchmarkEvaluator._polarity("no answer") == -1


def test_semantic_batch_and_evaluator_receipts_fail_closed(tmp_path: Path) -> None:
    gold, package, ledger, receipt = frozen_evaluation_inputs()
    duplicate = SemanticGoldJudgment(
        gold_id=gold.gold_id,
        all_claims_support=SemanticSupport.NONE,
        traceable_claims_support=SemanticSupport.NONE,
        all_context_item_ids=(),
        traceable_context_item_ids=(),
        explanation="none",
    )
    with pytest.raises(ValidationError, match="duplicate Gold"):
        SemanticVerificationBatch(judgments=(duplicate, duplicate))
    endpoint = FakeModelEndpoint(
        SemanticVerificationBatch(
            judgments=(duplicate.model_copy(update={"gold_id": StableId("gold.wrong")}),)
        ).model_dump_json()
    )
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="semantic-wrong",
                model_name="semantic-wrong",
                adapter=endpoint,
            ),
        )
    )
    request = ModelRequest(
        request_id=StableId("request.semantic-wrong"),
        run_id=RunId("run.semantic-wrong"),
        task_id=TaskId("task.semantic-wrong"),
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.EVALUATION,
        trace_id="trace-semantic-wrong",
        prompt="replaced",
    )
    with pytest.raises(ValueError, match="Gold id set"):
        asyncio.run(
            ModelSemanticSupportVerifier(gateway).verify(
                gold_items=(gold,),
                package=package,
                evidence_ledger=ledger,
                request=request,
            )
        )
    for size in (0, 9):
        with pytest.raises(ValueError, match="batch size"):
            ModelSemanticSupportVerifier(gateway, batch_size=size)

    judgment = {gold.gold_id: duplicate}
    _repository, metric_args = _metric_bundle(tmp_path, (gold,))
    with pytest.raises(ValueError, match="Gold ids"):
        MemoryBenchmarkEvaluator().evaluate(
            package=package,
            evidence_ledger=ledger,
            gold_items=(gold,),
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            freeze_receipt=receipt,
            **metric_args,
            semantic_judgments={StableId("gold.wrong"): duplicate},
            verifier_receipt_ref=package.evidence_ledger_ref,
        )
    assert judgment


def test_frozen_hashes_and_ready_status_are_mandatory(tmp_path: Path) -> None:
    gold, package, ledger, receipt = frozen_evaluation_inputs()
    evaluator = MemoryBenchmarkEvaluator()
    _repository, metric_args = _metric_bundle(tmp_path, (gold,))
    nonready = package.model_copy(
        update={
            "budget_report": package.budget_report.model_copy(
                update={"final_status": ContextAssemblyStatus.EVIDENCE_INSUFFICIENT}
            )
        }
    )
    with pytest.raises(ValueError, match="only READY"):
        evaluator.evaluate(
            package=nonready,
            evidence_ledger=ledger,
            freeze_receipt=receipt,
            gold_items=(gold,),
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            **metric_args,
        )
    with pytest.raises(ValueError, match="ledger hash"):
        evaluator.evaluate(
            package=package,
            evidence_ledger=ledger.model_copy(
                update={"rendered_tokens": ledger.rendered_tokens + 1}
            ),
            freeze_receipt=receipt,
            gold_items=(gold,),
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            **metric_args,
        )
    bad_receipt = receipt.model_copy(
        update={
            "arm_artifact_hashes": receipt.arm_artifact_hashes
            | {"A": content_id({"wrong-package": True})}
        }
    )
    with pytest.raises(ValueError, match="freeze receipt hash"):
        evaluator.evaluate(
            package=package,
            evidence_ledger=ledger,
            freeze_receipt=bad_receipt,
            gold_items=(gold,),
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            **metric_args,
        )


def test_unified_reporting_preserves_arms_and_rejects_profile_mixing(tmp_path: Path) -> None:
    _bundle, private_case, _public, _runner, comparison = resolved_public_comparison()
    package = comparison.deterministic.writer_context
    ledger = comparison.deterministic.evidence_ledger
    receipt = comparison.freeze_receipt
    assert package is not None and ledger is not None and receipt is not None
    gold_items = (
        *private_case.observed_use_gold,
        *private_case.operational_constraint_gold,
        *private_case.plan_obligation_gold,
    )
    repository, metric_args = _metric_bundle(tmp_path, gold_items)
    evaluation = MemoryBenchmarkEvaluator(
        semantic_verifier=lambda _gold, _items: SemanticSupport.SUPPORTS
    ).evaluate(
        package=package,
        evidence_ledger=ledger,
        gold_items=gold_items,
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        freeze_receipt=receipt,
        **metric_args,
    )
    ledger_ref = repository.put(
        canonical_json_bytes(ledger.model_dump(mode="json")),
        "application/vnd.test.evidence-ledger+json",
        package.evidence_ledger_ref.schema_version,
    )
    case = MemoryBenchmarkCaseArmReport(
        case_id=StableId("ZTJ-P001"),
        checkpoint_chapter=20,
        arm="A",
        code_version="golden-test.v1",
        run_config_hash=receipt.run_config_hash,
        benchmark_contract_hash=content_id({"golden": "benchmark-contract"}),
        matcher_version="gold_evidence_matcher.v3",
        writer_token_budget=package.budget_report.configured_writer_token_budget,
        evidence_ledger_token_budget=12_000,
        assembly_status=ContextAssemblyStatus.READY,
        writer_tokens=100,
        evidence_tokens=100,
        selected_unit_count=1,
        comparable=True,
        writer_evidence_ledger_ref=ledger_ref,
        evaluation=evaluation,
    )
    reporter = MemoryBenchmarkReporter(
        artifact_reader=repository.read_verified,
        enforce_formal_contract=False,
    )
    visible = reporter.aggregate(
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        cases=(case,),
    )
    assert visible.case_count == 1 and visible.cases[0].arm == "A"

    with pytest.raises(ValueError, match="mix information profiles"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
            cases=(case,),
        )
    with pytest.raises(ValueError, match="duplicate"):
        reporter.aggregate(
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            cases=(case, case),
        )
