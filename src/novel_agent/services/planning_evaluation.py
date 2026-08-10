"""Freeze-first seven-mode Stage 4 evaluation and ablation reporting."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import JsonValue, ValidationError

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import SchemaVersion, StableId
from novel_agent.domain.planning import (
    PlanningEvaluationCase,
    PlanningEvaluationManifest,
    PlanningEvaluationProfile,
    PlanningEvaluationReport,
    PlanningLoopResult,
    PlanningLoopTerminal,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id


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
    ) -> PlanningLoopResult: ...


class ConfiguredPlanningEvaluationAdapter:
    def __init__(
        self,
        execute: Callable[[PlanningEvaluationCase, PlanningEvaluationArm], PlanningLoopResult],
    ) -> None:
        self._execute = execute

    def run_case(
        self,
        case: PlanningEvaluationCase,
        arm: PlanningEvaluationArm,
    ) -> PlanningLoopResult:
        return self._execute(case, arm)


class FakePlanningEvaluationAdapter:
    """Deterministic adapter for runner/report contract tests, never a formal Gate."""

    def __init__(self, results: Mapping[tuple[str, str], PlanningLoopResult]) -> None:
        self._results = dict(results)

    def run_case(
        self,
        case: PlanningEvaluationCase,
        arm: PlanningEvaluationArm,
    ) -> PlanningLoopResult:
        try:
            return self._results[(case.case_id.root, arm.value)]
        except KeyError as error:
            raise ValueError("fake Stage 4 evaluation result is not predeclared") from error


BlindEvaluator = Callable[[ArtifactRef], Mapping[str, JsonValue]]

REQUIRED_BLIND_REVIEW_METRICS = frozenset(
    {
        "accepted_plan_canon_contradiction_count",
        "alternative_quality_score",
        "author_intent_coverage_rate",
        "continuity_score",
        "future_leakage_count",
        "provenance_error_count",
        "reviewer_issue_recall",
        "unsupported_factualization_count",
    }
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
    ) -> None:
        self._adapter = adapter
        self._artifacts = artifacts
        self._schema_version = schema_version
        self._blind_evaluator = blind_evaluator
        self._profile = (
            PlanningEvaluationProfile.DETERMINISTIC_FAKE
            if isinstance(adapter, FakePlanningEvaluationAdapter)
            else PlanningEvaluationProfile.FORMAL_CONFIGURED
        )
        if self._profile is PlanningEvaluationProfile.FORMAL_CONFIGURED and blind_evaluator is None:
            raise PlanningEvaluationError(
                "formal Stage 4 evaluation requires a post-freeze blind evaluator"
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
        if not arms or len(arms) != len(set(arms)):
            raise ValueError("Stage 4 evaluation arms must be non-empty and unique")
        manifest_ref = self._artifacts.put(
            canonical_json_bytes(manifest.model_dump(mode="json")),
            "application/vnd.novel-agent.stage4-evaluation-manifest+json",
            self._schema_version,
        )
        arm_results: dict[str, dict[str, PlanningLoopResult]] = {}
        for arm in arms:
            per_case: dict[str, PlanningLoopResult] = {}
            for case in manifest.cases:
                if arm is PlanningEvaluationArm.GRAPH_ONLY and not any(
                    tag in {"relation", "causal", "multi_hop"} for tag in case.expected_issue_tags
                ):
                    continue
                result = self._adapter.run_case(case, arm)
                if result.request_id != case.request.request_id:
                    raise ValueError("Stage 4 evaluation result belongs to another request")
                per_case[case.case_id.root] = result
            arm_results[arm.value] = per_case
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
        blind_payload = {
            "manifest_ref": manifest_ref.model_dump(mode="json"),
            "candidates": blind_candidates,
        }
        blind_ref = self._artifacts.put(
            canonical_json_bytes(blind_payload),
            "application/vnd.novel-agent.stage4-blind-evaluator-input+json",
            self._schema_version,
        )
        reviewer_metrics: dict[str, JsonValue] = {}
        if self._blind_evaluator is not None:
            reviewer_metrics = dict(self._blind_evaluator(blind_ref))
            missing_metrics = REQUIRED_BLIND_REVIEW_METRICS.difference(reviewer_metrics)
            if missing_metrics:
                missing = ", ".join(sorted(missing_metrics))
                raise PlanningEvaluationError(
                    f"blind Stage 4 evaluator omitted required metrics: {missing}"
                )
        ablation_metrics: dict[str, JsonValue] = {}
        for arm_name, results in arm_results.items():
            total = len(results)
            ready = sum(
                result.terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY
                for result in results.values()
            )
            human = sum(
                result.terminal is PlanningLoopTerminal.HUMAN_REQUIRED
                for result in results.values()
            )
            ablation_metrics[arm_name] = {
                "case_count": total,
                "ready_count": ready,
                "ready_rate": 0.0 if total == 0 else ready / total,
                "human_required_count": human,
                "blind_candidate_ids": [
                    content_id(
                        {
                            "manifest_id": manifest.manifest_id.root,
                            "case_id": case.case_id.root,
                            "arm": arm_name,
                        }
                    ).root
                    for case in manifest.cases
                    if case.case_id.root in results
                ],
            }
        configured_results = tuple(configured[case.case_id.root] for case in manifest.cases)
        all_results = tuple(
            result for results in arm_results.values() for result in results.values()
        )
        lineage = tuple(
            dict.fromkeys(
                (
                    manifest_ref,
                    blind_ref,
                    *(artifact for result in all_results for artifact in result.event_artifacts),
                )
            )
        )
        report = PlanningEvaluationReport(
            manifest_id=manifest.manifest_id,
            evaluation_profile=self._profile,
            gate_eligible=self._profile is PlanningEvaluationProfile.FORMAL_CONFIGURED,
            results=configured_results,
            lineage_artifacts=lineage,
            ablation_metrics=ablation_metrics,
            reviewer_metrics=reviewer_metrics,
            leakage_count=sum(
                "FUTURE" in code or "LEAK" in code
                for result in configured_results
                for code in result.diagnostic_codes
            ),
            provenance_error_count=sum(
                "PROVENANCE" in code
                for result in configured_results
                for code in result.diagnostic_codes
            ),
        )
        report_ref = self._artifacts.put(
            canonical_json_bytes(report.model_dump(mode="json")),
            "application/vnd.novel-agent.stage4-evaluation-report+json",
            self._schema_version,
        )
        return report, report_ref


def evaluation_identity(manifest: PlanningEvaluationManifest) -> StableId:
    digest = content_id(manifest.model_dump(mode="json")).root.removeprefix("sha256:")[:24]
    return StableId(f"stage4-evaluation.{digest}")
