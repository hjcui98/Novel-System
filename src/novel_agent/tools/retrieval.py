"""Typed adapters exposing Stage 1 retrieval backends to bounded Stage 2 agents."""

from __future__ import annotations

from collections.abc import Mapping
from time import monotonic

from pydantic import JsonValue, ValidationError

from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import (
    CandidatePool,
    RetrievalChannel,
    Stage1MemoryNeed,
    Stage1QueryIntent,
)
from novel_agent.domain.retrieval_routing import ChannelFailureCode
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
        allowed_channels_by_need: Mapping[StableId, tuple[RetrievalChannel, ...]] | None = None,
    ) -> None:
        if max_limit < 1 or max_limit > 100:
            raise ValueError("retrieval tool max limit must be between 1 and 100")
        indexed = {need.need_id: need for need in needs}
        if len(indexed) != len(needs):
            raise ValueError("retrieval tool needs must have unique ids")
        self._backend = backend
        self._needs = indexed
        self._max_limit = max_limit
        self._allowed_channels_by_need = (
            {} if allowed_channels_by_need is None else dict(allowed_channels_by_need)
        )
        self._seen_unit_ids_by_scope: dict[tuple[str, str], set[StableId]] = {}
        if any(need_id not in indexed for need_id in self._allowed_channels_by_need):
            raise ValueError("retrieval tool route policy references an unknown memory need")

    def handlers(self) -> dict[str, ToolHandler]:
        return {name: self._handler(channel) for name, channel in CHANNEL_BY_TOOL.items()}

    def _handler(self, channel: RetrievalChannel) -> ToolHandler:
        async def search(context: ToolCallContext, arguments: JsonValue) -> ToolResult:
            try:
                query = MemoryToolQuery.model_validate(arguments, strict=True)
            except ValidationError:
                return self._failure(context, ToolFailureCode.INVALID_QUERY, channel)
            need = self._needs.get(query.need_id)
            if need is None:
                return self._failure(context, ToolFailureCode.INVALID_QUERY, channel)
            if need.run_id != context.run_id or need.task_id != context.task_id:
                return self._failure(context, ToolFailureCode.SCOPE_MISMATCH, channel)
            if need.base_commit != context.base_commit:
                return self._failure(context, ToolFailureCode.BASE_COMMIT_MISMATCH, channel)
            if need.access_scope != context.access_scope.value:
                return self._failure(context, ToolFailureCode.ACCESS_DENIED, channel)
            if (
                need.query_intent in PLAN_INTENTS or need.retrieval_may_return_plan
            ) and not context.plan_permission:
                return self._failure(context, ToolFailureCode.ACCESS_DENIED, channel)
            allowed_channels = self._allowed_channels_by_need.get(need.need_id)
            if allowed_channels is not None and channel not in allowed_channels:
                return self._failure(context, ToolFailureCode.SCOPE_MISMATCH, channel)
            if POOL_BY_CHANNEL[channel] not in need.allowed_candidate_pools:
                return self._failure(context, ToolFailureCode.SCOPE_MISMATCH, channel)
            limit = min(query.limit, self._max_limit)
            started = monotonic()
            hits = self._backend.search(need, channel, limit)
            latency_ms = round((monotonic() - started) * 1000)
            if any(hit.unit.source_commit != context.base_commit for hit in hits):
                return self._failure(context, ToolFailureCode.BASE_COMMIT_MISMATCH, channel)
            if context.snapshot_id is not None and any(
                hit.unit.snapshot_id != context.snapshot_id for hit in hits
            ):
                return self._failure(context, ToolFailureCode.SNAPSHOT_STALE, channel)
            scope_key = (context.run_id.root, context.task_id.root)
            seen = self._seen_unit_ids_by_scope.setdefault(scope_key, set())
            hit_ids = {hit.unit.unit_id for hit in hits}
            new_information_gain = len(hit_ids - seen)
            seen.update(hit_ids)
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
                query_variant=channel.value,
                backend_latency_ms=latency_ms,
                new_information_gain=new_information_gain,
                retrieval_channel=channel,
                channel_candidate_count=(hits[0].candidate_count if hits else 0),
                audit_ref=StableId(f"tool-audit.{context.tool_call_id.root}"),
            )

        return search

    @staticmethod
    def _failure(
        context: ToolCallContext,
        code: ToolFailureCode,
        channel: RetrievalChannel,
    ) -> ToolResult:
        channel_code = {
            ToolFailureCode.BASE_COMMIT_MISMATCH: ChannelFailureCode.BASIS_MISMATCH,
            ToolFailureCode.SNAPSHOT_STALE: ChannelFailureCode.STALE,
            ToolFailureCode.ACCESS_DENIED: ChannelFailureCode.FORBIDDEN,
            ToolFailureCode.SCOPE_MISMATCH: ChannelFailureCode.FORBIDDEN,
            ToolFailureCode.TIMEOUT: ChannelFailureCode.TIMEOUT,
            ToolFailureCode.BACKEND_UNAVAILABLE: ChannelFailureCode.BACKEND_UNAVAILABLE,
        }.get(code)
        return ToolResult(
            tool_call_id=context.tool_call_id,
            status=ToolResultStatus.FAILED,
            basis_commit=context.base_commit,
            snapshot_id=context.snapshot_id,
            retrieval_channel=channel,
            channel_failure_code=channel_code,
            failure_code=code,
            audit_ref=StableId(f"tool-audit.{context.tool_call_id.root}"),
        )
