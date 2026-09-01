"""Freeze U4-L0 canary variables so four-condition diffs stay attributable."""

from __future__ import annotations

from novel_agent.domain.v05_readout import U4L0CanaryVariableLock

CONTROLLER_LEVELS = frozenset({"C0", "C1+C2"})
PLANNER_LEVELS = frozenset({"P0", "P0+P1"})
_COMPARISON_FIELDS = (
    "budget_profile",
    "controller_context_level",
    "planner_context_level",
    "thinking_enabled",
)


class CanaryVariableError(ValueError):
    """A canary lock or comparison mixes more than one U4-L0 variable."""


def single_factor_diff(
    baseline: U4L0CanaryVariableLock,
    candidate: U4L0CanaryVariableLock,
) -> str:
    """Return the one changed variable. Mixing factors is a protocol error."""

    diffs = [
        field
        for field in _COMPARISON_FIELDS
        if getattr(baseline, field) != getattr(candidate, field)
    ]
    if baseline.c3_admission != candidate.c3_admission:
        diffs.append("c3_admission")
    if len(diffs) != 1:
        raise CanaryVariableError(
            f"canary comparison must change exactly one frozen variable, got {diffs or 'none'}"
        )
    return diffs[0]


__all__ = ["CanaryVariableError", "U4L0CanaryVariableLock", "single_factor_diff"]
