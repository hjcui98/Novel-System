from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from novel_agent.domain.benchmark import PlanEvidenceRef
from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import RetrievalUnit, RetrievalUnitKind
from novel_agent.domain.retrieval_routing import RetrievalBackendProfile
from novel_agent.domain.stage2 import PairedPilotCaseResult, Stage2PairedPilotReport
from novel_agent.services.benchmark_importer import (
    bundle_content_id,
    content_id,
    world_root_content_id,
)
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
    assert report.tool_call_reduction_count == 1
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
