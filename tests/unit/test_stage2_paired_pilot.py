from __future__ import annotations

import json
from typing import cast
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from novel_agent.domain.benchmark import PlanEvidenceRef
from novel_agent.domain.ids import ArtifactId, CommitId, StableId
from novel_agent.domain.memory import (
    ChannelHit,
    ContextBudgetReport,
    FusedCandidate,
    RetrievalChannel,
    RetrievalStopReason,
    RetrievalTrace,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1ContextPackage,
    Stage1QueryIntent,
)
from novel_agent.domain.retrieval_routing import (
    RetrievalBackendProfile,
    SnapshotCapability,
    SnapshotCapabilityStatus,
)
from novel_agent.domain.stage2 import (
    BenchmarkInformationProfile,
    ControllerArm,
    ControllerMode,
    ControllerStopReason,
    PairedContextArmResult,
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
from novel_agent.services.retrieval import RetrievalBackend
from novel_agent.services.stage1_benchmark import Stage1NeedGenerator
from novel_agent.services.stage2_paired_pilot import Stage2PairedPilotRunner
from tests.fixtures.stage1_synthetic import PLACEHOLDER_HASH, make_synthetic_bundle


def _report() -> Stage2PairedPilotReport:
    return Stage2PairedPilotRunner().run(make_synthetic_bundle())


def test_paired_pilot_runs_both_arms_on_one_audited_basis() -> None:
    report = _report()

    assert report.paired_results_count == 2
    assert report.comparable_results_count == 2
    assert report.future_leakage_count == 0
    assert report.safety_regression_count == 0
    assert report.accuracy_gain_count == 0
    assert report.tool_call_reduction_count == 2
    assert report.held_out_complex_gain_proven is False
    result = report.cases[0]
    assert result.checkpoint_chapter == 20
    assert result.comparison_basis_fingerprint == report.configuration_fingerprint
    assert result.deterministic_metrics.gold_evidence_recall == 1.0
    assert result.agentic_metrics.gold_evidence_recall == 1.0
    assert result.deterministic_metrics.evidence_traceability == 1.0
    assert Stage2PairedPilotRunner().run(make_synthetic_bundle()) == report


@pytest.mark.parametrize(("token_budget", "max_candidates"), ((0, 20), (4000, 0), (4000, 101)))
def test_paired_pilot_rejects_invalid_limits(token_budget: int, max_candidates: int) -> None:
    with pytest.raises(ValueError, match="paired Pilot"):
        Stage2PairedPilotRunner(
            token_budget=token_budget,
            max_candidates=max_candidates,
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
        PublicCheckpointCase(
            case_id=case.case_id,
            project_id=case.project_id,
            target_range=case.target_range,
            history_range=case.history_range,
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
        selected_unit_ids=tuple(
            dict.fromkeys(candidate.unit.unit_id for candidate in selected)
        ),
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
        (
            _arm_c_candidate(
                StableId("unit.a.injury"), evidence_refs=(injury_evidence,)
            ),
        ),
        arm=ControllerArm.DETERMINISTIC,
    )
    agentic = _arm_c_result(
        (
            _arm_c_candidate(
                StableId("unit.b.promise"), evidence_refs=(promise_evidence,)
            ),
        ),
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
