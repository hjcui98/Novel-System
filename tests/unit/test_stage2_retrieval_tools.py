from __future__ import annotations

import asyncio

import pytest
from pydantic import JsonValue

from novel_agent.domain.ids import CommitId, ProjectId, RunId, StableId, TaskId
from novel_agent.domain.memory import (
    CandidatePool,
    ChannelHit,
    NeedRisk,
    RequirementLevel,
    ResolutionPath,
    RetrievalChannel,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1MemoryNeed,
    Stage1QueryIntent,
)
from novel_agent.domain.stage2 import (
    AccessScope,
    AgentMode,
    AgentType,
    ToolCallContext,
    ToolFailureCode,
    ToolResult,
    ToolResultStatus,
)
from novel_agent.tools.retrieval import CHANNEL_BY_TOOL, RetrievalToolAdapter

COMMIT = CommitId("sha256:" + "a" * 64)
OTHER_COMMIT = CommitId("sha256:" + "b" * 64)
SNAPSHOT = StableId("snapshot.1")
RUN = RunId("run.1")


def need(
    *,
    run_id: RunId = RUN,
    base_commit: CommitId = COMMIT,
    intent: Stage1QueryIntent = Stage1QueryIntent.CURRENT_STATE,
    pools: tuple[CandidatePool, ...] = tuple(CandidatePool),
    identity: str = "need.1",
) -> Stage1MemoryNeed:
    return Stage1MemoryNeed(
        need_id=StableId(identity),
        run_id=run_id,
        task_id=TaskId("task.1"),
        base_commit=base_commit,
        chapter_target=20,
        need_type="test",
        query_intent=intent,
        query_text="hero state",
        why_needed="test",
        risk_level=NeedRisk.HIGH,
        requirement=RequirementLevel.MANDATORY,
        preferred_resolution_path=ResolutionPath.EXACT_TEMPORAL,
        allowed_candidate_pools=pools,
        stop_condition="one result",
    )


def context(*, plan_permission: bool = False) -> ToolCallContext:
    return ToolCallContext(
        tool_call_id=StableId("call.1"),
        run_id=RunId("run.1"),
        task_id=TaskId("task.1"),
        agent_type=AgentType.MEMORY_CONTROLLER,
        agent_mode=AgentMode.BOUNDED_R2,
        project_id=ProjectId("project.1"),
        base_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        worldline="main",
        narrative_chapter=20,
        access_scope=AccessScope.WRITER_SAFE,
        plan_permission=plan_permission,
        timeout_ms=100,
    )


class Backend:
    def __init__(
        self,
        *,
        source_commit: CommitId = COMMIT,
        snapshot_id: StableId = SNAPSHOT,
        empty: bool = False,
    ) -> None:
        self.source_commit = source_commit
        self.snapshot_id = snapshot_id
        self.empty = empty
        self.calls: list[tuple[RetrievalChannel, int]] = []

    def search(
        self, memory_need: Stage1MemoryNeed, channel: RetrievalChannel, limit: int
    ) -> tuple[ChannelHit, ...]:
        self.calls.append((channel, limit))
        if self.empty:
            return ()
        unit = RetrievalUnit(
            unit_id=StableId(f"unit.{channel.value}"),
            unit_kind=RetrievalUnitKind.STATE_ANCHOR,
            source_commit=self.source_commit,
            snapshot_id=self.snapshot_id,
            text=memory_need.query_text,
        )
        return (
            ChannelHit(
                unit=unit,
                channel=channel,
                channel_rank=1,
                raw_score=1,
                candidate_count=1,
                hit_reason="test",
            ),
        )


def invoke(
    adapter: RetrievalToolAdapter,
    tool_name: str,
    arguments: JsonValue,
    trusted_context: ToolCallContext | None = None,
) -> ToolResult:
    handler = adapter.handlers()[tool_name]

    async def execute() -> ToolResult:
        return await handler(trusted_context or context(), arguments)

    return asyncio.run(execute())


def test_retrieval_adapter_exposes_all_typed_channels_and_clamps_limits() -> None:
    backend = Backend()
    adapter = RetrievalToolAdapter(backend, (need(),), max_limit=7)

    for tool_name, channel in CHANNEL_BY_TOOL.items():
        result = invoke(adapter, tool_name, {"need_id": "need.1", "limit": 100})
        assert result.status is ToolResultStatus.SUCCEEDED
        assert result.payload is not None
        assert result.coverage == 1
        assert result.retrieval_channel is channel
        assert result.channel_candidate_count == 1
        assert result.channel_failure_code is None
        assert backend.calls[-1] == (channel, 7)

    empty = RetrievalToolAdapter(Backend(empty=True), (need(),))
    assert invoke(empty, "memory.search_exact", {"need_id": "need.1"}).coverage == 0


@pytest.mark.parametrize("max_limit", [0, 101])
def test_retrieval_adapter_rejects_invalid_profiles(max_limit: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 100"):
        RetrievalToolAdapter(Backend(), (need(),), max_limit=max_limit)
    with pytest.raises(ValueError, match="unique"):
        RetrievalToolAdapter(Backend(), (need(), need()))


def test_retrieval_adapter_returns_distinct_query_scope_basis_and_access_failures() -> None:
    adapter = RetrievalToolAdapter(Backend(), (need(),))
    assert (
        invoke(adapter, "memory.search_exact", {"bad": "shape"}).failure_code
        is ToolFailureCode.INVALID_QUERY
    )
    assert (
        invoke(adapter, "memory.search_exact", {"need_id": "need.unknown"}).failure_code
        is ToolFailureCode.INVALID_QUERY
    )
    wrong_run = RetrievalToolAdapter(Backend(), (need(run_id=RunId("run.other")),))
    assert (
        invoke(wrong_run, "memory.search_exact", {"need_id": "need.1"}).failure_code
        is ToolFailureCode.SCOPE_MISMATCH
    )
    wrong_base = RetrievalToolAdapter(Backend(), (need(base_commit=OTHER_COMMIT),))
    assert (
        invoke(wrong_base, "memory.search_exact", {"need_id": "need.1"}).failure_code
        is ToolFailureCode.BASE_COMMIT_MISMATCH
    )
    plan = RetrievalToolAdapter(Backend(), (need(intent=Stage1QueryIntent.PLAN_OBLIGATION),))
    assert (
        invoke(plan, "memory.search_exact", {"need_id": "need.1"}).failure_code
        is ToolFailureCode.ACCESS_DENIED
    )
    assert (
        invoke(
            plan,
            "memory.search_exact",
            {"need_id": "need.1"},
            context(plan_permission=True),
        ).status
        is ToolResultStatus.SUCCEEDED
    )
    restricted = RetrievalToolAdapter(Backend(), (need(pools=(CandidatePool.ANCHOR,)),))
    assert (
        invoke(restricted, "memory.search_exact", {"need_id": "need.1"}).failure_code
        is ToolFailureCode.SCOPE_MISMATCH
    )


def test_retrieval_adapter_enforces_per_need_route_channel_allowlist() -> None:
    backend = Backend()
    adapter = RetrievalToolAdapter(
        backend,
        (need(),),
        allowed_channels_by_need={StableId("need.1"): (RetrievalChannel.R1_EXACT,)},
    )

    assert (
        invoke(adapter, "memory.search_anchor_bm25", {"need_id": "need.1"}).failure_code
        is ToolFailureCode.SCOPE_MISMATCH
    )
    assert backend.calls == []
    assert (
        invoke(adapter, "memory.search_exact", {"need_id": "need.1"}).status
        is ToolResultStatus.SUCCEEDED
    )
    with pytest.raises(ValueError, match="unknown memory need"):
        RetrievalToolAdapter(
            backend,
            (need(),),
            allowed_channels_by_need={StableId("need.unknown"): (RetrievalChannel.R1_EXACT,)},
        )


def test_retrieval_adapter_rejects_backend_results_from_another_basis() -> None:
    wrong_commit = RetrievalToolAdapter(Backend(source_commit=OTHER_COMMIT), (need(),))
    assert (
        invoke(wrong_commit, "memory.search_exact", {"need_id": "need.1"}).failure_code
        is ToolFailureCode.BASE_COMMIT_MISMATCH
    )
    wrong_snapshot = RetrievalToolAdapter(
        Backend(snapshot_id=StableId("snapshot.other")), (need(),)
    )
    assert (
        invoke(wrong_snapshot, "memory.search_exact", {"need_id": "need.1"}).failure_code
        is ToolFailureCode.SNAPSHOT_STALE
    )
