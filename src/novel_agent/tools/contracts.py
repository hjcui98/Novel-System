"""Trusted-context injection and permission enforcement for in-process tools."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Protocol

from pydantic import JsonValue

from novel_agent.domain.stage2 import (
    ControllerStopReason,
    ToolCallContext,
    ToolFailureCode,
    ToolPolicy,
    ToolResult,
    ToolResultStatus,
)

ToolHandler = Callable[[ToolCallContext, JsonValue], Awaitable[ToolResult]]

TERMINAL_TOOL_FAILURE_CODES: frozenset[ToolFailureCode] = frozenset(
    {
        ToolFailureCode.BUDGET_EXCEEDED,
        ToolFailureCode.BASE_COMMIT_MISMATCH,
        ToolFailureCode.SNAPSHOT_STALE,
        ToolFailureCode.ACCESS_DENIED,
        ToolFailureCode.SCOPE_MISMATCH,
    }
)


class ToolAuditSink(Protocol):
    def requested(
        self,
        context: ToolCallContext,
        invocation: ToolInvocation,
        policy: ToolPolicy,
    ) -> None: ...

    def completed(
        self,
        context: ToolCallContext,
        invocation: ToolInvocation,
        policy: ToolPolicy,
        result: ToolResult,
    ) -> None: ...


class ToolBindingError(ValueError):
    pass


@dataclass(frozen=True)
class ToolInvocation:
    tool_name: str
    arguments: JsonValue


@dataclass
class ToolBudget:
    max_calls: int
    deadline: float
    calls_used: int = 0

    @classmethod
    def from_policy(cls, policy: ToolPolicy) -> ToolBudget:
        return cls(
            max_calls=policy.max_tool_calls,
            deadline=monotonic() + policy.wall_clock_budget_ms / 1000,
        )

    def remaining_ms(self) -> float:
        return max(0.0, (self.deadline - monotonic()) * 1000)

    def exhausted(self) -> bool:
        return self.calls_used >= self.max_calls or monotonic() >= self.deadline


@dataclass
class ControllerBudgetState:
    """Unified controller budget covering model decisions and tool calls (WP1)."""

    deadline_monotonic: float
    max_decision_model_calls: int
    max_tool_calls: int
    decision_model_calls_used: int = 0
    tool_calls_used: int = 0
    tool_success_count: int = 0
    tool_failure_count: int = 0
    backend_search_count: int = 0
    terminal_failure: str | None = None
    tool_budget: ToolBudget = field(init=False)

    def __post_init__(self) -> None:
        self.tool_budget = ToolBudget(
            max_calls=self.max_tool_calls,
            deadline=self.deadline_monotonic,
            calls_used=self.tool_calls_used,
        )

    @classmethod
    def from_policy(
        cls,
        policy: ToolPolicy,
        *,
        max_tool_calls: int | None = None,
        max_decision_model_calls: int = 2,
        wall_clock_budget_ms: int | None = None,
    ) -> ControllerBudgetState:
        wall_ms = (
            policy.wall_clock_budget_ms if wall_clock_budget_ms is None else wall_clock_budget_ms
        )
        return cls(
            deadline_monotonic=monotonic() + wall_ms / 1000,
            max_decision_model_calls=max_decision_model_calls,
            max_tool_calls=policy.max_tool_calls if max_tool_calls is None else max_tool_calls,
        )

    def remaining_wall_clock_ms(self) -> float:
        return max(0.0, (self.deadline_monotonic - monotonic()) * 1000)

    def wall_clock_exhausted(self) -> bool:
        return monotonic() >= self.deadline_monotonic

    def can_decide(self) -> bool:
        return (
            self.terminal_failure is None
            and not self.wall_clock_exhausted()
            and self.decision_model_calls_used < self.max_decision_model_calls
        )

    def can_invoke_tool(self) -> bool:
        return (
            self.terminal_failure is None
            and not self.wall_clock_exhausted()
            and self.tool_calls_used < self.max_tool_calls
        )

    def record_decision_call(self) -> None:
        self.decision_model_calls_used += 1

    def mark_terminal(self, failure: str) -> None:
        self.terminal_failure = failure

    def sync_tool_budget(self) -> ToolBudget:
        self.tool_budget.max_calls = self.max_tool_calls
        self.tool_budget.deadline = self.deadline_monotonic
        self.tool_budget.calls_used = self.tool_calls_used
        return self.tool_budget

    def note_tool_result(self, result: ToolResult, *, backend_executed: bool) -> None:
        self.tool_calls_used = self.tool_budget.calls_used
        if result.status is ToolResultStatus.SUCCEEDED:
            self.tool_success_count += 1
            if backend_executed:
                self.backend_search_count += 1
            return
        self.tool_failure_count += 1
        code = result.failure_code
        if code is ToolFailureCode.BUDGET_EXCEEDED or code is ToolFailureCode.TIMEOUT:
            self.mark_terminal(ControllerStopReason.BUDGET_EXHAUSTED.value)
        elif code in {
            ToolFailureCode.BASE_COMMIT_MISMATCH,
            ToolFailureCode.SNAPSHOT_STALE,
        }:
            self.mark_terminal(ControllerStopReason.FRESHNESS_BLOCKED.value)
        elif code in {ToolFailureCode.ACCESS_DENIED, ToolFailureCode.SCOPE_MISMATCH}:
            self.mark_terminal(ControllerStopReason.ACCESS_BLOCKED.value)

    def stop_reason_for_terminal(self) -> ControllerStopReason:
        if self.terminal_failure is None:
            return ControllerStopReason.BUDGET_EXHAUSTED
        try:
            return ControllerStopReason(self.terminal_failure)
        except ValueError:
            return ControllerStopReason.BUDGET_EXHAUSTED


class ToolBinding:
    """Executes one allow-listed handler with runtime-owned identity and basis."""

    def __init__(
        self,
        policy: ToolPolicy,
        handlers: dict[str, ToolHandler],
        audit_sink: ToolAuditSink | None = None,
    ) -> None:
        missing = set(policy.allowed_tools) - handlers.keys()
        if missing:
            raise ToolBindingError(f"allowed tools have no handler: {sorted(missing)}")
        self._policy = policy
        self._handlers = dict(handlers)
        self._audit_sink = audit_sink

    async def invoke(
        self,
        invocation: ToolInvocation,
        trusted_context: ToolCallContext,
        budget: ToolBudget,
    ) -> ToolResult:
        if self._audit_sink is not None:
            self._audit_sink.requested(trusted_context, invocation, self._policy)
        if invocation.tool_name not in self._policy.allowed_tools:
            self._audit_complete(
                trusted_context,
                invocation,
                self._failure(trusted_context, ToolFailureCode.ACCESS_DENIED),
            )
            raise ToolBindingError(f"tool is not allowed: {invocation.tool_name}")
        if not trusted_context.read_only:
            self._audit_complete(
                trusted_context,
                invocation,
                self._failure(trusted_context, ToolFailureCode.ACCESS_DENIED),
            )
            raise ToolBindingError("Stage 2 tool binding requires read-only trusted context")
        if budget.calls_used >= budget.max_calls or monotonic() >= budget.deadline:
            result = self._failure(trusted_context, ToolFailureCode.BUDGET_EXCEEDED)
            self._audit_complete(trusted_context, invocation, result)
            return result
        budget.calls_used += 1
        timeout_seconds = min(
            trusted_context.timeout_ms / 1000,
            max(0.0, budget.deadline - monotonic()),
        )
        if timeout_seconds <= 0:
            result = self._failure(trusted_context, ToolFailureCode.BUDGET_EXCEEDED)
            self._audit_complete(trusted_context, invocation, result)
            return result
        try:
            result = await asyncio.wait_for(
                self._handlers[invocation.tool_name](trusted_context, invocation.arguments),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            result = self._failure(trusted_context, ToolFailureCode.TIMEOUT)
            self._audit_complete(trusted_context, invocation, result)
            return result
        except (ConnectionError, OSError):
            result = self._failure(trusted_context, ToolFailureCode.BACKEND_UNAVAILABLE)
            self._audit_complete(trusted_context, invocation, result)
            return result
        if result.tool_call_id != trusted_context.tool_call_id:
            self._audit_complete(
                trusted_context,
                invocation,
                self._failure(trusted_context, ToolFailureCode.INVALID_QUERY),
            )
            raise ToolBindingError("tool result call identity mismatch")
        if result.basis_commit != trusted_context.base_commit:
            result = self._failure(trusted_context, ToolFailureCode.BASE_COMMIT_MISMATCH)
            self._audit_complete(trusted_context, invocation, result)
            return result
        if (
            trusted_context.snapshot_id is not None
            and result.snapshot_id != trusted_context.snapshot_id
        ):
            result = self._failure(trusted_context, ToolFailureCode.SNAPSHOT_STALE)
            self._audit_complete(trusted_context, invocation, result)
            return result
        self._audit_complete(trusted_context, invocation, result)
        return result

    def _audit_complete(
        self,
        context: ToolCallContext,
        invocation: ToolInvocation,
        result: ToolResult,
    ) -> None:
        if self._audit_sink is not None:
            self._audit_sink.completed(context, invocation, self._policy, result)

    @staticmethod
    def _failure(context: ToolCallContext, code: ToolFailureCode) -> ToolResult:
        return ToolResult(
            tool_call_id=context.tool_call_id,
            status=ToolResultStatus.FAILED,
            basis_commit=context.base_commit,
            snapshot_id=context.snapshot_id,
            failure_code=code,
            audit_ref=context.tool_call_id,
        )
