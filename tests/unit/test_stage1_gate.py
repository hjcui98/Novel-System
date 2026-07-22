from __future__ import annotations

from typing import Any

from novel_agent.domain.benchmark import (
    BenchmarkBundle,
    BenchmarkTrack,
    FailureCategory,
    ReplayGateEvidence,
    ReplayMetricSet,
    Stage1BenchmarkResult,
    Stage1GateVerdict,
)
from novel_agent.services.stage1_benchmark import Stage1BenchmarkRunner
from novel_agent.services.stage1_gate import Stage1GateEvaluator
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


def _eligible_bundle() -> BenchmarkBundle:
    bundle = make_synthetic_bundle()
    replay = bundle.replay_manifests[0].model_copy(
        update={"chapter_range": (1, 50), "gate_eligible": True}
    )
    return bundle.model_copy(update={"replay_manifests": (replay,)})


def _replay_evidence(bundle: BenchmarkBundle, *, state_f1: float = 1.0) -> ReplayGateEvidence:
    return ReplayGateEvidence(
        replay_case_id=bundle.replay_manifests[0].replay_case_id,
        metrics=ReplayMetricSet(
            state_delta_precision=1.0,
            state_delta_recall=1.0,
            state_delta_f1=state_f1,
            event_extraction_f1=1.0,
            relation_delta_f1=1.0,
            plan_obligation_update_f1=1.0,
            wrong_target_binding_rate=0.0,
            false_world_fact_promotion_rate=0.0,
            missed_critical_change_rate=0.0,
            evidence_binding_accuracy=1.0,
            commit_reject_rate=0.0,
        ),
        replayed_chapters=50,
        silent_canonical_pollution_count=0,
        silent_stale_snapshot_reads=0,
    )


def _read_results(
    bundle: BenchmarkBundle,
) -> tuple[Stage1BenchmarkResult, Stage1BenchmarkResult]:
    original = make_synthetic_bundle()
    case = original.case_manifests[0]
    oracle = Stage1BenchmarkRunner().run(original, case.case_id, BenchmarkTrack.ORACLE)
    return (
        oracle.model_copy(update={"bundle_id": bundle.bundle_id}),
        oracle.model_copy(
            update={"bundle_id": bundle.bundle_id, "track": BenchmarkTrack.END_TO_END}
        ),
    )


def _replace_k4_metric(result: Stage1BenchmarkResult, **updates: Any) -> Stage1BenchmarkResult:
    profiles = tuple(
        profile.model_copy(update={"metrics": profile.metrics.model_copy(update=updates)})
        if profile.profile == "K4-memory-kernel"
        else profile
        for profile in result.profile_results
    )
    return result.model_copy(update={"profile_results": profiles})


def test_synthetic_bundle_is_never_formally_gate_eligible() -> None:
    bundle = make_synthetic_bundle()
    report = Stage1GateEvaluator().evaluate(bundle, (), ())
    assert report.verdict is Stage1GateVerdict.NOT_ELIGIBLE
    assert report.blockers


def test_formal_gate_is_incomplete_until_every_track_and_replay_are_present() -> None:
    bundle = _eligible_bundle()
    report = Stage1GateEvaluator().evaluate(bundle, (), ())
    assert report.verdict is Stage1GateVerdict.INCOMPLETE
    assert report.read_results_expected == 2
    assert report.read_results_present == 0
    assert report.replay_cases_expected == 1
    assert report.replay_cases_present == 0
    assert len(report.blockers) == 2


def test_formal_gate_pass_conditional_and_fail_outcomes_are_distinct() -> None:
    bundle = _eligible_bundle()
    reads = _read_results(bundle)
    replay = _replay_evidence(bundle)
    passed = Stage1GateEvaluator().evaluate(bundle, reads, (replay,))
    assert passed.verdict is Stage1GateVerdict.PASS
    assert passed.checks and all(passed.checks.values())
    assert passed.failure_counts[FailureCategory.RETRIEVE] > 0

    conditional_reads = (
        _replace_k4_metric(reads[0], current_state_accuracy=0.50),
        reads[1],
    )
    conditional = Stage1GateEvaluator().evaluate(bundle, conditional_reads, (replay,))
    assert conditional.verdict is Stage1GateVerdict.CONDITIONAL_PASS

    failed_reads = (
        _replace_k4_metric(reads[0], mandatory_constraint_coverage=0.50),
        reads[1],
    )
    failed = Stage1GateEvaluator().evaluate(bundle, failed_reads, (replay,))
    assert failed.verdict is Stage1GateVerdict.FAIL


def test_missing_k4_profile_is_reported_as_incomplete() -> None:
    bundle = _eligible_bundle()
    reads = _read_results(bundle)
    first = reads[0].model_copy(
        update={
            "profile_results": tuple(
                item for item in reads[0].profile_results if item.profile != "K4-memory-kernel"
            )
        }
    )
    report = Stage1GateEvaluator().evaluate(bundle, (first, reads[1]), (_replay_evidence(bundle),))
    assert report.verdict is Stage1GateVerdict.INCOMPLETE
    assert "has no K4 profile" in report.blockers[0]

    missing_baseline = reads[0].model_copy(
        update={
            "profile_results": tuple(
                item for item in reads[0].profile_results if item.profile != "B0-recent-3"
            )
        }
    )
    baseline_report = Stage1GateEvaluator().evaluate(
        bundle, (missing_baseline, reads[1]), (_replay_evidence(bundle),)
    )
    assert baseline_report.verdict is Stage1GateVerdict.INCOMPLETE
    assert any("lacks required B0/B1" in item for item in baseline_report.blockers)
