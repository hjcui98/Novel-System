"""Freeze-first seven-mode Stage 4 evaluation and ablation reporting."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from pydantic import JsonValue, ValidationError

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import SchemaVersion, StableId
from novel_agent.domain.model_calls import ModelCallLedgerAggregate
from novel_agent.domain.planning import (
    PlanningEvaluationCase,
    PlanningEvaluationManifest,
    PlanningEvaluationMetric,
    PlanningEvaluationObservation,
    PlanningEvaluationProfile,
    PlanningEvaluationReport,
    PlanningEvaluationRubric,
    PlanningEvaluationThresholds,
    PlanningLoopTerminal,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.model_call_ledger import ModelCallLedgerPort, aggregate_model_calls


class PlanningEvaluationArm(StrEnum):
    CONFIGURED = "configured"
    AUTHOR_CURRENT_PLAN_ONLY = "author_current_plan_only"
    EXACT_TEMPORAL_ONLY = "exact_temporal_only"
    ANCHOR_BM25_ONLY = "anchor_bm25_only"
    ANCHOR_DENSE_ONLY = "anchor_dense_only"
    BM25_DENSE = "bm25_dense"
    GRAPH_ONLY = "graph_only"
    ANCHOR_GRAPH_CONDITIONAL = "anchor_graph_conditional"
    LEGACY_REGISTERED_TRIPLE_DIAGNOSTIC = "legacy_registered_triple_diagnostic"


class PlanningEvaluationError(ValueError):
    """A frozen Stage 4 evaluation input or runtime boundary is invalid."""


class PlanningCaseLoadError(PlanningEvaluationError):
    """A formal Stage 4 case file cannot be read or validated."""


class PlanningGateLoadError(PlanningEvaluationError):
    """Frozen Stage 4 pilot, rubric, or threshold input is invalid."""


def load_planning_evaluation_case(path: Path) -> PlanningEvaluationCase:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise PlanningCaseLoadError(f"cannot read Stage 4 case: {path}") from error
    try:
        return PlanningEvaluationCase.model_validate_json(payload)
    except ValidationError as error:
        raise PlanningCaseLoadError(f"invalid Stage 4 case: {path}") from error


class PlanningEvaluationAdapter(Protocol):
    def run_case(
        self,
        case: PlanningEvaluationCase,
        arm: PlanningEvaluationArm,
    ) -> PlanningEvaluationObservation: ...


class ConfiguredPlanningEvaluationAdapter:
    def __init__(
        self,
        execute: Callable[
            [PlanningEvaluationCase, PlanningEvaluationArm], PlanningEvaluationObservation
        ],
    ) -> None:
        self._execute = execute

    def run_case(
        self,
        case: PlanningEvaluationCase,
        arm: PlanningEvaluationArm,
    ) -> PlanningEvaluationObservation:
        return self._execute(case, arm)


class FakePlanningEvaluationAdapter:
    """Deterministic adapter for runner/report contract tests, never a formal Gate."""

    def __init__(
        self,
        results: Mapping[tuple[str, str], PlanningEvaluationObservation],
    ) -> None:
        self._results = dict(results)

    def run_case(
        self,
        case: PlanningEvaluationCase,
        arm: PlanningEvaluationArm,
    ) -> PlanningEvaluationObservation:
        try:
            return self._results[(case.case_id.root, arm.value)]
        except KeyError as error:
            raise ValueError("fake Stage 4 evaluation result is not predeclared") from error


BlindEvaluator = Callable[[ArtifactRef], Mapping[str, JsonValue]]

REQUIRED_BLIND_REVIEW_METRICS = frozenset(metric.value for metric in PlanningEvaluationMetric)


@dataclass(frozen=True, slots=True)
class FrozenPlanningEvaluationGate:
    pilot_ref: ArtifactRef
    rubric_ref: ArtifactRef
    threshold_ref: ArtifactRef
    rubric: PlanningEvaluationRubric
    thresholds: PlanningEvaluationThresholds


def load_frozen_planning_evaluation_gate(
    *,
    pilot_path: Path,
    rubric_path: Path,
    threshold_path: Path,
    artifacts: ArtifactRepository,
    schema_version: SchemaVersion,
) -> FrozenPlanningEvaluationGate:
    try:
        pilot = json.loads(pilot_path.read_text(encoding="utf-8"))
        rubric = PlanningEvaluationRubric.model_validate_json(rubric_path.read_bytes())
        thresholds = PlanningEvaluationThresholds.model_validate_json(threshold_path.read_bytes())
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        raise PlanningGateLoadError("cannot load frozen Stage 4 Gate inputs") from error
    pilot_ref = artifacts.put(
        canonical_json_bytes(pilot),
        "application/vnd.novel-agent.stage4-evaluation-pilot+json",
        schema_version,
    )
    rubric_ref = artifacts.put(
        canonical_json_bytes(rubric.model_dump(mode="json")),
        "application/vnd.novel-agent.stage4-evaluation-rubric+json",
        schema_version,
    )
    threshold_ref = artifacts.put(
        canonical_json_bytes(thresholds.model_dump(mode="json")),
        "application/vnd.novel-agent.stage4-evaluation-thresholds+json",
        schema_version,
    )
    return FrozenPlanningEvaluationGate(
        pilot_ref=pilot_ref,
        rubric_ref=rubric_ref,
        threshold_ref=threshold_ref,
        rubric=rubric,
        thresholds=thresholds,
    )


class PlanningEvaluationRunner:
    version = "stage4_planning_evaluation.v1"

    def __init__(
        self,
        *,
        adapter: PlanningEvaluationAdapter,
        artifacts: ArtifactRepository,
        schema_version: SchemaVersion,
        blind_evaluator: BlindEvaluator | None = None,
        frozen_gate: FrozenPlanningEvaluationGate | None = None,
        call_ledger: ModelCallLedgerPort | None = None,
    ) -> None:
        self._adapter = adapter
        self._artifacts = artifacts
        self._schema_version = schema_version
        self._blind_evaluator = blind_evaluator
        self._frozen_gate = frozen_gate
        self._call_ledger = call_ledger
        self._profile = (
            PlanningEvaluationProfile.DETERMINISTIC_FAKE
            if isinstance(adapter, FakePlanningEvaluationAdapter)
            else PlanningEvaluationProfile.FORMAL_CONFIGURED
        )
        if self._profile is PlanningEvaluationProfile.FORMAL_CONFIGURED and blind_evaluator is None:
            raise PlanningEvaluationError(
                "formal Stage 4 evaluation requires a post-freeze blind evaluator"
            )
        if self._profile is PlanningEvaluationProfile.FORMAL_CONFIGURED and frozen_gate is None:
            raise PlanningEvaluationError(
                "formal Stage 4 evaluation requires frozen pilot, rubric, and thresholds"
            )

    def run(
        self,
        manifest: PlanningEvaluationManifest,
        *,
        arms: tuple[PlanningEvaluationArm, ...] = (
            PlanningEvaluationArm.CONFIGURED,
            PlanningEvaluationArm.AUTHOR_CURRENT_PLAN_ONLY,
            PlanningEvaluationArm.EXACT_TEMPORAL_ONLY,
            PlanningEvaluationArm.ANCHOR_BM25_ONLY,
            PlanningEvaluationArm.ANCHOR_DENSE_ONLY,
            PlanningEvaluationArm.BM25_DENSE,
            PlanningEvaluationArm.GRAPH_ONLY,
            PlanningEvaluationArm.ANCHOR_GRAPH_CONDITIONAL,
            PlanningEvaluationArm.LEGACY_REGISTERED_TRIPLE_DIAGNOSTIC,
        ),
    ) -> tuple[PlanningEvaluationReport, ArtifactRef]:
        if not manifest.frozen_before_evaluator:
            raise ValueError("Stage 4 evaluation manifest must freeze before evaluator access")
        frozen_refs: tuple[ArtifactRef, ...] = ()
        if self._profile is PlanningEvaluationProfile.FORMAL_CONFIGURED:
            frozen_refs = self._validate_frozen_gate(manifest)
        if not arms or len(arms) != len(set(arms)):
            raise ValueError("Stage 4 evaluation arms must be non-empty and unique")
        manifest_ref = self._artifacts.put(
            canonical_json_bytes(manifest.model_dump(mode="json")),
            "application/vnd.novel-agent.stage4-evaluation-manifest+json",
            self._schema_version,
        )
        arm_observations: dict[str, dict[str, PlanningEvaluationObservation]] = {}
        for arm in arms:
            per_case: dict[str, PlanningEvaluationObservation] = {}
            for case in manifest.cases:
                if arm is PlanningEvaluationArm.GRAPH_ONLY and not any(
                    tag in {"relation", "causal", "multi_hop"} for tag in case.expected_issue_tags
                ):
                    continue
                observation = self._adapter.run_case(case, arm)
                result = observation.result
                if result.request_id != case.request.request_id:
                    raise ValueError("Stage 4 evaluation result belongs to another request")
                if observation.configuration_fingerprint != manifest.configuration_fingerprint:
                    raise PlanningEvaluationError(
                        "Stage 4 evaluation observation configuration differs from manifest"
                    )
                if observation.model_fingerprint != manifest.model_fingerprint:
                    raise PlanningEvaluationError(
                        "Stage 4 evaluation observation model differs from manifest"
                    )
                per_case[case.case_id.root] = observation
            arm_observations[arm.value] = per_case
        arm_results = {
            arm_name: {case_id: observation.result for case_id, observation in observations.items()}
            for arm_name, observations in arm_observations.items()
        }
        configured = arm_results.get(PlanningEvaluationArm.CONFIGURED.value, {})
        if set(configured) != {case.case_id.root for case in manifest.cases}:
            raise ValueError("configured Stage 4 arm must cover every Planner mode")
        blind_candidates = tuple(
            {
                "candidate_id": content_id(
                    {
                        "manifest_id": manifest.manifest_id.root,
                        "case_id": case.case_id.root,
                        "arm": arm_name,
                    }
                ).root,
                "case_id": case.case_id.root,
                "mode": case.mode.value,
                "result": results[case.case_id.root].model_dump(mode="json"),
            }
            for arm_name, results in arm_results.items()
            for case in manifest.cases
            if case.case_id.root in results
        )
        blind_payload = {"candidates": blind_candidates}
        blind_ref = self._artifacts.put(
            canonical_json_bytes(blind_payload),
            "application/vnd.novel-agent.stage4-blind-evaluator-input+json",
            self._schema_version,
        )
        reviewer_metrics: dict[str, JsonValue] = {}
        candidate_scores: dict[str, dict[str, float]] = {}
        if self._blind_evaluator is not None:
            reviewer_metrics = dict(self._blind_evaluator(blind_ref))
            if self._profile is PlanningEvaluationProfile.FORMAL_CONFIGURED:
                candidate_scores = _blind_candidate_scores(
                    reviewer_metrics,
                    tuple(cast(str, candidate["candidate_id"]) for candidate in blind_candidates),
                )
        ablation_metrics: dict[str, JsonValue] = {}
        for arm_name, results in arm_results.items():
            observations = arm_observations[arm_name]
            total = len(results)
            ready = sum(
                result.terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY
                for result in results.values()
            )
            human = sum(
                result.terminal is PlanningLoopTerminal.HUMAN_REQUIRED
                for result in results.values()
            )
            blind_candidate_ids = [
                content_id(
                    {
                        "manifest_id": manifest.manifest_id.root,
                        "case_id": case.case_id.root,
                        "arm": arm_name,
                    }
                ).root
                for case in manifest.cases
                if case.case_id.root in results
            ]
            arm_metrics: dict[str, JsonValue] = {
                "case_count": total,
                "ready_count": ready,
                "ready_rate": 0.0 if total == 0 else ready / total,
                "human_required_count": human,
                "prompt_tokens": sum(item.prompt_tokens for item in observations.values()),
                "completion_tokens": sum(item.completion_tokens for item in observations.values()),
                "latency_ms": sum(item.latency_ms for item in observations.values()),
                "model_call_count": sum(item.model_call_count for item in observations.values()),
                "exposed_evidence_count": sum(
                    item.exposed_evidence_count for item in observations.values()
                ),
                "used_evidence_count": sum(
                    item.used_evidence_count for item in observations.values()
                ),
                "channel_failure_count": sum(
                    item.channel_failure_count for item in observations.values()
                ),
                "degraded_count": sum(item.degraded for item in observations.values()),
                "blind_candidate_ids": cast(list[JsonValue], blind_candidate_ids),
            }
            if candidate_scores:
                arm_metrics["semantic_metrics"] = cast(
                    JsonValue,
                    _aggregate_candidate_scores(
                        blind_candidate_ids,
                        candidate_scores,
                    ),
                )
            ablation_metrics[arm_name] = arm_metrics
        configured_results = tuple(configured[case.case_id.root] for case in manifest.cases)
        all_results = tuple(
            result for results in arm_results.values() for result in results.values()
        )
        lineage = tuple(
            dict.fromkeys(
                (
                    manifest_ref,
                    *frozen_refs,
                    blind_ref,
                    *(artifact for result in all_results for artifact in result.event_artifacts),
                )
            )
        )
        leakage_count = sum(
            "FUTURE" in code or "LEAK" in code
            for result in configured_results
            for code in result.diagnostic_codes
        )
        provenance_error_count = sum(
            "PROVENANCE" in code
            for result in configured_results
            for code in result.diagnostic_codes
        )
        configured_human_required_rate = sum(
            result.terminal is PlanningLoopTerminal.HUMAN_REQUIRED for result in configured_results
        ) / len(configured_results)
        configured_semantic_metrics = (
            _aggregate_candidate_scores(
                [
                    content_id(
                        {
                            "manifest_id": manifest.manifest_id.root,
                            "case_id": case.case_id.root,
                            "arm": PlanningEvaluationArm.CONFIGURED.value,
                        }
                    ).root
                    for case in manifest.cases
                ],
                candidate_scores,
            )
            if candidate_scores
            else {}
        )
        model_call_aggregates: tuple[ModelCallLedgerAggregate, ...] = ()
        if self._call_ledger is not None:
            task_ids = {case.request.task_id for case in manifest.cases}
            run_ids = {case.request.run_id for case in manifest.cases}
            ledger_entries = tuple(
                entry
                for run_id in run_ids
                for entry in self._call_ledger.list_for_run(run_id)
                if entry.task_id in task_ids
            )
            model_call_aggregates = aggregate_model_calls(ledger_entries)
        report = PlanningEvaluationReport(
            manifest_id=manifest.manifest_id,
            evaluation_profile=self._profile,
            gate_eligible=self._profile is PlanningEvaluationProfile.FORMAL_CONFIGURED,
            semantic_gate_passed=(
                self._semantic_gate_passed(
                    configured_semantic_metrics,
                    human_required_rate=configured_human_required_rate,
                    leakage_count=leakage_count,
                    provenance_error_count=provenance_error_count,
                )
                if self._profile is PlanningEvaluationProfile.FORMAL_CONFIGURED
                else None
            ),
            results=configured_results,
            lineage_artifacts=lineage,
            ablation_metrics=ablation_metrics,
            reviewer_metrics=reviewer_metrics,
            leakage_count=leakage_count,
            provenance_error_count=provenance_error_count,
            model_call_aggregates=model_call_aggregates,
        )
        report_ref = self._artifacts.put(
            canonical_json_bytes(report.model_dump(mode="json")),
            "application/vnd.novel-agent.stage4-evaluation-report+json",
            self._schema_version,
        )
        return report, report_ref

    def _validate_frozen_gate(
        self,
        manifest: PlanningEvaluationManifest,
    ) -> tuple[ArtifactRef, ...]:
        gate = cast(FrozenPlanningEvaluationGate, self._frozen_gate)
        expected = (
            (gate.pilot_ref, manifest.pilot_fingerprint, "pilot"),
            (gate.rubric_ref, manifest.rubric_fingerprint, "rubric"),
            (gate.threshold_ref, manifest.threshold_fingerprint, "threshold"),
        )
        for ref, fingerprint, label in expected:
            if ref.artifact_id != fingerprint:
                raise PlanningEvaluationError(
                    f"frozen Stage 4 {label} artifact differs from manifest fingerprint"
                )
            self._artifacts.read_verified(ref)
        if content_id(gate.rubric.model_dump(mode="json")) != gate.rubric_ref.artifact_id:
            raise PlanningEvaluationError("frozen Stage 4 rubric content differs from its artifact")
        if content_id(gate.thresholds.model_dump(mode="json")) != gate.threshold_ref.artifact_id:
            raise PlanningEvaluationError(
                "frozen Stage 4 thresholds content differs from its artifact"
            )
        return gate.pilot_ref, gate.rubric_ref, gate.threshold_ref

    def _semantic_gate_passed(
        self,
        metrics: Mapping[str, float],
        *,
        human_required_rate: float,
        leakage_count: int,
        provenance_error_count: int,
    ) -> bool:
        gate = cast(FrozenPlanningEvaluationGate, self._frozen_gate)
        thresholds = gate.thresholds
        checks = (
            metrics["author_intent_coverage_rate"] >= thresholds.author_intent_coverage_rate_min,
            metrics["accepted_plan_canon_contradiction_count"]
            <= thresholds.accepted_plan_canon_contradiction_count_max,
            metrics["obligation_arc_hook_continuity_score"]
            >= thresholds.obligation_arc_hook_continuity_score_min,
            metrics["rolling_hierarchy_consistency_score"]
            >= thresholds.rolling_hierarchy_consistency_score_min,
            metrics["chapter_feasibility_score"] >= thresholds.chapter_feasibility_score_min,
            metrics["alternative_quality_score"] >= thresholds.alternative_quality_score_min,
            metrics["decision_rationale_score"] >= thresholds.decision_rationale_score_min,
            metrics["reviewer_issue_recall"] >= thresholds.reviewer_issue_recall_min,
            human_required_rate <= thresholds.human_required_rate_max,
            metrics["future_leakage_count"] <= thresholds.future_leakage_count_max,
            metrics["provenance_error_count"] <= thresholds.provenance_error_count_max,
            metrics["unsupported_factualization_count"]
            <= thresholds.unsupported_factualization_count_max,
            leakage_count == 0,
            provenance_error_count == 0,
        )
        return all(checks)


def _blind_candidate_scores(
    evaluator_output: Mapping[str, JsonValue],
    candidate_ids: tuple[str, ...],
) -> dict[str, dict[str, float]]:
    raw_scores = evaluator_output.get("candidate_scores")
    if not isinstance(raw_scores, dict):
        raise PlanningEvaluationError("blind Stage 4 evaluator omitted candidate_scores")
    if set(raw_scores) != set(candidate_ids):
        raise PlanningEvaluationError("blind Stage 4 evaluator candidate identities differ")
    scores: dict[str, dict[str, float]] = {}
    for candidate_id, raw_metrics in raw_scores.items():
        if not isinstance(raw_metrics, dict):
            raise PlanningEvaluationError("blind Stage 4 candidate score must be an object")
        if set(raw_metrics) != REQUIRED_BLIND_REVIEW_METRICS:
            raise PlanningEvaluationError("blind Stage 4 candidate rubric metrics differ")
        scores[candidate_id] = {
            name: _metric_number(raw_metrics, name) for name in REQUIRED_BLIND_REVIEW_METRICS
        }
    return scores


def _aggregate_candidate_scores(
    candidate_ids: list[str],
    scores: Mapping[str, Mapping[str, float]],
) -> dict[str, float]:
    if not candidate_ids:
        return {}
    aggregate: dict[str, float] = {}
    for metric in PlanningEvaluationMetric:
        values = [scores[candidate_id][metric.value] for candidate_id in candidate_ids]
        aggregate[metric.value] = (
            sum(values) if metric.value.endswith("_count") else sum(values) / len(values)
        )
    return aggregate


def _metric_number(metrics: Mapping[str, JsonValue], name: str) -> float:
    value = metrics[name]
    if type(value) not in (int, float):
        raise PlanningEvaluationError(f"blind Stage 4 metric must be numeric: {name}")
    return float(cast(int | float, value))


def evaluation_identity(manifest: PlanningEvaluationManifest) -> StableId:
    digest = content_id(manifest.model_dump(mode="json")).root.removeprefix("sha256:")[:24]
    return StableId(f"stage4-evaluation.{digest}")
