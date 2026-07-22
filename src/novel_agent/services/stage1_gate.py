"""Formal Stage 1 gate evaluation; synthetic fixtures can never self-promote."""

from __future__ import annotations

from collections import Counter

from novel_agent.domain.benchmark import (
    BenchmarkBundle,
    FailureCategory,
    ReplayGateEvidence,
    Stage1BenchmarkResult,
    Stage1GateReport,
    Stage1GateVerdict,
)


class Stage1GateEvaluator:
    def evaluate(
        self,
        bundle: BenchmarkBundle,
        read_results: tuple[Stage1BenchmarkResult, ...],
        replay_evidence: tuple[ReplayGateEvidence, ...],
    ) -> Stage1GateReport:
        eligible_read = tuple(case for case in bundle.case_manifests if case.gate_eligible)
        eligible_replay = tuple(case for case in bundle.replay_manifests if case.gate_eligible)
        expected_read = {
            (case.case_id, track) for case in eligible_read for track in case.expected_tracks
        }
        result_by_key = {(result.case_id, result.track): result for result in read_results}
        replay_by_id = {item.replay_case_id: item for item in replay_evidence}
        blockers: list[str] = []
        checks: dict[str, bool] = {}
        if not eligible_read or not eligible_replay:
            blockers.append(
                "formal gate requires gate-eligible read cases and at least one 50-chapter replay"
            )
            verdict = Stage1GateVerdict.NOT_ELIGIBLE
        else:
            missing_read = expected_read - set(result_by_key)
            missing_replay = {case.replay_case_id for case in eligible_replay} - set(replay_by_id)
            if missing_read:
                blockers.append(f"missing {len(missing_read)} required read track result(s)")
            if missing_replay:
                blockers.append(f"missing {len(missing_replay)} required replay result(s)")
            for key in sorted(expected_read, key=lambda item: (item[0].root, item[1].value)):
                result = result_by_key.get(key)
                if result is None:
                    continue
                profile = next(
                    (item for item in result.profile_results if item.profile == "K4-memory-kernel"),
                    None,
                )
                prefix = f"read.{key[0].root}.{key[1].value}"
                if profile is None:
                    blockers.append(f"{prefix} has no K4 profile")
                    continue
                metrics = profile.metrics
                baselines = tuple(
                    item
                    for item in result.profile_results
                    if item.profile in {"B0-recent-3", "B1-recent-3+chapter-summary"}
                )
                if len(baselines) != 2:
                    blockers.append(f"{prefix} lacks required B0/B1 baselines")
                else:
                    checks[f"{prefix}.baseline_improvement"] = metrics.gold_evidence_recall > max(
                        item.metrics.gold_evidence_recall for item in baselines
                    ) or metrics.context_utility_per_1k_tokens > max(
                        item.metrics.context_utility_per_1k_tokens for item in baselines
                    )
                checks[f"{prefix}.current_state_accuracy"] = (
                    metrics.current_state_accuracy is not None
                    and metrics.current_state_accuracy >= 0.95
                )
                checks[f"{prefix}.gold_evidence_recall"] = metrics.gold_evidence_recall >= 0.90
                checks[f"{prefix}.mandatory_coverage"] = (
                    metrics.mandatory_constraint_coverage == 1.0
                )
                checks[f"{prefix}.operational_coverage"] = (
                    metrics.operational_constraint_coverage >= 0.95
                )
                checks[f"{prefix}.future_leakage"] = metrics.future_leakage_rate == 0.0
                checks[f"{prefix}.traceability"] = metrics.evidence_traceability == 1.0
            for case in eligible_replay:
                evidence = replay_by_id.get(case.replay_case_id)
                if evidence is None:
                    continue
                prefix = f"replay.{case.replay_case_id.root}"
                checks[f"{prefix}.length"] = evidence.replayed_chapters >= 50
                checks[f"{prefix}.state_f1"] = evidence.metrics.state_delta_f1 >= 0.85
                checks[f"{prefix}.false_promotion"] = (
                    evidence.metrics.false_world_fact_promotion_rate <= 0.01
                )
                checks[f"{prefix}.evidence_binding"] = (
                    evidence.metrics.evidence_binding_accuracy >= 0.99
                )
                checks[f"{prefix}.silent_canonical_pollution"] = (
                    evidence.silent_canonical_pollution_count == 0
                )
                checks[f"{prefix}.silent_stale_snapshot"] = (
                    evidence.silent_stale_snapshot_reads == 0
                )
            if blockers:
                verdict = Stage1GateVerdict.INCOMPLETE
            elif checks and all(checks.values()):
                verdict = Stage1GateVerdict.PASS
            else:
                critical_suffixes = (
                    ".mandatory_coverage",
                    ".future_leakage",
                    ".false_promotion",
                    ".evidence_binding",
                    ".silent_canonical_pollution",
                    ".silent_stale_snapshot",
                )
                critical_failed = any(
                    not passed and name.endswith(critical_suffixes)
                    for name, passed in checks.items()
                )
                verdict = (
                    Stage1GateVerdict.FAIL
                    if critical_failed
                    else Stage1GateVerdict.CONDITIONAL_PASS
                )
        failure_counts: Counter[FailureCategory] = Counter(
            category
            for result in read_results
            for profile in result.profile_results
            for category in profile.failure_categories
        )
        return Stage1GateReport(
            bundle_id=bundle.bundle_id,
            verdict=verdict,
            read_results_expected=len(expected_read),
            read_results_present=len(expected_read.intersection(result_by_key)),
            replay_cases_expected=len(eligible_replay),
            replay_cases_present=len(
                {case.replay_case_id for case in eligible_replay}.intersection(replay_by_id)
            ),
            checks=checks,
            failure_counts=dict(failure_counts),
            blockers=tuple(blockers),
        )
