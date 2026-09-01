"""U4-L0 canary report-lock regressions."""

from __future__ import annotations

import pytest
from scripts.run_u4l0_canary import _pair_report

from novel_agent.domain.model_calls import BudgetResolutionProfile
from novel_agent.services.u4l0_canary import U4L0CanaryVariableLock


def _arm(lock: U4L0CanaryVariableLock) -> dict[str, object]:
    return {
        "arm_id": "arm",
        "lock": lock.model_dump(mode="json"),
        "status": "COMPLETED",
        "basis": {"commit": "commit", "snapshot_id": "snapshot"},
        "model_identity": {"id": "qwen38-27b-fp8"},
        "future_leakage_count": 0,
        "planner": {"fallback_used": False},
        "raw_and_ledger": {"raw_artifact_refs_complete": True},
    }


@pytest.mark.parametrize(
    ("factor", "update"),
    (
        ("controller", {"controller_context_level": "C1+C2"}),
        ("planner", {"planner_context_level": "P0+P1"}),
        ("thinking", {"thinking_enabled": True}),
    ),
)
def test_pair_report_accepts_semantic_factor_labels(factor: str, update: dict[str, object]) -> None:
    baseline_lock = U4L0CanaryVariableLock(
        budget_profile=BudgetResolutionProfile.CANARY,
        controller_context_level="C0",
        planner_context_level="P0",
        thinking_enabled=False,
    )
    candidate_lock = baseline_lock.model_copy(update=update)

    report = _pair_report(
        factor=factor,
        baseline=_arm(baseline_lock),
        candidate=_arm(candidate_lock),
    )

    assert report["comparable"] is True
    assert report["expected_lock_field"] in {
        "controller_context_level",
        "planner_context_level",
        "thinking_enabled",
    }
