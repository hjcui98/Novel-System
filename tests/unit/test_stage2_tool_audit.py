from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import monotonic

import pytest
from sqlalchemy import create_engine

from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import RetrievalChannel
from novel_agent.domain.runtime import RunEventType
from novel_agent.domain.stage2 import (
    AccessScope,
    AgentMode,
    AgentType,
    ToolCallContext,
    ToolFailureCode,
    ToolPolicy,
    ToolResult,
    ToolResultStatus,
)
from novel_agent.services.event_log import RunEventLogRepository
from novel_agent.services.tool_audit import RunEventToolAuditSink, ToolAuditConflictError
from novel_agent.tools.contracts import (
    ToolBinding,
    ToolBindingError,
    ToolBudget,
    ToolInvocation,
)

RUN = RunId("run.tool-audit")
TASK = TaskId("task.tool-audit")
COMMIT = CommitId("sha256:" + "a" * 64)
HASH = ArtifactId("sha256:" + "b" * 64)
VERSION = SchemaVersion("2.0.0")
NOW = datetime(2026, 7, 21, tzinfo=UTC)


def context(call_id: str = "tool-call.1", *, read_only: bool = True) -> ToolCallContext:
    return ToolCallContext(
        tool_call_id=StableId(call_id),
        run_id=RUN,
        task_id=TASK,
        agent_type=AgentType.MEMORY_CONTROLLER,
        agent_mode=AgentMode.BOUNDED_R2,
        project_id=ProjectId("project.tool-audit"),
        base_commit=COMMIT,
        snapshot_id=StableId("snapshot.tool-audit"),
        worldline="main",
        narrative_chapter=20,
        access_scope=AccessScope.WRITER_SAFE,
        timeout_ms=1000,
        read_only=read_only,
    )


def policy() -> ToolPolicy:
    return ToolPolicy(
        policy_id=StableId("policy.tool-audit"),
        version=VERSION,
        content_hash=HASH,
        allowed_tools=("memory.test",),
        max_tool_calls=4,
    )


async def handler(tool_context: ToolCallContext, _: object) -> ToolResult:
    return ToolResult(
        tool_call_id=tool_context.tool_call_id,
        status=ToolResultStatus.SUCCEEDED,
        basis_commit=tool_context.base_commit,
        snapshot_id=tool_context.snapshot_id,
        payload={"secret": "result payload is not copied into RunEvent"},
        coverage=1,
        retrieval_channel=RetrievalChannel.ANCHOR_BM25,
        channel_candidate_count=3,
        audit_ref=StableId(f"audit.{tool_context.tool_call_id.root}"),
    )


def test_tool_binding_persists_replayable_request_and_result_events_without_raw_arguments() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    repository = RunEventLogRepository(build_session_factory(engine))
    sink = RunEventToolAuditSink(
        repository,
        RUN,
        trace_id="trace.tool-audit",
        schema_version=VERSION,
        clock=lambda: NOW,
    )
    binding = ToolBinding(policy(), {"memory.test": handler}, sink)
    invocation = ToolInvocation("memory.test", {"need_id": "need.private"})
    result = asyncio.run(binding.invoke(invocation, context(), ToolBudget(4, monotonic() + 10)))

    assert result.status is ToolResultStatus.SUCCEEDED
    events = repository.replay(RUN)
    assert tuple(event.event_type for event in events) == (
        RunEventType.TOOL_REQUESTED,
        RunEventType.TOOL_COMPLETED,
    )
    requested_payload = events[0].payload
    completed_payload = events[1].payload
    assert isinstance(requested_payload, dict)
    assert isinstance(completed_payload, dict)
    arguments_hash = requested_payload["arguments_hash"]
    assert isinstance(arguments_hash, str) and arguments_hash.startswith("sha256:")
    assert "need.private" not in events[0].model_dump_json()
    assert completed_payload["coverage"] == 1.0
    assert completed_payload["retrieval_channel"] == "anchor_bm25"
    assert completed_payload["channel_candidate_count"] == 3
    assert all(event.tool_policy_hash == HASH.root for event in events)

    asyncio.run(binding.invoke(invocation, context(), ToolBudget(4, monotonic() + 10)))
    restarted = RunEventToolAuditSink(
        repository,
        RUN,
        trace_id="trace.tool-audit",
        schema_version=VERSION,
        clock=lambda: NOW,
    )
    restarted.requested(context(), invocation, policy())
    assert len(repository.replay(RUN)) == 2
    engine.dispose()


def test_tool_audit_records_budget_and_rejected_attempts_and_rejects_identity_conflicts() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    repository = RunEventLogRepository(build_session_factory(engine))
    sink = RunEventToolAuditSink(
        repository,
        RUN,
        trace_id="trace.tool-audit",
        schema_version=VERSION,
        clock=lambda: NOW,
    )
    binding = ToolBinding(policy(), {"memory.test": handler}, sink)
    exhausted = asyncio.run(
        binding.invoke(
            ToolInvocation("memory.test", {}),
            context("tool-call.budget"),
            ToolBudget(0, monotonic() + 10),
        )
    )
    assert exhausted.failure_code is ToolFailureCode.BUDGET_EXCEEDED
    with pytest.raises(ToolBindingError, match="not allowed"):
        asyncio.run(
            binding.invoke(
                ToolInvocation("memory.forbidden", {}),
                context("tool-call.forbidden"),
                ToolBudget(4, monotonic() + 10),
            )
        )
    with pytest.raises(ToolBindingError, match="read-only"):
        asyncio.run(
            binding.invoke(
                ToolInvocation("memory.test", {}),
                context("tool-call.write", read_only=False),
                ToolBudget(4, monotonic() + 10),
            )
        )
    events = repository.replay(RUN)
    assert sum(event.event_type is RunEventType.TOOL_FAILED for event in events) == 3

    with pytest.raises(ToolAuditConflictError, match="another event"):
        sink.requested(
            context("tool-call.budget"),
            ToolInvocation("memory.test", {"changed": True}),
            policy(),
        )
    with pytest.raises(ToolAuditConflictError, match="another run"):
        sink.requested(
            context().model_copy(update={"run_id": RunId("run.other")}),
            ToolInvocation("memory.test", {}),
            policy(),
        )
    engine.dispose()
