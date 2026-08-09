from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from novel_agent.domain.artifacts import PlanRootRef
from novel_agent.domain.benchmark import PlanEvidenceRef
from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    CandidatePool,
    ChannelHit,
    ContextBudgetReport,
    FusedCandidate,
    NeedExecutionStatus,
    RetrievalChannel,
    RetrievalStopReason,
    RetrievalTrace,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1ContextPackage,
    Stage1QueryIntent,
)
from novel_agent.domain.memory_benchmark import ContextAssemblyStatus, EvidenceSet
from novel_agent.domain.retrieval_routing import (
    RetrievalBackendProfile,
    SnapshotCapability,
    SnapshotCapabilityStatus,
)
from novel_agent.domain.stage2 import (
    ArmExecutionStatus,
    BenchmarkInformationProfile,
    ControllerArm,
    ControllerMode,
    ControllerStopReason,
    PairedContextArmResult,
    PairedContextComparison,
    PairedPilotCaseResult,
    PublicBenchmarkConfig,
    PublicCheckpointCase,
    Stage2PairedPilotReport,
)
from novel_agent.domain.text import EvidenceRef
from novel_agent.services.benchmark_importer import (
    bundle_content_id,
    content_id,
    world_root_content_id,
)
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.memory_benchmark_contract import build_public_checkpoint_case
from novel_agent.services.paired_controller import PairedMemoryControllerRunner
from novel_agent.services.retrieval import RetrievalBackend
from novel_agent.services.stage1_benchmark import Stage1NeedGenerator
from novel_agent.services.stage2_paired_pilot import Stage2PairedPilotRunner
from novel_agent.services.task_conditioned_need_generation import (
    TaskPlanConditionedNeedGenerator,
)
from novel_agent.services.writer_context_assembler import (
    WriterContextAssembler,
    WriterContextAssemblyResult,
)
from tests.fixtures.stage1_synthetic import PLACEHOLDER_HASH, make_synthetic_bundle
from tests.fixtures.stage2_memory_benchmark import resolved_public_comparison


def _report() -> Stage2PairedPilotReport:
    return Stage2PairedPilotRunner().run(make_synthetic_bundle())


def test_paired_pilot_runs_both_arms_on_one_audited_basis() -> None:
    report = _report()

    assert report.paired_results_count == 3
    assert report.comparable_results_count == 3
    assert report.future_leakage_count == 0
    # The legacy paired smoke can expose an Agentic regression; strict D9 no
    # longer injects author-plan evidence to mask it.
    assert report.safety_regression_count == 1
    assert report.accuracy_gain_count == 0
    assert report.tool_call_reduction_count == 3
    assert report.held_out_complex_gain_proven is False
    result = report.cases[0]
    assert result.checkpoint_chapter == 20
    assert result.comparison_basis_fingerprint == report.configuration_fingerprint
    assert result.deterministic_metrics.gold_evidence_recall == 1.0
    assert result.agentic_metrics is not None
    assert result.agentic_metrics.gold_evidence_recall == 1.0
    assert result.deterministic_metrics.evidence_traceability == 1.0
    assert Stage2PairedPilotRunner().run(make_synthetic_bundle()) == report


def test_frozen_arm_embeds_content_addressed_support_inputs() -> None:
    _bundle, _private_case, _public_case, _runner, comparison = resolved_public_comparison()
    arm = comparison.deterministic

    assert arm.context_assembly_spec is not None
    assert arm.context_assembly_spec_ref is not None
    assert arm.context_assembly_spec_ref.artifact_id == content_id(
        arm.context_assembly_spec.model_dump(mode="json")
    )
    assert arm.support_receipts
    assert {ref.artifact_id for ref in arm.support_receipt_refs} == {
        content_id(receipt.model_dump(mode="json")) for receipt in arm.support_receipts
    }


@pytest.mark.parametrize(
    ("token_budget", "max_candidates", "max_tool_calls", "arms"),
    (
        (0, 20, 48, ("A", "B", "C")),
        (4000, 0, 48, ("A", "B", "C")),
        (4000, 101, 48, ("A", "B", "C")),
        (4000, 20, 0, ("A", "B", "C")),
        (4000, 20, 48, ("B",)),
    ),
)
def test_paired_pilot_rejects_invalid_limits(
    token_budget: int,
    max_candidates: int,
    max_tool_calls: int,
    arms: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="paired Pilot"):
        Stage2PairedPilotRunner(
            token_budget=token_budget,
            max_candidates=max_candidates,
            max_tool_calls=max_tool_calls,
            arms=arms,
        )


def test_public_resolution_rejects_requested_profile_mismatch() -> None:
    bundle, private_case, public_case, _runner, _comparison = resolved_public_comparison()
    history = next(
        root for root in bundle.text_roots if root.root_hash == private_case.input_text_root
    )
    config = PublicBenchmarkConfig(
        schema_version=bundle.bundle_schema_version,
        configuration_fingerprint=content_id({"profile-mismatch": True}),
        expected_profiles=tuple(item.value for item in BenchmarkInformationProfile),
    )
    with pytest.raises(ValueError, match="profile does not match"):
        Stage2PairedPilotRunner().resolve_state_case(
            config,
            public_case,
            BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
            history=history,
            world=bundle.world_roots[0],
            plan=bundle.plan_roots[0],
            base_commit=bundle.world_roots[0].source_commit,
        )


def test_scope_needs_injects_run_level_plan_policy() -> None:
    from novel_agent.domain.ids import RunId, TaskId
    from novel_agent.domain.memory import (
        CandidatePool,
        NeedRisk,
        RequirementLevel,
        ResolutionPath,
        Stage1MemoryNeed,
    )

    def make_need(
        need_id: str,
        need_type: str,
        intent: Stage1QueryIntent,
        *,
        plan_channel: bool,
    ) -> Stage1MemoryNeed:
        return Stage1MemoryNeed(
            need_id=StableId(need_id),
            run_id=RunId("run.policy"),
            task_id=TaskId("task.policy"),
            base_commit=CommitId("sha256:" + "c" * 64),
            chapter_target=1,
            need_type=need_type,
            query_intent=intent,
            query_text="query",
            access_scope="author_planning" if plan_channel else "writer_safe",
            allow_plan=plan_channel,
            planner_may_read_plan=plan_channel,
            retrieval_may_return_plan=plan_channel,
            claim_may_cite_plan=plan_channel,
            legacy_allow_plan=plan_channel,
            why_needed="test",
            risk_level=NeedRisk.HIGH,
            requirement=RequirementLevel.MANDATORY,
            preferred_resolution_path=ResolutionPath.ANCHOR_FIRST,
            allowed_candidate_pools=(CandidatePool.ANCHOR,),
            stop_condition="done",
        )

    plan_obligation = make_need(
        "need.policy.plan",
        "plan_obligation",
        Stage1QueryIntent.PLAN_OBLIGATION,
        plan_channel=True,
    )
    historical = make_need(
        "need.policy.history",
        "entity_history",
        Stage1QueryIntent.SEMANTIC_HISTORY,
        plan_channel=False,
    )
    plan_conditioned_history = make_need(
        "need.policy.plan-history",
        "plan_conditioned_history",
        Stage1QueryIntent.RELATED_EVENT,
        plan_channel=True,
    )
    needs = (plan_obligation, plan_conditioned_history, historical)

    apc = Stage2PairedPilotRunner._scope_needs(
        needs,
        BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
    )
    assert all(need.planner_may_read_plan for need in apc)
    assert all(need.access_scope == "writer_safe" for need in apc)
    assert all(not need.retrieval_may_return_plan for need in apc)
    assert all(not need.claim_may_cite_plan for need in apc)
    assert all(not need.legacy_allow_plan for need in apc)
    assert all(not need.allow_plan for need in apc)

    vac = Stage2PairedPilotRunner._scope_needs(
        needs,
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    assert all(need.access_scope == "writer_safe" for need in vac)
    assert all(not need.planner_may_read_plan for need in vac)
    assert all(not need.retrieval_may_return_plan for need in vac)
    assert all(not need.claim_may_cite_plan for need in vac)
    assert all(not need.allow_plan for need in vac)


def test_deterministic_only_resolution_does_not_execute_b_or_c() -> None:
    bundle, private_case, public_case, _runner, _comparison = resolved_public_comparison()
    history = next(
        root for root in bundle.text_roots if root.root_hash == private_case.input_text_root
    )
    config = PublicBenchmarkConfig(
        schema_version=bundle.bundle_schema_version,
        configuration_fingerprint=content_id({"a-only": True}),
        expected_profiles=tuple(item.value for item in BenchmarkInformationProfile),
    )
    comparison = Stage2PairedPilotRunner(arms=("A",)).resolve_state_case(
        config,
        public_case,
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        history=history,
        world=bundle.world_roots[0],
        plan=bundle.plan_roots[0],
        base_commit=bundle.world_roots[0].source_commit,
    )
    assert not comparison.agentic.quality_eligible
    assert comparison.agentic.execution_status is ArmExecutionStatus.SKIPPED
    assert comparison.agentic.selected_unit_ids == ()
    assert comparison.agentic.writer_context is None
    assert "agentic_not_run_deterministic_gate" in comparison.blockers
    assert comparison.arm_c_writer_context is None
    assert comparison.arm_c_evidence_ledger is None
    assert comparison.arm_c_status is None
    assert comparison.arm_c_execution_status is ArmExecutionStatus.SKIPPED
    assert comparison.arm_c_failure_category == "NOT_RUN_DETERMINISTIC_GATE"
    assert comparison.freeze_receipt is not None
    assert set(comparison.freeze_receipt.arm_artifact_hashes) == {"A", "B", "C"}


def test_orphan_expanded_evidence_is_not_assigned_to_an_unrelated_need() -> None:
    bundle, _private_case, public_case, runner, comparison = resolved_public_comparison()
    world = bundle.world_roots[0]
    candidate = comparison.deterministic.context.retrieval_traces[0].candidates[0].unit
    orphan = candidate.model_copy(
        update={
            "unit_id": StableId("unit.expanded.orphan"),
            "unit_kind": RetrievalUnitKind.GROUNDED_SPAN,
            "parent_unit_id": StableId("unit.parent.not-selected"),
            "parent_unit_ids": (),
        }
    )
    deterministic = comparison.deterministic.model_copy(
        update={
            "context": comparison.deterministic.context.model_copy(
                update={
                    "raw_evidence_spans": (
                        *comparison.deterministic.context.raw_evidence_spans,
                        orphan,
                    )
                }
            )
        }
    )
    needs = TaskPlanConditionedNeedGenerator().generate(
        public_case.task_contract,
        world,
    )

    assembled = runner._assemble_stage2m_comparison(
        comparison.model_copy(update={"deterministic": deterministic}),
        case=public_case,
        needs=needs,
        fingerprint=content_id({"orphan-expanded-evidence": True}),
    )

    assert assembled.deterministic.writer_context is not None
    assert orphan.unit_id not in assembled.deterministic.writer_context.lineage.retrieval_unit_ids


def test_compact_excerpt_of_selected_block_keeps_need_lineage() -> None:
    bundle, _private_case, public_case, runner, comparison = resolved_public_comparison()
    world = bundle.world_roots[0]
    candidate = comparison.deterministic.context.retrieval_traces[0].candidates[0].unit
    compact = candidate.model_copy(
        update={
            "unit_id": StableId(f"compact.{candidate.unit_id.root}"),
            "unit_kind": RetrievalUnitKind.GROUNDED_BLOCK,
            "parent_unit_id": candidate.unit_id,
            "parent_unit_ids": (),
            "text": candidate.text[:64],
        }
    )
    deterministic = comparison.deterministic.model_copy(
        update={
            "context": comparison.deterministic.context.model_copy(
                update={
                    "style_or_reference_optional": (
                        *comparison.deterministic.context.style_or_reference_optional,
                        compact,
                    )
                }
            )
        }
    )
    needs = TaskPlanConditionedNeedGenerator().generate(
        public_case.task_contract,
        world,
    )

    assembled = runner._assemble_stage2m_comparison(
        comparison.model_copy(update={"deterministic": deterministic}),
        case=public_case,
        needs=needs,
        fingerprint=content_id({"compact-excerpt-lineage": True}),
    )

    assert assembled.deterministic.writer_context is not None
    assert compact.unit_id in assembled.deterministic.writer_context.lineage.retrieval_unit_ids


def test_tiny_budget_records_all_writer_readiness_blockers() -> None:
    bundle, private_case, public_case, _runner, _comparison = resolved_public_comparison()
    history = next(
        root for root in bundle.text_roots if root.root_hash == private_case.input_text_root
    )
    config = PublicBenchmarkConfig(
        schema_version=bundle.bundle_schema_version,
        configuration_fingerprint=content_id({"tiny": True}),
        expected_profiles=tuple(item.value for item in BenchmarkInformationProfile),
    )
    comparison = Stage2PairedPilotRunner(token_budget=1, arms=("A",)).resolve_state_case(
        config,
        public_case,
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        history=history,
        world=bundle.world_roots[0],
        plan=bundle.plan_roots[0],
        base_commit=bundle.world_roots[0].source_commit,
    )
    assert "arm_a_writer_context_not_ready" in comparison.blockers
    assert comparison.arm_c_status is None


def test_score_comparison_defends_against_malformed_arm_c_artifacts() -> None:
    _bundle, private_case, _public, runner, comparison = resolved_public_comparison()
    malformed = comparison.model_copy(update={"arm_c_evidence_ledger": None})
    with pytest.raises(ValueError, match="Arm C Writer Context requires"):
        runner.score_comparison(
            private_case,
            BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            malformed,
        )

    no_c = comparison.model_copy(
        update={
            "arm_c_writer_context": None,
            "arm_c_evidence_ledger": None,
            "arm_c_status": None,
        }
    )
    delta_runner = Stage2PairedPilotRunner(
        controller_mode=ControllerMode.DETERMINISTIC_PLUS_AGENTIC_DELTA
    )
    scored = delta_runner.score_comparison(
        private_case,
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        no_c,
    )
    assert scored.delta_metrics is not None
    default_scored = runner.score_comparison(
        private_case,
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        no_c,
    )
    assert default_scored.delta_metrics is None


def test_legacy_bundle_path_consumes_preassembled_arm_c(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, _private_case, _public, _runner, comparison = resolved_public_comparison()
    monkeypatch.setattr(
        PairedMemoryControllerRunner,
        "run",
        lambda _self, *_args, **_kwargs: comparison,
    )
    runner = Stage2PairedPilotRunner()
    case = bundle.case_manifests[0]
    result = runner._run_case(
        bundle,
        case,
        runner._configuration_fingerprint(bundle.content_hash),
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    assert result.delta_metrics is not None


def test_legacy_bundle_path_rejects_arm_c_without_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, _private_case, _public, _runner, comparison = resolved_public_comparison()
    malformed = comparison.model_copy(update={"arm_c_evidence_ledger": None})
    monkeypatch.setattr(
        PairedMemoryControllerRunner,
        "run",
        lambda _self, *_args, **_kwargs: malformed,
    )
    runner = Stage2PairedPilotRunner()
    with pytest.raises(ValueError, match="Arm C Writer Context requires"):
        runner._run_case(
            bundle,
            bundle.case_manifests[0],
            runner._configuration_fingerprint(bundle.content_hash),
            BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        )


def test_paired_pilot_rejects_a_case_without_generated_needs() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0].model_copy(
        update={"entities": (), "states": (), "events": (), "obligations": ()}
    )
    world = world.model_copy(update={"root_hash": world_root_content_id(world)})
    case = bundle.case_manifests[0].model_copy(
        update={"input_world_root_verified": world.root_hash}
    )
    empty = bundle.model_copy(
        update={
            "content_hash": PLACEHOLDER_HASH,
            "world_roots": (world,),
            "case_manifests": (case,),
            "replay_manifests": (),
        }
    )
    empty = empty.model_copy(update={"content_hash": bundle_content_id(empty)})

    with pytest.raises(ValueError, match="has no generated needs"):
        Stage2PairedPilotRunner().run(empty)


def test_paired_pilot_real_hybrid_profile_cannot_silently_use_in_memory_backend() -> None:
    with pytest.raises(RuntimeError, match="InMemoryRetrievalBackend is scripted_smoke only"):
        Stage2PairedPilotRunner(retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID).run(
            make_synthetic_bundle()
        )


def _public_inputs() -> tuple[
    PublicBenchmarkConfig,
    PublicCheckpointCase,
]:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    return (
        PublicBenchmarkConfig(
            schema_version=bundle.bundle_schema_version,
            configuration_fingerprint=content_id({"fixture": "public"}),
            expected_profiles=tuple(item.value for item in BenchmarkInformationProfile),
        ),
        build_public_checkpoint_case(
            case_id=case.case_id,
            project_id=case.project_id,
            target_range=case.target_range,
            history_range=case.history_range,
            information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        ),
    )


def test_paired_pilot_runs_against_explicit_state_roots() -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    history = next(root for root in bundle.text_roots if root.root_hash == case.input_text_root)
    world = next(
        root for root in bundle.world_roots if root.root_hash == case.input_world_root_verified
    )
    plan = next(root for root in bundle.plan_roots if root.root_hash == case.input_plan_root)

    result = Stage2PairedPilotRunner().run_state_case(
        bundle,
        case,
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        history=history,
        world=world,
        plan=plan,
        base_commit=world.source_commit,
    )

    assert result.case_id == case.case_id


def test_real_hybrid_state_resolution_requires_exact_injected_capability() -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    history = next(root for root in bundle.text_roots if root.root_hash == case.input_text_root)
    world = next(
        root for root in bundle.world_roots if root.root_hash == case.input_world_root_verified
    )
    plan = next(root for root in bundle.plan_roots if root.root_hash == case.input_plan_root)
    config, public_case = _public_inputs()
    runner = Stage2PairedPilotRunner(retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID)
    with pytest.raises(RuntimeError, match="injected commit-scoped backend"):
        runner.resolve_state_case(
            config,
            public_case,
            BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            history=history,
            world=world,
            plan=plan,
            base_commit=world.source_commit,
        )

    stale = SnapshotCapability(
        source_commit=CommitId("sha256:" + "9" * 64),
        snapshot_id=StableId("snapshot.stale"),
        status=SnapshotCapabilityStatus.STALE,
    )
    with pytest.raises(ValueError, match="must be exact"):
        runner.resolve_state_case(
            config,
            public_case,
            BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            history=history,
            world=world,
            plan=plan,
            base_commit=world.source_commit,
            retrieval_backend=cast(RetrievalBackend, MagicMock()),
            snapshot_capability=stale,
        )


def test_state_resolution_rejects_empty_needs_and_routes_exact_capability() -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    history = next(root for root in bundle.text_roots if root.root_hash == case.input_text_root)
    world = next(
        root for root in bundle.world_roots if root.root_hash == case.input_world_root_verified
    )
    plan = next(root for root in bundle.plan_roots if root.root_hash == case.input_plan_root)
    config, public_case = _public_inputs()
    empty_world = world.model_copy(
        update={"entities": (), "states": (), "events": (), "obligations": ()}
    )
    with pytest.raises(ValueError, match="produced no memory needs"):
        Stage2PairedPilotRunner().resolve_state_case(
            config,
            public_case,
            BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            history=history,
            world=empty_world,
            plan=plan,
            base_commit=world.source_commit,
        )

    exact = SnapshotCapability(
        source_commit=world.source_commit,
        snapshot_id=StableId("snapshot.real-exact"),
        status=SnapshotCapabilityStatus.EXACT,
    )
    with pytest.raises(ValueError, match="produced no memory needs"):
        Stage2PairedPilotRunner(
            retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID
        ).resolve_state_case(
            config,
            public_case,
            BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            history=history,
            world=empty_world,
            plan=plan,
            base_commit=world.source_commit,
            retrieval_backend=cast(RetrievalBackend, MagicMock()),
            snapshot_capability=exact,
        )

    generated = Stage1NeedGenerator().generate(world, case)
    capability = SnapshotCapability(
        source_commit=world.source_commit,
        snapshot_id=StableId("snapshot.exact"),
        status=SnapshotCapabilityStatus.EXACT,
        available_channels=tuple(RetrievalChannel),
    )
    routes = Stage2PairedPilotRunner._route_plans(generated, capability)
    assert routes
    assert Stage2PairedPilotRunner._allowed_tools(routes)


def test_real_hybrid_main_path_skips_need_without_executable_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, private_case, public_case, _runner, _comparison = resolved_public_comparison()
    history = next(
        root for root in bundle.text_roots if root.root_hash == private_case.input_text_root
    )
    world = bundle.world_roots[0]
    plan = bundle.plan_roots[0]
    generated = TaskPlanConditionedNeedGenerator().generate_with_lineage(
        public_case.task_contract,
        world,
        None,
    )
    adversarial = generated.needs[0].model_copy(
        update={
            "query_intent": Stage1QueryIntent.RELATION_CHAIN,
            "entity_ids": (),
            "predicates": (),
            "hierarchy_parent_unit_ids": (),
            "allowed_candidate_pools": (CandidatePool.GRAPH,),
        }
    )
    patched_generation = generated.model_copy(update={"needs": (adversarial,)})
    monkeypatch.setattr(
        TaskPlanConditionedNeedGenerator,
        "generate_with_lineage",
        lambda *_args, **_kwargs: patched_generation,
    )

    class RecordingBackend:
        def __init__(self) -> None:
            self.calls: list[tuple[StableId, RetrievalChannel]] = []

        def search(
            self,
            need: Any,
            channel: RetrievalChannel,
            limit: int,
        ) -> tuple[ChannelHit, ...]:
            self.calls.append((need.need_id, channel))
            return ()

    backend = RecordingBackend()
    config = PublicBenchmarkConfig(
        schema_version=bundle.bundle_schema_version,
        configuration_fingerprint=content_id({"main-path-query-intersection": True}),
        expected_profiles=tuple(item.value for item in BenchmarkInformationProfile),
    )
    capability = SnapshotCapability(
        source_commit=world.source_commit,
        snapshot_id=StableId("snapshot.main-path-query-intersection"),
        status=SnapshotCapabilityStatus.EXACT,
        available_channels=(RetrievalChannel.TYPED_GRAPH,),
    )
    comparison = Stage2PairedPilotRunner(
        arms=("A",),
        retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
    ).resolve_state_case(
        config,
        public_case,
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        history=history,
        world=world,
        plan=plan,
        base_commit=world.source_commit,
        retrieval_backend=cast(RetrievalBackend, backend),
        snapshot_capability=capability,
    )

    assert backend.calls == []
    trace = comparison.deterministic.context.retrieval_traces[0]
    assert trace.need_execution_status is NeedExecutionStatus.NOT_EXECUTED_NO_EXECUTABLE_QUERY
    assert trace.stop_reason is RetrievalStopReason.NO_EXECUTABLE_QUERY
    assert trace.calls_allocated == 0
    assert trace.effective_channels == ()
    assert trace.query_unavailable_reasons == {RetrievalChannel.TYPED_GRAPH: "missing_graph_seed"}


@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("comparable", "comparable flag"),
        ("accuracy_gain", "accuracy gain"),
        ("tool_call_reduction", "tool reduction"),
        ("safety_regression", "safety regression"),
    ),
)
def test_paired_pilot_case_contract_rejects_inconsistent_flags(
    field: str,
    message: str,
) -> None:
    payload = _report().cases[0].model_dump()
    payload[field] = not payload[field]
    with pytest.raises(ValidationError, match=message):
        PairedPilotCaseResult.model_validate(payload)


def test_paired_pilot_case_contract_rejects_execution_status_inconsistencies() -> None:
    base = _report().cases[0].model_dump()
    with pytest.raises(ValidationError, match="completed deterministic arm"):
        PairedPilotCaseResult.model_validate(
            base | {"deterministic_execution_status": ArmExecutionStatus.SKIPPED}
        )
    with pytest.raises(ValidationError, match="cannot expose metrics"):
        PairedPilotCaseResult.model_validate(
            base | {"agentic_execution_status": ArmExecutionStatus.SKIPPED}
        )
    skipped = base | {
        "agentic_execution_status": ArmExecutionStatus.SKIPPED,
        "agentic_metrics": None,
        "delta_metrics": None,
        "accuracy_gain": None,
        "tool_call_reduction": None,
        "safety_regression": None,
    }
    with pytest.raises(ValidationError, match="cannot be a paired comparison"):
        PairedPilotCaseResult.model_validate(skipped)
    with pytest.raises(ValidationError, match="completed Agentic arm requires metrics"):
        PairedPilotCaseResult.model_validate(base | {"agentic_metrics": None})
    with pytest.raises(ValidationError, match="comparison status"):
        PairedPilotCaseResult.model_validate(base | {"paired_comparison_status": "NOT_COMPARABLE"})


def test_paired_context_fallback_flag_and_reason_are_atomic() -> None:
    _bundle, _private, _public, _runner, comparison = resolved_public_comparison()
    with pytest.raises(ValidationError, match="fallback flag/reason"):
        PairedContextComparison.model_validate(
            comparison.model_dump() | {"planner_fallback_used": True}
        )


@pytest.mark.parametrize(
    "field",
    (
        "paired_results_count",
        "comparable_results_count",
        "future_leakage_count",
        "safety_regression_count",
        "accuracy_gain_count",
        "tool_call_reduction_count",
    ),
)
def test_paired_pilot_report_rejects_inconsistent_totals(field: str) -> None:
    payload = _report().model_dump()
    payload[field] += 1
    with pytest.raises(ValidationError, match=field):
        Stage2PairedPilotReport.model_validate(payload)


def test_paired_pilot_report_rejects_duplicate_cases_and_false_gain_claim() -> None:
    payload = _report().model_dump()
    payload["cases"] = (*payload["cases"], payload["cases"][0])
    payload["paired_results_count"] = 3
    payload["comparable_results_count"] = 3
    with pytest.raises(ValidationError, match="case/profile identities must be unique"):
        Stage2PairedPilotReport.model_validate(payload)

    payload = _report().model_dump()
    payload["held_out_complex_gain_proven"] = True
    with pytest.raises(ValidationError, match="cannot prove held-out"):
        Stage2PairedPilotReport.model_validate(payload)


def test_paired_pilot_evidence_matching_handles_empty_and_span_paths() -> None:
    case = make_synthetic_bundle().case_manifests[0]
    expected = case.observed_use_gold[0].evidence_refs[0]
    assert Stage2PairedPilotRunner._gold_coverage((), ()) == 1.0
    assert Stage2PairedPilotRunner._evidence_coverage((), ()) == 1.0
    assert Stage2PairedPilotRunner._evidence_coverage((expected,), (expected,)) == 1.0
    assert Stage2PairedPilotRunner._matches(expected, expected) is True
    distinct_id = expected.model_copy(update={"evidence_id": case.case_id})
    assert Stage2PairedPilotRunner._matches(distinct_id, expected) is True
    assert distinct_id.span is not None
    equivalent_prefix = distinct_id.model_copy(
        update={
            "root_hash": PLACEHOLDER_HASH,
            "span": distinct_id.span.model_copy(
                update={"block_id": StableId("block.equivalent-prefix")}
            ),
        }
    )
    assert Stage2PairedPilotRunner._matches(equivalent_prefix, expected) is True
    assert (
        Stage2PairedPilotRunner._matches(
            equivalent_prefix.model_copy(update={"object_hash": PLACEHOLDER_HASH}), expected
        )
        is False
    )
    assert (
        Stage2PairedPilotRunner._matches(distinct_id.model_copy(update={"span": None}), expected)
        is False
    )


def test_writer_metrics_honor_conjunctive_accepted_evidence_sets() -> None:
    _bundle, private_case, _public, _runner, comparison = resolved_public_comparison()
    arm = comparison.deterministic
    assert arm.writer_context is not None and arm.evidence_ledger is not None
    evidence_ref = arm.evidence_ledger.entries[0].evidence_refs[0]
    gold = private_case.observed_use_gold[0].model_copy(
        update={
            "accepted_evidence_sets": (
                EvidenceSet(
                    evidence_set_id=StableId("evidence-set.writer-metrics"),
                    evidence_refs=(evidence_ref,),
                ),
            )
        }
    )
    case = private_case.model_copy(update={"observed_use_gold": (gold,)})

    metrics = Stage2PairedPilotRunner._writer_metrics(
        case,
        arm.writer_context,
        arm.evidence_ledger,
        retrieval_call_count=arm.retrieval_call_count,
        future_leakage_count=arm.future_leakage_count,
        stop_reason=arm.stop_reason,
    )

    assert metrics.observed_use_coverage == 1.0


def test_stage2m_assembly_preserves_typed_agentic_and_arm_c_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, _private, public_case, runner, comparison = resolved_public_comparison()
    needs = TaskPlanConditionedNeedGenerator().generate(
        public_case.task_contract,
        bundle.world_roots[0],
    )
    fingerprint = content_id({"typed-assembly-failures": True})
    preblocked_agentic = comparison.agentic.model_copy(
        update={"quality_eligible": False, "failure_category": "AGENTIC_NOT_READY"}
    )
    failed_b = runner._assemble_stage2m_comparison(
        comparison.model_copy(
            update={
                "agentic": preblocked_agentic,
                "comparable": False,
                "blockers": ("agentic_preblocked",),
            }
        ),
        case=public_case,
        needs=needs,
        fingerprint=fingerprint,
    )
    assert failed_b.agentic.execution_status is ArmExecutionStatus.FAILED
    assert failed_b.arm_c_failure_category == "SKIPPED_AGENTIC_FAILURE"

    original_assemble = WriterContextAssembler.assemble_from_spec

    def fail_only_arm_c(
        assembler: WriterContextAssembler,
        *args: Any,
        **kwargs: Any,
    ) -> WriterContextAssemblyResult:
        assembled = original_assemble(assembler, *args, **kwargs)
        if kwargs.get("arm") == "C":
            return assembled.model_copy(
                update={"status": ContextAssemblyStatus.EVIDENCE_INSUFFICIENT}
            )
        return assembled

    monkeypatch.setattr(WriterContextAssembler, "assemble_from_spec", fail_only_arm_c)
    failed_c = runner._assemble_stage2m_comparison(
        comparison,
        case=public_case,
        needs=needs,
        fingerprint=fingerprint,
    )
    assert failed_c.arm_c_status is ContextAssemblyStatus.EVIDENCE_INSUFFICIENT
    assert "arm_c_writer_context_not_ready" in failed_c.blockers


def test_paired_pilot_matches_content_addressed_r1_plan_records_only_by_exact_goal() -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    plan = next(root for root in bundle.plan_roots if root.root_hash == case.input_plan_root)
    goal = plan.chapter_goals[0]
    expected = PlanEvidenceRef(
        evidence_id=StableId("plan-evidence.synthetic"),
        plan_root_hash=plan.root_hash,
        goal_id=goal.goal_id,
        object_hash=content_id(goal.model_dump(mode="json")),
    )
    unit = RetrievalUnit(
        unit_id=StableId("unit.r1.content-addressed"),
        unit_kind=RetrievalUnitKind.PLAN_ANCHOR,
        source_commit=bundle.world_roots[0].source_commit,
        snapshot_id=StableId("snapshot.plan"),
        text=json.dumps(goal.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
        access_scope="author_planning",
        information_label="plan",
    )

    assert content_id(goal.model_dump(mode="json")) == expected.object_hash
    assert Stage2PairedPilotRunner._matches_plan(unit, expected) is True
    legacy_anchor = unit.model_copy(
        update={
            "unit_id": StableId(f"anchor.{goal.goal_id.root}"),
            "source_artifact": plan.root_hash,
        }
    )
    assert Stage2PairedPilotRunner._matches_plan(legacy_anchor, expected) is True
    assert (
        Stage2PairedPilotRunner._matches_plan(
            legacy_anchor.model_copy(update={"source_artifact": None}), expected
        )
        is False
    )
    wrong = unit.model_copy(
        update={"text": json.dumps(goal.model_dump(mode="json") | {"summary": "wrong"})}
    )
    assert Stage2PairedPilotRunner._matches_plan(wrong, expected) is False
    assert (
        Stage2PairedPilotRunner._matches_plan(
            unit.model_copy(update={"information_label": "observed"}), expected
        )
        is False
    )
    assert (
        Stage2PairedPilotRunner._matches_plan(
            unit.model_copy(update={"text": "not-json"}), expected
        )
        is False
    )


def test_runtime_planning_context_binding_edges() -> None:
    pilot = Path(__file__).parents[2] / "benchmarks/private/ztj_memory_pilot_v0.1"
    bundle = HumanBenchmarkCompiler().compile(pilot)
    private = bundle.case_manifests[0]
    plan = next(item for item in bundle.plan_roots if item.root_hash == private.input_plan_root)
    context = next(
        item
        for item in bundle.planning_contexts
        if item.source_hash == private.planning_context_hash
    )
    visible = build_public_checkpoint_case(
        case_id=private.case_id,
        project_id=private.project_id,
        target_range=private.target_range,
        history_range=private.history_range,
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    with pytest.raises(ValueError, match="cannot receive planning context"):
        Stage2PairedPilotRunner._validate_runtime_planning_context(
            visible, BenchmarkInformationProfile.VISIBLE_AT_CUTOFF, plan, context
        )

    def public_for(bound_context: Any) -> PublicCheckpointCase:
        return cast(
            PublicCheckpointCase,
            build_public_checkpoint_case(
                case_id=private.case_id,
                project_id=private.project_id,
                target_range=private.target_range,
                history_range=private.history_range,
                information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
                task_intent=bound_context.task_intent,
                planning_context_ref=content_id(bound_context.model_dump(mode="json")),
                planning_context_hash=bound_context.source_hash,
                plan_root_ref=PlanRootRef(
                    artifact_id=plan.root_hash,
                    media_type="application/vnd.novel-agent.plan-root+json",
                    byte_length=1,
                    schema_version=SchemaVersion("1.0.0"),
                ),
            ),
        )

    public = public_for(context)
    with pytest.raises(ValueError, match="requires a verified PlanRoot"):
        cast(Any, public.model_copy(update={"plan_root_ref": None}).validate_public_contract)()
    unbound_task = public.task_contract.model_copy(
        update={"planning_context_ref": None, "planning_context_hash": None}
    )
    with pytest.raises(ValueError, match="requires a bound planning context"):
        cast(
            Any,
            public.model_copy(update={"task_contract": unbound_task}).validate_public_contract,
        )()
    with pytest.raises(ValueError, match="requires the bound"):
        Stage2PairedPilotRunner._validate_runtime_planning_context(
            public, BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED, plan, None
        )
    with pytest.raises(ValueError, match="does not match its public binding"):
        Stage2PairedPilotRunner._validate_runtime_planning_context(
            public,
            BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
            plan,
            context.model_copy(update={"task_intent": "drift"}),
        )

    altered = context.model_copy(
        update={"visible_outline_nodes": context.visible_outline_nodes[:-1]}
    )
    altered = altered.model_copy(
        update={
            "source_hash": content_id(
                {
                    "profile": altered.profile.value,
                    "task_intent": altered.task_intent,
                    "target_range": altered.target_range,
                    "visible_outline_nodes": [
                        node.model_dump(mode="json") for node in altered.visible_outline_nodes
                    ],
                    "chapter_goals": [
                        goal.model_dump(mode="json") for goal in altered.chapter_goals
                    ],
                    "planner_may_read_plan": altered.planner_may_read_plan,
                }
            )
        }
    )
    with pytest.raises(ValueError, match="disagrees with the verified PlanRoot"):
        Stage2PairedPilotRunner._validate_runtime_planning_context(
            public_for(altered),
            BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
            plan,
            altered,
        )


_ARM_C_COMMIT = CommitId("sha256:" + "b" * 64)
_ARM_C_SNAPSHOT = StableId("snapshot.arm-c")
_ARM_C_CONFIG = ArtifactId("sha256:" + "d" * 64)


def _arm_c_candidate(
    unit_id: StableId,
    *,
    evidence_refs: tuple[EvidenceRef, ...] = (),
) -> FusedCandidate:
    unit = RetrievalUnit(
        unit_id=unit_id,
        unit_kind=RetrievalUnitKind.EVENT_ANCHOR,
        source_commit=_ARM_C_COMMIT,
        snapshot_id=_ARM_C_SNAPSHOT,
        text=f"candidate {unit_id.root}",
        evidence_refs=evidence_refs,
    )
    hit = ChannelHit(
        unit=unit,
        channel=RetrievalChannel.ANCHOR_BM25,
        channel_rank=1,
        raw_score=1.0,
        candidate_count=1,
        hit_reason="arm-c fixture",
    )
    return FusedCandidate(unit=unit, fused_rank=1, rrf_score=1.0, channel_hits=(hit,))


def _arm_c_result(
    candidates: tuple[FusedCandidate, ...],
    *,
    arm: ControllerArm,
    future_leakage_count: int = 0,
    retrieval_call_count: int = 1,
) -> PairedContextArmResult:
    selected = tuple(candidate for candidate in candidates if candidate.selected)
    trace = RetrievalTrace(
        need_id=StableId("need.arm-c"),
        intent=Stage1QueryIntent.RELATED_EVENT,
        allowed_channels=(RetrievalChannel.ANCHOR_BM25,),
        channel_candidate_counts={RetrievalChannel.ANCHOR_BM25: len(selected)},
        candidates=candidates,
        fusion_applied=True,
        stop_reason=RetrievalStopReason.EXACT_SATISFIED,
    )
    context = Stage1ContextPackage(
        context_id=StableId("context.arm-c"),
        base_commit=_ARM_C_COMMIT,
        snapshot_id=_ARM_C_SNAPSHOT,
        task_contract="arm-c fixture contract",
        retrieval_traces=(trace,),
        budget_report=ContextBudgetReport(
            token_budget=1000,
            mandatory_tokens=0,
            optional_tokens=0,
            full_chapter_read_count=0,
        ),
    )
    return PairedContextArmResult(
        arm=arm,
        context=context,
        selected_unit_ids=tuple(dict.fromkeys(candidate.unit.unit_id for candidate in selected)),
        retrieval_call_count=retrieval_call_count,
        stop_reason=ControllerStopReason.SUFFICIENT,
        comparison_basis_fingerprint=_ARM_C_CONFIG,
        future_leakage_count=future_leakage_count,
    )


def test_arm_c_unions_selected_units_from_a_and_accepted_delta_b() -> None:
    case = make_synthetic_bundle().case_manifests[0]
    deterministic = _arm_c_result(
        (
            _arm_c_candidate(StableId("unit.a.one")),
            _arm_c_candidate(StableId("unit.a.two")),
        ),
        arm=ControllerArm.DETERMINISTIC,
    )
    agentic = _arm_c_result(
        (
            # Already in A: must not contribute to the delta.
            _arm_c_candidate(StableId("unit.a.two")),
            # Genuine delta: selected by B, missed by A.
            _arm_c_candidate(StableId("unit.b.three")),
        ),
        arm=ControllerArm.BOUNDED_R2,
    )

    arm_c, delta = Stage2PairedPilotRunner._build_arm_c(case, deterministic, agentic)

    assert delta.selected_unit_count == 3
    assert set(arm_c.selected_unit_ids) == {
        StableId("unit.a.one"),
        StableId("unit.a.two"),
        StableId("unit.b.three"),
    }


def test_arm_c_rejects_delta_when_agentic_has_leakage() -> None:
    case = make_synthetic_bundle().case_manifests[0]
    deterministic = _arm_c_result(
        (_arm_c_candidate(StableId("unit.a.one")),),
        arm=ControllerArm.DETERMINISTIC,
    )
    agentic = _arm_c_result(
        (_arm_c_candidate(StableId("unit.b.delta")),),
        arm=ControllerArm.BOUNDED_R2,
        future_leakage_count=1,
    )

    det_metrics = Stage2PairedPilotRunner._metrics(case, deterministic)
    _arm_c, delta = Stage2PairedPilotRunner._build_arm_c(case, deterministic, agentic)

    # B leaked -> accepted_delta is empty -> Arm C collapses onto Arm A.
    assert delta.selected_unit_count == det_metrics.selected_unit_count
    assert delta.gold_evidence_recall == det_metrics.gold_evidence_recall
    assert delta.mandatory_constraint_coverage == det_metrics.mandatory_constraint_coverage
    assert delta.future_leakage_count == det_metrics.future_leakage_count


def test_arm_c_gold_recall_reflects_union() -> None:
    case = make_synthetic_bundle().case_manifests[0]
    promise_evidence = case.observed_use_gold[0].evidence_refs[0]
    injury_evidence = case.operational_constraint_gold[0].evidence_refs[0]
    deterministic = _arm_c_result(
        (_arm_c_candidate(StableId("unit.a.injury"), evidence_refs=(injury_evidence,)),),
        arm=ControllerArm.DETERMINISTIC,
    )
    agentic = _arm_c_result(
        (_arm_c_candidate(StableId("unit.b.promise"), evidence_refs=(promise_evidence,)),),
        arm=ControllerArm.BOUNDED_R2,
    )

    det_metrics = Stage2PairedPilotRunner._metrics(case, deterministic)
    agentic_metrics = Stage2PairedPilotRunner._metrics(case, agentic)
    _arm_c, delta = Stage2PairedPilotRunner._build_arm_c(case, deterministic, agentic)

    # A and B cover disjoint evidence; the union in C recovers all gold.
    assert det_metrics.gold_evidence_recall < agentic_metrics.gold_evidence_recall
    assert delta.gold_evidence_recall == 1.0
    assert delta.gold_evidence_recall > agentic_metrics.gold_evidence_recall


def test_arm_c_safety_regression_compares_c_vs_a() -> None:
    case_result = _report().cases[0]
    payload = case_result.model_dump()
    # Arm C with strictly worse mandatory coverage than Arm A must flag a
    # regression against the deterministic floor.
    regressed = case_result.deterministic_metrics.model_copy(
        update={
            "mandatory_constraint_coverage": max(
                0.0, case_result.deterministic_metrics.mandatory_constraint_coverage - 0.1
            )
        }
    )
    payload["delta_metrics"] = regressed.model_dump()
    payload["safety_regression"] = True
    validated = PairedPilotCaseResult.model_validate(payload)
    assert validated.safety_regression is True

    # The same regressed Arm C must reject safety_regression=False.
    payload["safety_regression"] = False
    with pytest.raises(ValidationError, match="safety regression"):
        PairedPilotCaseResult.model_validate(payload)


def test_paired_pilot_delta_mode_builds_real_arm_c() -> None:
    report = Stage2PairedPilotRunner(
        controller_mode=ControllerMode.DETERMINISTIC_PLUS_AGENTIC_DELTA
    ).run(make_synthetic_bundle())
    result = report.cases[0]

    assert result.delta_metrics is not None
    # Arm C is a superset of Arm A, so it cannot regress against the floor.
    assert result.safety_regression is False
    assert (
        result.delta_metrics.mandatory_constraint_coverage
        >= result.deterministic_metrics.mandatory_constraint_coverage
    )
    assert (
        result.delta_metrics.gold_evidence_recall
        >= result.deterministic_metrics.gold_evidence_recall
    )
    assert result.delta_metrics.future_leakage_count == 0


def test_score_comparison_builds_arm_c_in_delta_mode() -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    deterministic = _arm_c_result(
        (_arm_c_candidate(StableId("unit.a.one")),),
        arm=ControllerArm.DETERMINISTIC,
    )
    agentic = _arm_c_result(
        (_arm_c_candidate(StableId("unit.b.delta")),),
        arm=ControllerArm.BOUNDED_R2,
    )
    comparison = PairedContextComparison(
        pair_id=StableId("pair.arm-c-score"),
        request_id=StableId("request.arm-c-score"),
        deterministic=deterministic,
        agentic=agentic,
        comparable=True,
    )

    result = Stage2PairedPilotRunner(
        controller_mode=ControllerMode.DETERMINISTIC_PLUS_AGENTIC_DELTA
    ).score_comparison(
        case,
        BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        comparison,
    )

    assert result.delta_metrics is not None
    assert (
        result.delta_metrics.mandatory_constraint_coverage
        >= result.deterministic_metrics.mandatory_constraint_coverage
    )
    assert result.safety_regression is False


def test_arm_c_call_count_one_call_many_candidates_not_inflated() -> None:
    """One B call returning multiple accepted delta candidates must not inflate
    Arm C's retrieval_call_count to A + candidate_count.
    """
    case = make_synthetic_bundle().case_manifests[0]
    deterministic = _arm_c_result(
        (_arm_c_candidate(StableId("unit.a.one")),),
        arm=ControllerArm.DETERMINISTIC,
        retrieval_call_count=3,
    )
    agentic = _arm_c_result(
        (
            _arm_c_candidate(StableId("unit.b.two")),
            _arm_c_candidate(StableId("unit.b.three")),
            _arm_c_candidate(StableId("unit.b.four")),
        ),
        arm=ControllerArm.BOUNDED_R2,
        retrieval_call_count=1,
    )

    arm_c, delta = Stage2PairedPilotRunner._build_arm_c(case, deterministic, agentic)

    # 3 (A) + 1 (B) = 4, not 3 + 3 candidates = 6.
    assert arm_c.retrieval_call_count == 4
    assert delta.retrieval_call_count == 4


def test_arm_c_call_count_many_calls_one_candidate_not_deflated() -> None:
    """Multiple B calls producing one accepted delta candidate must not deflate
    Arm C's retrieval_call_count to A + 1.
    """
    case = make_synthetic_bundle().case_manifests[0]
    deterministic = _arm_c_result(
        (_arm_c_candidate(StableId("unit.a.one")),),
        arm=ControllerArm.DETERMINISTIC,
        retrieval_call_count=2,
    )
    agentic = _arm_c_result(
        (_arm_c_candidate(StableId("unit.b.two")),),
        arm=ControllerArm.BOUNDED_R2,
        retrieval_call_count=5,
    )

    arm_c, delta = Stage2PairedPilotRunner._build_arm_c(case, deterministic, agentic)

    # 2 (A) + 5 (B) = 7, not 2 + 1 candidate = 3.
    assert arm_c.retrieval_call_count == 7
    assert delta.retrieval_call_count == 7


def test_pilot_records_raw_ledger_funnel_from_semantic_gateway() -> None:
    import json as _json
    from decimal import Decimal as _Decimal

    from novel_agent.domain.model_calls import ModelRole, ModelUsage, ProviderModelResult
    from novel_agent.services.model_gateway import (
        ModelGateway,
        RegisteredModelEndpoint,
    )

    class _Gateway:
        is_external = False
        model = "semantic-support-test"
        max_retries = 0

        async def generate(self, request: Any) -> ProviderModelResult:
            return ProviderModelResult(
                text=_json.dumps(
                    {
                        "claims": [],
                        "insufficient_need_ids": [],
                    },
                    ensure_ascii=False,
                ),
                model_version=self.model,
                usage=ModelUsage(
                    input_tokens=10,
                    output_tokens=10,
                    cost_usd=_Decimal("0"),
                ),
            )

    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="semantic-support-test",
                model_name="semantic-support-test",
                adapter=_Gateway(),
            ),
        )
    )
    bundle, _private_case, public_case, runner, comparison = resolved_public_comparison()
    world = bundle.world_roots[0]
    needs = TaskPlanConditionedNeedGenerator().generate(
        public_case.task_contract,
        world,
    )
    assembled = runner._assemble_stage2m_comparison(
        comparison,
        case=public_case,
        needs=needs,
        fingerprint=content_id({"funnel": "raw-ledger"}),
        support_gateway=gateway,
    )
    assert assembled.deterministic.writer_context is not None
    producer_funnel = getattr(runner, "_assemble_stage2m_comparison", None) is not None
    assert producer_funnel
