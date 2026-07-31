from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import FakeModelEndpoint, ScriptedModelEndpoint
from novel_agent.domain.ids import ArtifactId, RunId, StableId, TaskId
from novel_agent.domain.memory import RetrievalUnitKind
from novel_agent.domain.memory_benchmark import (
    BenchmarkInformationProfile,
    ContextAssemblyStatus,
    EvidenceSet,
    EvidenceStageCoverage,
    EvidenceStageFailure,
    FreezeReceipt,
    GoldMatchStatus,
    PerGoldStageLossDiagnostic,
)
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.benchmark_importer import content_id
from novel_agent.services.gold_evidence_matching import GoldEvidenceMatcher
from novel_agent.services.memory_benchmark_contract import build_safe_task_contract
from novel_agent.services.memory_benchmark_evaluation import (
    MemoryBenchmarkEvaluator,
    ModelSemanticSupportVerifier,
    SemanticGoldJudgment,
    SemanticSupport,
    SemanticVerificationBatch,
)
from novel_agent.services.memory_benchmark_metric_contracts import GoldMetricContractBuilder
from novel_agent.services.memory_pipeline import AnchorBuilder
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.writer_context_assembler import WriterContextAssembler
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


def test_semantic_verifier_rejects_invalid_batch_limits() -> None:
    gateway = ModelGateway(())

    with pytest.raises(ValueError, match="batch size"):
        ModelSemanticSupportVerifier(gateway, batch_size=0)
    with pytest.raises(ValueError, match="batch attempts"):
        ModelSemanticSupportVerifier(gateway, max_batch_attempts=0)


def _frozen() -> tuple[Any, ...]:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    history, _future = bundle.text_roots
    world = bundle.world_roots[0]
    plan = bundle.plan_roots[0]
    gold = case.operational_constraint_gold[0]
    units = AnchorBuilder().build(
        world,
        history,
        plan,
        snapshot_id=StableId("snapshot.evaluator"),
    )
    unit = next(
        item
        for item in units
        if item.unit_kind is RetrievalUnitKind.STATE_ANCHOR
        and {ref.evidence_id for ref in item.evidence_refs}.intersection(
            ref.evidence_id for ref in gold.evidence_refs
        )
    )
    task = build_safe_task_contract(
        case_id=case.case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    assembled = WriterContextAssembler().assemble(
        task=task,
        units=(unit.model_copy(update={"mandatory": True}),),
        needs=(),
        basis_commit_id=world.source_commit,
        basis_snapshot_id=StableId("snapshot.evaluator"),
        arm="A",
        writer_token_budget=1000,
    )
    package = assembled.package
    receipt = FreezeReceipt(
        receipt_id=StableId("freeze.evaluator"),
        public_input_hash=content_id({"public": True}),
        code_version="test",
        run_config_hash=content_id({"config": True}),
        arm_artifact_hashes={
            "A": content_id(package.model_dump(mode="json")),
            "B": content_id({"failure": "not-run"}),
            "C": content_id({"failure": "not-run"}),
        },
        frozen_before_reveal=True,
    )
    return gold, package, assembled.evidence_ledger, receipt


def _metric_args(
    tmp_path: Path,
    gold_items: tuple[Any, ...],
) -> dict[str, Any]:
    manifest_id = StableId("evaluator-manifest.unit")
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "metric-objects"))
    builder = GoldMetricContractBuilder(repository)
    _manifest, manifest_ref = builder.build_manifest(
        gold_items=gold_items,
        evaluator_manifest_id=manifest_id,
    )
    return {
        "evaluator_manifest_id": manifest_id,
        "evaluator_manifest_ref": manifest_ref,
        "evaluator_manifest_hash": manifest_ref.artifact_id,
        "gold_metric_descriptors": builder.build(
            gold_items=gold_items,
            evaluator_manifest_id=manifest_id,
            evaluator_manifest_hash=manifest_ref.artifact_id,
        ),
    }


def test_per_gold_evaluator_requires_semantics_and_accepted_provenance(tmp_path: Path) -> None:
    gold, package, ledger, receipt = _frozen()
    report = MemoryBenchmarkEvaluator(
        semantic_verifier=lambda _gold, _items: SemanticSupport.SUPPORTS
    ).evaluate(
        package=package,
        evidence_ledger=ledger,
        gold_items=(gold,),
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        freeze_receipt=receipt,
        **_metric_args(tmp_path, (gold,)),
    )

    assert report.comparisons[0].status is GoldMatchStatus.HIT
    assert report.weighted_coverage == 1.0
    assert report.mandatory_hit_rate == 1.0


def test_typed_context_failure_produces_auditable_all_miss_report(tmp_path: Path) -> None:
    gold, _package, _ledger, receipt = _frozen()
    evaluator = MemoryBenchmarkEvaluator()
    report = evaluator.evaluate_typed_failure(
        gold_items=(gold,),
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        assembly_status=ContextAssemblyStatus.EVIDENCE_INSUFFICIENT,
        freeze_receipt=receipt,
        **_metric_args(tmp_path, (gold,)),
    )

    assert report.comparisons[0].status is GoldMatchStatus.MISS
    assert "EVIDENCE_INSUFFICIENT" in report.comparisons[0].explanation
    assert report.weighted_coverage == 0.0
    assert report.mandatory_hit_rate == 0.0
    with pytest.raises(ValueError, match="READY context"):
        evaluator.evaluate_typed_failure(
            gold_items=(gold,),
            profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            assembly_status=ContextAssemblyStatus.READY,
            freeze_receipt=receipt,
            **_metric_args(tmp_path, (gold,)),
        )


def test_semantically_correct_claim_without_accepted_provenance_is_untraceable(
    tmp_path: Path,
) -> None:
    gold, package, ledger, receipt = _frozen()
    unrelated = gold.evidence_refs[0].model_copy(
        update={
            "evidence_id": StableId("evidence.unrelated"),
            "object_hash": ArtifactId("sha256:" + "f" * 64),
        }
    )
    untraceable_gold = gold.model_copy(
        update={"evidence_refs": (unrelated,), "accepted_evidence_sets": ()}
    )
    report = MemoryBenchmarkEvaluator(
        semantic_verifier=lambda _gold, _items: SemanticSupport.SUPPORTS
    ).evaluate(
        package=package,
        evidence_ledger=ledger,
        gold_items=(untraceable_gold,),
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        freeze_receipt=receipt,
        **_metric_args(tmp_path, (untraceable_gold,)),
    )

    assert report.comparisons[0].status is GoldMatchStatus.UNTRACEABLE


def test_semantically_partial_claim_with_partial_accepted_provenance_is_partial(
    tmp_path: Path,
) -> None:
    gold, package, ledger, receipt = _frozen()
    present = ledger.entries[0].evidence_refs[0]
    absent = present.model_copy(
        update={
            "evidence_id": StableId("evidence.absent"),
            "object_hash": ArtifactId("sha256:" + "d" * 64),
        }
    )
    partial_gold = gold.model_copy(
        update={
            "accepted_evidence_sets": (
                EvidenceSet(
                    evidence_set_id=StableId("accepted.partial-evaluator"),
                    evidence_refs=(present, absent),
                ),
            )
        }
    )
    report = MemoryBenchmarkEvaluator(
        semantic_verifier=lambda _gold, _items: SemanticSupport.PARTIAL
    ).evaluate(
        package=package,
        evidence_ledger=ledger,
        gold_items=(partial_gold,),
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        freeze_receipt=receipt,
        **_metric_args(tmp_path, (partial_gold,)),
    )

    assert report.comparisons[0].status is GoldMatchStatus.PARTIAL
    assert report.comparisons[0].matched_evidence_ledger_ids


def test_same_chapter_without_exact_object_and_span_does_not_match() -> None:
    gold, _package, ledger, _receipt = _frozen()
    expected = gold.evidence_refs[0].model_copy(
        update={
            "evidence_id": StableId("evidence.same-chapter-only"),
            "object_hash": ArtifactId("sha256:" + "e" * 64),
            "span": None,
        }
    )
    broad_gold = gold.model_copy(
        update={"evidence_refs": (expected,), "accepted_evidence_sets": ()}
    )

    assert GoldEvidenceMatcher().match(broad_gold, ledger).matched is False


def test_model_semantic_verifier_batches_frozen_claims_and_gold() -> None:
    gold, package, ledger, _receipt = _frozen()
    expected = SemanticVerificationBatch(
        judgments=(
            SemanticGoldJudgment(
                gold_id=gold.gold_id,
                all_claims_support=SemanticSupport.SUPPORTS,
                traceable_claims_support=SemanticSupport.SUPPORTS,
                all_context_item_ids=(package.current_world_state[0].context_item_id,),
                traceable_context_item_ids=(package.current_world_state[0].context_item_id,),
                explanation="the traceable frozen conclusion expresses the Gold fact",
            ),
        )
    )
    endpoint = FakeModelEndpoint(expected.model_dump_json())
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="semantic-fake",
                model_name="semantic-fake",
                adapter=endpoint,
            ),
        )
    )
    batch, calls = asyncio.run(
        ModelSemanticSupportVerifier(gateway).verify(
            gold_items=(gold,),
            package=package,
            evidence_ledger=ledger,
            request=ModelRequest(
                request_id=StableId("request.semantic"),
                run_id=RunId("run.semantic"),
                task_id=TaskId("task.semantic"),
                model_role=ModelRole.BATCH_TEST,
                purpose=ModelCallPurpose.EVALUATION,
                trace_id="trace-semantic",
                prompt="replaced",
            ),
        )
    )

    assert batch == expected
    assert len(calls) == 1
    assert calls[0].purpose is ModelCallPurpose.EVALUATION
    assert (gold.fact or gold.description) in endpoint.requests[0].prompt
    expected_gold_type = gold.gold_type.value if gold.gold_type is not None else gold.kind.value
    assert expected_gold_type in endpoint.requests[0].prompt
    assert "accepted author-visible plan node" in endpoint.requests[0].prompt
    assert package.current_world_state[0].context_item_id.root in endpoint.requests[0].prompt


def test_model_semantic_verifier_fails_closed_on_unbound_traceable_support() -> None:
    gold, package, ledger, _receipt = _frozen()
    context_item_id = package.current_world_state[0].context_item_id
    claimed = SemanticVerificationBatch(
        judgments=(
            SemanticGoldJudgment(
                gold_id=gold.gold_id,
                all_claims_support=SemanticSupport.SUPPORTS,
                traceable_claims_support=SemanticSupport.SUPPORTS,
                all_context_item_ids=(context_item_id,),
                traceable_context_item_ids=(),
                explanation="claimed traceable support without a bound context item",
            ),
        )
    )
    endpoint = FakeModelEndpoint(claimed.model_dump_json())
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="semantic-unbound",
                model_name="semantic-unbound",
                adapter=endpoint,
            ),
        )
    )

    batch, _calls = asyncio.run(
        ModelSemanticSupportVerifier(gateway).verify(
            gold_items=(gold,),
            package=package,
            evidence_ledger=ledger,
            request=ModelRequest(
                request_id=StableId("request.semantic.unbound"),
                run_id=RunId("run.semantic"),
                task_id=TaskId("task.semantic"),
                model_role=ModelRole.BATCH_TEST,
                purpose=ModelCallPurpose.EVALUATION,
                trace_id="trace-semantic",
                prompt="replaced",
            ),
        )
    )

    judgment = batch.judgments[0]
    assert judgment.all_claims_support is SemanticSupport.SUPPORTS
    assert judgment.traceable_claims_support is SemanticSupport.NONE
    assert "TRACEABLE_CLAIMS_SUPPORT_WITHOUT_CONTEXT_ITEM_IDS" in judgment.validation_diagnostics


def test_evaluator_revalidates_model_context_item_bindings(tmp_path: Path) -> None:
    gold, package, ledger, receipt = _frozen()
    judgment = SemanticGoldJudgment(
        gold_id=gold.gold_id,
        all_claims_support=SemanticSupport.SUPPORTS,
        traceable_claims_support=SemanticSupport.SUPPORTS,
        all_context_item_ids=(StableId("context-item.outside-frozen-package"),),
        traceable_context_item_ids=(StableId("context-item.outside-frozen-package"),),
        explanation="claimed support from an unknown context item",
    )

    report = MemoryBenchmarkEvaluator().evaluate(
        package=package,
        evidence_ledger=ledger,
        gold_items=(gold,),
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        freeze_receipt=receipt,
        **_metric_args(tmp_path, (gold,)),
        semantic_judgments={gold.gold_id: judgment},
        verifier_receipt_ref=package.evidence_ledger_ref,
    )

    assert report.comparisons[0].status is GoldMatchStatus.MISS


def test_model_semantic_verifier_retries_incomplete_gold_id_set() -> None:
    gold, package, ledger, _receipt = _frozen()
    complete = SemanticVerificationBatch(
        judgments=(
            SemanticGoldJudgment(
                gold_id=gold.gold_id,
                all_claims_support=SemanticSupport.SUPPORTS,
                traceable_claims_support=SemanticSupport.SUPPORTS,
                all_context_item_ids=(package.current_world_state[0].context_item_id,),
                traceable_context_item_ids=(package.current_world_state[0].context_item_id,),
                explanation="supported",
            ),
        )
    )
    empty = SemanticVerificationBatch(judgments=())
    responses = iter((empty.model_dump_json(), complete.model_dump_json()))
    endpoint = ScriptedModelEndpoint(lambda _request: next(responses))
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="semantic-scripted",
                model_name="semantic-scripted",
                adapter=endpoint,
            ),
        )
    )

    batch, calls = asyncio.run(
        ModelSemanticSupportVerifier(gateway).verify(
            gold_items=(gold,),
            package=package,
            evidence_ledger=ledger,
            request=ModelRequest(
                request_id=StableId("request.semantic.retry"),
                run_id=RunId("run.semantic"),
                task_id=TaskId("task.semantic"),
                model_role=ModelRole.BATCH_TEST,
                purpose=ModelCallPurpose.EVALUATION,
                trace_id="trace-semantic",
                prompt="replaced",
            ),
        )
    )

    assert batch == complete
    assert len(calls) == 2
    assert endpoint.requests[1].request_id.root.endswith(".batch1.retry2")


def test_model_semantic_verifier_fails_closed_after_incomplete_retries() -> None:
    gold, package, ledger, _receipt = _frozen()
    endpoint = FakeModelEndpoint(SemanticVerificationBatch(judgments=()).model_dump_json())
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="semantic-incomplete",
                model_name="semantic-incomplete",
                adapter=endpoint,
            ),
        )
    )

    with pytest.raises(ValueError, match="Gold id set"):
        asyncio.run(
            ModelSemanticSupportVerifier(gateway, max_batch_attempts=2).verify(
                gold_items=(gold,),
                package=package,
                evidence_ledger=ledger,
                request=ModelRequest(
                    request_id=StableId("request.semantic.exhausted"),
                    run_id=RunId("run.semantic"),
                    task_id=TaskId("task.semantic"),
                    model_role=ModelRole.BATCH_TEST,
                    purpose=ModelCallPurpose.EVALUATION,
                    trace_id="trace-semantic",
                    prompt="replaced",
                ),
            )
        )

    assert len(endpoint.requests) == 2


def test_model_semantic_verifier_fails_closed_on_transport_timeout() -> None:
    gold, package, ledger, _receipt = _frozen()

    class TimeoutGateway:
        async def generate_structured(self, *_args: object) -> tuple[object, object]:
            raise TimeoutError

    batch, calls = asyncio.run(
        ModelSemanticSupportVerifier(TimeoutGateway()).verify(  # type: ignore[arg-type]
            gold_items=(gold,),
            package=package,
            evidence_ledger=ledger,
            request=ModelRequest(
                request_id=StableId("request.semantic.timeout"),
                run_id=RunId("run.semantic"),
                task_id=TaskId("task.semantic"),
                model_role=ModelRole.BATCH_TEST,
                purpose=ModelCallPurpose.EVALUATION,
                trace_id="trace-semantic",
                prompt="replaced",
            ),
        )
    )

    assert calls == ()
    assert batch.judgments[0].all_claims_support is SemanticSupport.NONE
    assert "timed out" in batch.judgments[0].explanation


def test_evaluator_requires_receipt_for_model_semantic_judgments(tmp_path: Path) -> None:
    gold, package, ledger, receipt = _frozen()
    judgment = SemanticGoldJudgment(
        gold_id=gold.gold_id,
        all_claims_support=SemanticSupport.SUPPORTS,
        traceable_claims_support=SemanticSupport.SUPPORTS,
        all_context_item_ids=(package.current_world_state[0].context_item_id,),
        traceable_context_item_ids=(package.current_world_state[0].context_item_id,),
        explanation="supported",
    )
    evaluator = MemoryBenchmarkEvaluator()
    kwargs = {
        "package": package,
        "evidence_ledger": ledger,
        "gold_items": (gold,),
        "profile": BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        "freeze_receipt": receipt,
        **_metric_args(tmp_path, (gold,)),
        "semantic_judgments": {gold.gold_id: judgment},
    }

    with pytest.raises(ValueError, match="require a verifier receipt"):
        evaluator.evaluate(**kwargs)
    report = evaluator.evaluate(
        **kwargs,
        verifier_receipt_ref=package.evidence_ledger_ref,
    )
    assert report.comparisons[0].status is GoldMatchStatus.HIT
    assert report.comparisons[0].verifier_receipt_ref == package.evidence_ledger_ref


def test_semantic_judgment_validation_rejects_unbound_support_shapes() -> None:
    _gold, package, _ledger, _receipt = _frozen()
    item_id = package.current_world_state[0].context_item_id
    traceable_not_all = SemanticGoldJudgment(
        gold_id=StableId("gold.semantic.traceable-not-all"),
        all_claims_support=SemanticSupport.NONE,
        traceable_claims_support=SemanticSupport.SUPPORTS,
        all_context_item_ids=(),
        traceable_context_item_ids=(item_id,),
        explanation="invalid traceable shape",
    )
    normalized = ModelSemanticSupportVerifier._validate_judgment(
        traceable_not_all,
        frozen_item_ids={item_id},
        matcher_traceable_item_ids={item_id},
    )
    assert normalized.traceable_claims_support is SemanticSupport.NONE
    assert "TRACEABLE_CONTEXT_ITEM_IDS_NOT_IN_ALL_CONTEXT_ITEMS" in (
        normalized.validation_diagnostics
    )

    support_without_ids = SemanticGoldJudgment(
        gold_id=StableId("gold.semantic.all-without-ids"),
        all_claims_support=SemanticSupport.SUPPORTS,
        traceable_claims_support=SemanticSupport.NONE,
        all_context_item_ids=(),
        traceable_context_item_ids=(),
        explanation="invalid all-context shape",
    )
    normalized = ModelSemanticSupportVerifier._validate_judgment(
        support_without_ids,
        frozen_item_ids={item_id},
        matcher_traceable_item_ids={item_id},
    )
    assert normalized.all_claims_support is SemanticSupport.NONE
    assert "ALL_CLAIMS_SUPPORT_WITHOUT_CONTEXT_ITEM_IDS" in normalized.validation_diagnostics


def test_evaluator_rejects_stage_diagnostic_and_metric_descriptor_identity_drift(
    tmp_path: Path,
) -> None:
    gold, _package, _ledger, _receipt = _frozen()
    evaluator = MemoryBenchmarkEvaluator()
    metric_args = _metric_args(tmp_path, (gold,))
    manifest_id = metric_args["evaluator_manifest_id"]
    manifest_hash = metric_args["evaluator_manifest_hash"]
    descriptors = metric_args["gold_metric_descriptors"]
    binding = descriptors[gold.gold_id]

    with pytest.raises(ValueError, match="descriptor ids"):
        evaluator._verify_metric_descriptors(
            (gold,),
            {},
            evaluator_manifest_id=manifest_id,
            evaluator_manifest_hash=manifest_hash,
        )

    for update, message in (
        ({"gold_id": StableId("gold.descriptor.wrong")}, "Gold id mismatch"),
        ({"weight": gold.weight + 1}, "score fields mismatch"),
        ({"gold_type": None}, "classification mismatch"),
        ({"applicable_profiles": ()}, "profile applicability mismatch"),
        (
            {"evaluator_manifest_id": StableId("evaluator-manifest.wrong")},
            "evaluator manifest mismatch",
        ),
    ):
        drifted = binding.model_copy(
            update={"descriptor": binding.descriptor.model_copy(update=update)}
        )
        with pytest.raises(ValueError, match=message):
            evaluator._verify_metric_descriptors(
                (gold,),
                {gold.gold_id: drifted},
                evaluator_manifest_id=manifest_id,
                evaluator_manifest_hash=manifest_hash,
            )

    coverage = EvidenceStageCoverage(accepted_reference_count=0, matched_reference_count=0)
    diagnostic = PerGoldStageLossDiagnostic(
        gold_id=gold.gold_id,
        candidate=coverage,
        rank_selected=coverage,
        stage1_selected=coverage,
        writer_ledger=coverage,
        primary_failure=EvidenceStageFailure.F_NEED_ROUTE_RETRIEVE,
    )
    with pytest.raises(ValueError, match="duplicate Gold"):
        evaluator._verify_stage_loss_diagnostics((gold,), (diagnostic, diagnostic))
    with pytest.raises(ValueError, match="do not match"):
        evaluator._verify_stage_loss_diagnostics(
            (gold,),
            (diagnostic.model_copy(update={"gold_id": StableId("gold.diagnostic.wrong")}),),
        )
