"""Replayable Tool Call RunEvent audit sink for Stage 2 typed bindings."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import JsonValue

from novel_agent.domain.ids import RunId, SchemaVersion, StableId
from novel_agent.domain.runtime import RunEvent, RunEventType
from novel_agent.domain.stage2 import (
    ToolCallContext,
    ToolPolicy,
    ToolResult,
    ToolResultStatus,
)
from novel_agent.services.content_addressing import content_id
from novel_agent.services.event_log import RunEventLogRepository
from novel_agent.tools.contracts import ToolInvocation


class ToolAuditConflictError(RuntimeError):
    pass


class RunEventToolAuditSink:
    def __init__(
        self,
        repository: RunEventLogRepository,
        run_id: RunId,
        *,
        trace_id: str,
        schema_version: SchemaVersion,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._run_id = run_id
        self._trace_id = trace_id
        self._schema_version = schema_version
        self._clock = clock
        existing = repository.replay(run_id)
        self._sequence = existing[-1].sequence_no if existing else 0
        self._by_identity = {event.idempotency_identity: event for event in existing}

    def requested(
        self,
        context: ToolCallContext,
        invocation: ToolInvocation,
        policy: ToolPolicy,
    ) -> None:
        self._assert_context(context)
        self._append(
            context,
            invocation,
            policy,
            RunEventType.TOOL_REQUESTED,
            StableId(f"tool-event.{context.tool_call_id.root}.requested"),
            {
                "tool_call_id": context.tool_call_id.root,
                "tool_name": invocation.tool_name,
                "arguments_hash": content_id(invocation.arguments).root,
                "base_commit": context.base_commit.root,
                "snapshot_id": context.snapshot_id.root if context.snapshot_id else None,
                "access_scope": context.access_scope.value,
                "read_only": context.read_only,
            },
        )

    def completed(
        self,
        context: ToolCallContext,
        invocation: ToolInvocation,
        policy: ToolPolicy,
        result: ToolResult,
    ) -> None:
        self._assert_context(context)
        event_type = (
            RunEventType.TOOL_COMPLETED
            if result.status is ToolResultStatus.SUCCEEDED
            else RunEventType.TOOL_FAILED
        )
        self._append(
            context,
            invocation,
            policy,
            event_type,
            StableId(f"tool-event.{context.tool_call_id.root}.completed"),
            {
                "tool_call_id": context.tool_call_id.root,
                "tool_name": invocation.tool_name,
                "status": result.status.value,
                "failure_code": result.failure_code.value if result.failure_code else None,
                "coverage": result.coverage,
                "basis_commit": result.basis_commit.root,
                "snapshot_id": result.snapshot_id.root if result.snapshot_id else None,
                "audit_ref": result.audit_ref.root,
            },
        )

    def _assert_context(self, context: ToolCallContext) -> None:
        if context.run_id != self._run_id:
            raise ToolAuditConflictError("tool context belongs to another run")

    def _append(
        self,
        context: ToolCallContext,
        invocation: ToolInvocation,
        policy: ToolPolicy,
        event_type: RunEventType,
        identity: StableId,
        payload: dict[str, JsonValue],
    ) -> None:
        existing = self._by_identity.get(identity)
        if existing is not None:
            if existing.event_type is not event_type or existing.payload != payload:
                raise ToolAuditConflictError("tool audit identity refers to another event")
            return
        self._sequence += 1
        event = RunEvent(
            event_id=StableId(f"event.{identity.root}"),
            run_id=context.run_id,
            task_id=context.task_id,
            sequence_no=self._sequence,
            event_type=event_type,
            occurred_at=self._clock(),
            idempotency_identity=identity,
            payload_schema_version=self._schema_version,
            trace_id=self._trace_id,
            payload=payload,
            tool_policy_hash=policy.content_hash.root,
        )
        persisted = self._repository.append(event)
        self._by_identity[identity] = persisted
