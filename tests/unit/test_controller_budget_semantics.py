"""WP1: controller wall-clock and terminal budget semantics."""

from __future__ import annotations

from time import monotonic

from novel_agent.domain.ids import CommitId, StableId
from novel_agent.domain.stage2 import (
    ControllerStopReason,
    ToolFailureCode,
    ToolResult,
    ToolResultStatus,
)
from novel_agent.tools.contracts import ControllerBudgetState, ToolBudget

COMMIT = CommitId("sha256:" + "a" * 64)


def test_controller_budget_blocks_decision_after_model_cap() -> None:
    budget = ControllerBudgetState(
        deadline_monotonic=monotonic() + 30,
        max_decision_model_calls=2,
        max_tool_calls=10,
    )
    assert budget.can_decide()
    budget.record_decision_call()
    budget.record_decision_call()
    assert not budget.can_decide()
    assert budget.can_invoke_tool()


def test_controller_budget_deadline_blocks_tools_and_decisions() -> None:
    budget = ControllerBudgetState(
        deadline_monotonic=monotonic() - 0.01,
        max_decision_model_calls=2,
        max_tool_calls=10,
    )
    assert budget.wall_clock_exhausted()
    assert not budget.can_decide()
    assert not budget.can_invoke_tool()


def test_terminal_mapping_for_budget_and_access_failures() -> None:
    budget = ControllerBudgetState(
        deadline_monotonic=monotonic() + 30,
        max_decision_model_calls=2,
        max_tool_calls=10,
    )
    failed = type(
        "R",
        (),
        {
            "status": ToolResultStatus.FAILED,
            "failure_code": ToolFailureCode.BUDGET_EXCEEDED,
        },
    )()
    budget.tool_budget.calls_used = 1
    budget.note_tool_result(failed, backend_executed=False)  # type: ignore[arg-type]
    assert budget.terminal_failure == ControllerStopReason.BUDGET_EXHAUSTED.value
    assert budget.tool_failure_count == 1
    assert budget.backend_search_count == 0

    budget2 = ControllerBudgetState(
        deadline_monotonic=monotonic() + 30,
        max_decision_model_calls=2,
        max_tool_calls=10,
    )
    denied = type(
        "R",
        (),
        {
            "status": ToolResultStatus.FAILED,
            "failure_code": ToolFailureCode.SCOPE_MISMATCH,
        },
    )()
    budget2.tool_budget.calls_used = 1
    budget2.note_tool_result(denied, backend_executed=False)  # type: ignore[arg-type]
    assert budget2.terminal_failure == ControllerStopReason.ACCESS_BLOCKED.value


def test_tool_budget_remaining_and_exhausted() -> None:
    live = ToolBudget(max_calls=2, deadline=monotonic() + 5, calls_used=0)
    assert not live.exhausted()
    assert live.remaining_ms() > 0
    dead = ToolBudget(max_calls=1, deadline=monotonic() + 5, calls_used=1)
    assert dead.exhausted()


def _succeeded_result(call_id: str = "call.success") -> ToolResult:
    return ToolResult(
        tool_call_id=StableId(call_id),
        status=ToolResultStatus.SUCCEEDED,
        basis_commit=COMMIT,
        audit_ref=StableId(f"audit.{call_id}"),
    )


def _failed_result(code: ToolFailureCode, call_id: str = "call.failed") -> ToolResult:
    return ToolResult(
        tool_call_id=StableId(call_id),
        status=ToolResultStatus.FAILED,
        basis_commit=COMMIT,
        failure_code=code,
        audit_ref=StableId(f"audit.{call_id}"),
    )


def test_note_tool_result_succeeded_without_backend_executed() -> None:
    budget = ControllerBudgetState(
        deadline_monotonic=monotonic() + 30,
        max_decision_model_calls=2,
        max_tool_calls=10,
    )
    budget.tool_budget.calls_used = 1
    budget.note_tool_result(_succeeded_result(), backend_executed=False)
    assert budget.tool_success_count == 1
    assert budget.backend_search_count == 0
    assert budget.terminal_failure is None


def test_note_tool_result_succeeded_with_backend_executed() -> None:
    budget = ControllerBudgetState(
        deadline_monotonic=monotonic() + 30,
        max_decision_model_calls=2,
        max_tool_calls=10,
    )
    budget.tool_budget.calls_used = 1
    budget.note_tool_result(_succeeded_result(), backend_executed=True)
    assert budget.tool_success_count == 1
    assert budget.backend_search_count == 1


def test_note_tool_result_freshness_blocked_for_base_commit_mismatch() -> None:
    budget = ControllerBudgetState(
        deadline_monotonic=monotonic() + 30,
        max_decision_model_calls=2,
        max_tool_calls=10,
    )
    budget.tool_budget.calls_used = 1
    budget.note_tool_result(
        _failed_result(ToolFailureCode.BASE_COMMIT_MISMATCH, "call.base"),
        backend_executed=False,
    )
    assert budget.terminal_failure == ControllerStopReason.FRESHNESS_BLOCKED.value
    assert budget.tool_failure_count == 1


def test_note_tool_result_freshness_blocked_for_snapshot_stale() -> None:
    budget = ControllerBudgetState(
        deadline_monotonic=monotonic() + 30,
        max_decision_model_calls=2,
        max_tool_calls=10,
    )
    budget.tool_budget.calls_used = 1
    budget.note_tool_result(
        _failed_result(ToolFailureCode.SNAPSHOT_STALE, "call.stale"),
        backend_executed=False,
    )
    assert budget.terminal_failure == ControllerStopReason.FRESHNESS_BLOCKED.value


def test_note_tool_result_access_blocked_for_access_denied() -> None:
    budget = ControllerBudgetState(
        deadline_monotonic=monotonic() + 30,
        max_decision_model_calls=2,
        max_tool_calls=10,
    )
    budget.tool_budget.calls_used = 1
    budget.note_tool_result(
        _failed_result(ToolFailureCode.ACCESS_DENIED, "call.denied"),
        backend_executed=False,
    )
    assert budget.terminal_failure == ControllerStopReason.ACCESS_BLOCKED.value


def test_note_tool_result_non_terminal_failure_does_not_mark_terminal() -> None:
    budget = ControllerBudgetState(
        deadline_monotonic=monotonic() + 30,
        max_decision_model_calls=2,
        max_tool_calls=10,
    )
    budget.tool_budget.calls_used = 1
    budget.note_tool_result(
        _failed_result(ToolFailureCode.BACKEND_UNAVAILABLE, "call.backend"),
        backend_executed=False,
    )
    assert budget.terminal_failure is None
    assert budget.tool_failure_count == 1


def test_stop_reason_for_terminal_without_terminal_failure() -> None:
    budget = ControllerBudgetState(
        deadline_monotonic=monotonic() + 30,
        max_decision_model_calls=2,
        max_tool_calls=10,
    )
    assert budget.terminal_failure is None
    assert budget.stop_reason_for_terminal() == ControllerStopReason.BUDGET_EXHAUSTED


def test_stop_reason_for_terminal_with_valid_terminal_failure() -> None:
    budget = ControllerBudgetState(
        deadline_monotonic=monotonic() + 30,
        max_decision_model_calls=2,
        max_tool_calls=10,
    )
    budget.mark_terminal(ControllerStopReason.ACCESS_BLOCKED.value)
    assert budget.stop_reason_for_terminal() == ControllerStopReason.ACCESS_BLOCKED


def test_stop_reason_for_terminal_with_invalid_terminal_failure() -> None:
    budget = ControllerBudgetState(
        deadline_monotonic=monotonic() + 30,
        max_decision_model_calls=2,
        max_tool_calls=10,
    )
    budget.mark_terminal("not-a-real-stop-reason")
    assert budget.stop_reason_for_terminal() == ControllerStopReason.BUDGET_EXHAUSTED
