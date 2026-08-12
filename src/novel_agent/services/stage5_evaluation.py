"""Deterministic isolated Stage 5 scenario report assembly."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from novel_agent.domain.ids import ArtifactId
from novel_agent.domain.stage5_evaluation import (
    IsolatedKernelStatus,
    Stage5IsolatedKernelReport,
    Stage5ScenarioEvidence,
)

REQUIRED_SCENARIOS = frozenset(
    {
        "plan_manual_accept_commit_freshness",
        "draft_accept_commit_freshness",
        "mode_stop_point_parity",
        "layer_local_recovery",
        "three_chapter_topology",
        "same_project_single_writer",
        "cross_project_model_admission",
    }
)


class IsolatedRuntimeEvaluator:
    async def evaluate(
        self,
        scenarios: Mapping[str, Callable[[], Awaitable[Stage5ScenarioEvidence]]],
        *,
        executable_commit: str,
        manifest_fingerprint: ArtifactId,
    ) -> Stage5IsolatedKernelReport:
        missing = REQUIRED_SCENARIOS.difference(scenarios)
        extra = set(scenarios).difference(REQUIRED_SCENARIOS)
        if missing or extra:
            raise ValueError(
                f"scenario registry mismatch: missing={sorted(missing)}, extra={sorted(extra)}"
            )
        evidence = tuple([await scenarios[name]() for name in sorted(REQUIRED_SCENARIOS)])
        if {item.scenario_id for item in evidence} != REQUIRED_SCENARIOS:
            raise ValueError("scenario evidence identities do not match the formal registry")
        status = (
            IsolatedKernelStatus.PASS
            if all(item.passed for item in evidence)
            else IsolatedKernelStatus.FAILED
        )
        return Stage5IsolatedKernelReport(
            status=status,
            executable_commit=executable_commit,
            manifest_fingerprint=manifest_fingerprint,
            scenarios=evidence,
        )


__all__ = ["REQUIRED_SCENARIOS", "IsolatedRuntimeEvaluator"]
