"""Trusted-context injection and permission enforcement for in-process tools."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from typing import Protocol

from pydantic import JsonValue

from novel_agent.domain.stage2 import (
    ToolCallContext,
    ToolFailureCode,
    ToolPolicy,
    ToolResult,
    ToolResultStatus,
)

ToolHandler = Callable[[ToolCallContext, JsonValue], Awaitable[ToolResult]]


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
