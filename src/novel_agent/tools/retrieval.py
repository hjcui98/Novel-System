"""Typed adapters exposing Stage 1 retrieval backends to bounded Stage 2 agents."""

from __future__ import annotations

from pydantic import JsonValue, ValidationError

from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import (
    CandidatePool,
    RetrievalChannel,
    Stage1MemoryNeed,
    Stage1QueryIntent,
)
from novel_agent.domain.stage2 import (
    MemoryToolQuery,
    ToolCallContext,
    ToolFailureCode,
    ToolResult,
    ToolResultStatus,
)
from novel_agent.services.retrieval import RetrievalBackend
from novel_agent.tools.contracts import ToolHandler

CHANNEL_BY_TOOL: dict[str, RetrievalChannel] = {
    "memory.resolve_context_local": RetrievalChannel.R0,
    "memory.search_exact": RetrievalChannel.R1_EXACT,
    "memory.search_temporal": RetrievalChannel.R1_TEMPORAL,
    "memory.search_graph": RetrievalChannel.TYPED_GRAPH,
    "memory.search_anchor_bm25": RetrievalChannel.ANCHOR_BM25,
    "memory.search_anchor_dense": RetrievalChannel.ANCHOR_DENSE,
    "memory.search_grounded_bm25": RetrievalChannel.GROUNDED_BM25,
    "memory.search_grounded_dense": RetrievalChannel.GROUNDED_DENSE,
    "memory.search_hierarchy": RetrievalChannel.HIERARCHY,
}

POOL_BY_CHANNEL: dict[RetrievalChannel, CandidatePool] = {
    RetrievalChannel.R0: CandidatePool.R0,
    RetrievalChannel.R1_EXACT: CandidatePool.R1,
    RetrievalChannel.R1_TEMPORAL: CandidatePool.R1,
    RetrievalChannel.ANCHOR_BM25: CandidatePool.ANCHOR,
    RetrievalChannel.ANCHOR_DENSE: CandidatePool.ANCHOR,
    RetrievalChannel.GROUNDED_BM25: CandidatePool.GROUNDED,
    RetrievalChannel.GROUNDED_DENSE: CandidatePool.GROUNDED,
    RetrievalChannel.HIERARCHY: CandidatePool.HIERARCHY,
    RetrievalChannel.TYPED_GRAPH: CandidatePool.GRAPH,
}

PLAN_INTENTS = {
    Stage1QueryIntent.PLAN_NODE,
    Stage1QueryIntent.PLAN_OBLIGATION,
    Stage1QueryIntent.GLOBAL_ARC,
}


class RetrievalToolAdapter:
    """Binds immutable needs to trusted run scope; agent arguments contain no authority."""

    def __init__(
        self,
        backend: RetrievalBackend,
        needs: tuple[Stage1MemoryNeed, ...],
        *,
        max_limit: int = 20,
    ) -> None:
        if max_limit < 1 or max_limit > 100:
            raise ValueError("retrieval tool max limit must be between 1 and 100")
        indexed = {need.need_id: need for need in needs}
        if len(indexed) != len(needs):
            raise ValueError("retrieval tool needs must have unique ids")
        self._backend = backend
        self._needs = indexed
        self._max_limit = max_limit

    def handlers(self) -> dict[str, ToolHandler]:
        return {name: self._handler(channel) for name, channel in CHANNEL_BY_TOOL.items()}

    def _handler(self, channel: RetrievalChannel) -> ToolHandler:
        async def search(context: ToolCallContext, arguments: JsonValue) -> ToolResult:
            try:
                query = MemoryToolQuery.model_validate(arguments, strict=True)
            except ValidationError:
                return self._failure(context, ToolFailureCode.INVALID_QUERY)
            need = self._needs.get(query.need_id)
            if need is None:
                return self._failure(context, ToolFailureCode.INVALID_QUERY)
            if need.run_id != context.run_id or need.task_id != context.task_id:
                return self._failure(context, ToolFailureCode.SCOPE_MISMATCH)
            if need.base_commit != context.base_commit:
                return self._failure(context, ToolFailureCode.BASE_COMMIT_MISMATCH)
            if need.query_intent in PLAN_INTENTS and not context.plan_permission:
                return self._failure(context, ToolFailureCode.ACCESS_DENIED)
            if POOL_BY_CHANNEL[channel] not in need.allowed_candidate_pools:
                return self._failure(context, ToolFailureCode.SCOPE_MISMATCH)
            limit = min(query.limit, self._max_limit)
            hits = self._backend.search(need, channel, limit)
            if any(hit.unit.source_commit != context.base_commit for hit in hits):
                return self._failure(context, ToolFailureCode.BASE_COMMIT_MISMATCH)
            if context.snapshot_id is not None and any(
                hit.unit.snapshot_id != context.snapshot_id for hit in hits
            ):
                return self._failure(context, ToolFailureCode.SNAPSHOT_STALE)
            return ToolResult(
                tool_call_id=context.tool_call_id,
                status=ToolResultStatus.SUCCEEDED,
                basis_commit=context.base_commit,
                snapshot_id=context.snapshot_id,
                payload={
                    "channel": channel.value,
                    "hits": [hit.model_dump(mode="json") for hit in hits],
                },
                coverage=1 if hits else 0,
                audit_ref=StableId(f"tool-audit.{context.tool_call_id.root}"),
            )

        return search

    @staticmethod
    def _failure(context: ToolCallContext, code: ToolFailureCode) -> ToolResult:
        return ToolResult(
            tool_call_id=context.tool_call_id,
            status=ToolResultStatus.FAILED,
            basis_commit=context.base_commit,
            snapshot_id=context.snapshot_id,
            failure_code=code,
            audit_ref=StableId(f"tool-audit.{context.tool_call_id.root}"),
        )
