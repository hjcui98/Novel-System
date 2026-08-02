"""Deterministic Stage 1 query routing, typed retrieval, and application-owned RRF."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import (
    CandidatePool,
    ChannelHit,
    FusedCandidate,
    RetrievalChannel,
    RetrievalStopReason,
    RetrievalTrace,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1MemoryNeed,
    Stage1QueryIntent,
)

ANCHOR_KINDS = frozenset(
    {
        RetrievalUnitKind.FACT_ANCHOR,
        RetrievalUnitKind.STATE_ANCHOR,
        RetrievalUnitKind.RELATION_ANCHOR,
        RetrievalUnitKind.EVENT_ANCHOR,
        RetrievalUnitKind.SCENE_ANCHOR,
        RetrievalUnitKind.CHAPTER_ANCHOR,
        RetrievalUnitKind.ARC_ANCHOR,
        RetrievalUnitKind.PLAN_ANCHOR,
    }
)
GROUNDED_KINDS = frozenset({RetrievalUnitKind.GROUNDED_BLOCK, RetrievalUnitKind.GROUNDED_SPAN})

VISIBLE_SCOPES_BY_NEED_SCOPE = {
    "writer_safe": frozenset({"writer_safe"}),
    "author_planning": frozenset({"writer_safe", "author_planning"}),
    "evaluator": frozenset({"writer_safe", "author_planning", "evaluator"}),
}


@dataclass(frozen=True, slots=True)
class RouteDefinition:
    channels: tuple[RetrievalChannel, ...]
    fusion: bool
    fallback_channels: tuple[RetrievalChannel, ...] = ()


ROUTES: dict[Stage1QueryIntent, RouteDefinition] = {
    Stage1QueryIntent.CURRENT_STATE: RouteDefinition(
        (RetrievalChannel.R1_EXACT, RetrievalChannel.R1_TEMPORAL), False
    ),
    Stage1QueryIntent.KNOWN_ID: RouteDefinition((RetrievalChannel.R1_EXACT,), False),
    Stage1QueryIntent.PLAN_NODE: RouteDefinition((RetrievalChannel.R1_EXACT,), False),
    Stage1QueryIntent.MANDATORY_CONSTRAINT: RouteDefinition(
        (RetrievalChannel.R1_EXACT, RetrievalChannel.R1_TEMPORAL), False
    ),
    Stage1QueryIntent.SEMANTIC_HISTORY: RouteDefinition(
        (RetrievalChannel.ANCHOR_BM25, RetrievalChannel.ANCHOR_DENSE),
        True,
        (RetrievalChannel.GROUNDED_BM25, RetrievalChannel.GROUNDED_DENSE),
    ),
    Stage1QueryIntent.RELATED_EVENT: RouteDefinition(
        (RetrievalChannel.ANCHOR_BM25, RetrievalChannel.ANCHOR_DENSE),
        True,
        (RetrievalChannel.GROUNDED_BM25,),
    ),
    Stage1QueryIntent.PLAN_OBLIGATION: RouteDefinition(
        (RetrievalChannel.ANCHOR_BM25, RetrievalChannel.ANCHOR_DENSE), True
    ),
    Stage1QueryIntent.GLOBAL_ARC: RouteDefinition(
        (RetrievalChannel.HIERARCHY, RetrievalChannel.ANCHOR_BM25), True
    ),
    Stage1QueryIntent.CHAPTER_THREAD: RouteDefinition(
        (RetrievalChannel.HIERARCHY, RetrievalChannel.ANCHOR_BM25), True
    ),
    Stage1QueryIntent.CHARACTER_ARC: RouteDefinition(
        (RetrievalChannel.HIERARCHY, RetrievalChannel.ANCHOR_BM25), True
    ),
    Stage1QueryIntent.EXACT_QUOTE: RouteDefinition((RetrievalChannel.GROUNDED_BM25,), False),
    Stage1QueryIntent.RARE_PHRASE: RouteDefinition((RetrievalChannel.GROUNDED_BM25,), False),
    Stage1QueryIntent.STYLE_VOICE: RouteDefinition(
        (RetrievalChannel.GROUNDED_BM25, RetrievalChannel.GROUNDED_DENSE), True
    ),
    Stage1QueryIntent.DIALOGUE_SAMPLE: RouteDefinition(
        (RetrievalChannel.GROUNDED_BM25, RetrievalChannel.GROUNDED_DENSE), True
    ),
    Stage1QueryIntent.CAUSAL_MULTI_HOP: RouteDefinition((RetrievalChannel.TYPED_GRAPH,), False),
    Stage1QueryIntent.RELATION_CHAIN: RouteDefinition((RetrievalChannel.TYPED_GRAPH,), False),
    Stage1QueryIntent.ANCHOR_INSUFFICIENT: RouteDefinition(
        (RetrievalChannel.GROUNDED_BM25, RetrievalChannel.GROUNDED_DENSE), True
    ),
}


class RetrievalBackend(Protocol):
    def search(
        self,
        need: Stage1MemoryNeed,
        channel: RetrievalChannel,
        limit: int,
    ) -> tuple[ChannelHit, ...]: ...


class PassageReranker(Protocol):
    @property
    def profile(self) -> str: ...

    def score(self, query: str, passages: tuple[str, ...]) -> Sequence[float]: ...


class RerankService:
    def __init__(self, adapter: PassageReranker) -> None:
        self._adapter = adapter

    def rerank(
        self,
        need: Stage1MemoryNeed,
        candidates: tuple[FusedCandidate, ...],
        *,
        limit: int,
    ) -> tuple[FusedCandidate, ...]:
        if limit < 1:
            raise ValueError("rerank limit must be positive")
        eligible = tuple(
            candidate
            for candidate in candidates
            if candidate.selected and candidate.unit.unit_kind in ANCHOR_KINDS
        )
        if not eligible:
            return candidates
        scores = tuple(
            float(score)
            for score in self._adapter.score(
                need.query_text,
                tuple(candidate.unit.text for candidate in eligible),
            )
        )
        if len(scores) != len(eligible):
            raise ValueError("reranker score count does not match candidate count")
        ranked = sorted(
            zip(scores, eligible, strict=True),
            key=lambda item: (-item[0], item[1].unit.unit_id.root),
        )
        reranked: list[FusedCandidate] = []
        for rank, (score, candidate) in enumerate(ranked, start=1):
            rerank_hit = ChannelHit(
                unit=candidate.unit,
                channel=RetrievalChannel.RERANK,
                channel_rank=rank,
                raw_score=score,
                candidate_count=len(eligible),
                hit_reason=f"reranker:{self._adapter.profile}",
            )
            reranked.append(
                candidate.model_copy(
                    update={
                        "fused_rank": rank,
                        "channel_hits": (*candidate.channel_hits, rerank_hit),
                        "selected": rank <= limit or candidate.unit.mandatory,
                        "rejection_reason": (
                            None if rank <= limit or candidate.unit.mandatory else "rerank_limit"
                        ),
                    }
                )
            )
        # Keep non-anchor and rejected/over-limit candidates in the trace for diagnostics.
        reranked.extend(candidate for candidate in candidates if candidate not in eligible)
        return tuple(reranked)


@dataclass(frozen=True, slots=True)
class CandidateQuotaPolicy:
    """Typed diversity limits applied after ranking, never to mandatory units."""

    max_per_unit_kind: int = 8
    max_per_narrative_chapter: int = 4
    collapse_duplicate_evidence: bool = True

    def __post_init__(self) -> None:
        if self.max_per_unit_kind < 1 or self.max_per_narrative_chapter < 1:
            raise ValueError("candidate quota limits must be positive")


class TypedCandidateSelector:
    def __init__(self, policy: CandidateQuotaPolicy | None = None) -> None:
        self._policy = policy or CandidateQuotaPolicy()

    def select(
        self,
        candidates: tuple[FusedCandidate, ...],
        *,
        limit: int,
    ) -> tuple[FusedCandidate, ...]:
        if limit < 1:
            raise ValueError("candidate selection limit must be positive")
        selected_optional = 0
        kind_counts: dict[RetrievalUnitKind, int] = defaultdict(int)
        chapter_counts: dict[int, int] = defaultdict(int)
        evidence_seen: set[StableId] = set()
        output: list[FusedCandidate] = []
        for candidate in candidates:
            unit = candidate.unit
            evidence_ids = {item.evidence_id for item in unit.evidence_refs}
            chapter = (
                unit.narrative_start
                if unit.narrative_start is not None and unit.narrative_start == unit.narrative_end
                else None
            )
            reason: str | None = None
            if not unit.mandatory:
                if selected_optional >= limit:
                    reason = "optional_candidate_limit"
                elif kind_counts[unit.unit_kind] >= self._policy.max_per_unit_kind:
                    reason = "unit_kind_quota"
                elif (
                    chapter is not None
                    and chapter_counts[chapter] >= self._policy.max_per_narrative_chapter
                ):
                    reason = "narrative_chapter_quota"
                elif (
                    self._policy.collapse_duplicate_evidence
                    and evidence_ids
                    and evidence_ids.issubset(evidence_seen)
                ):
                    reason = "duplicate_evidence"
            selected = unit.mandatory or reason is None
            if selected:
                if not unit.mandatory:
                    selected_optional += 1
                kind_counts[unit.unit_kind] += 1
                if chapter is not None:
                    chapter_counts[chapter] += 1
                evidence_seen.update(evidence_ids)
            output.append(
                candidate.model_copy(
                    update={
                        "selected": selected,
                        "rejection_reason": None if selected else reason,
                    }
                )
            )
        return tuple(output)


class FusionService:
    """One deterministic RRF owner; input channels must still be independently ranked."""

    def __init__(
        self,
        *,
        rrf_k: int = 60,
        selector: TypedCandidateSelector | None = None,
    ) -> None:
        if rrf_k < 1:
            raise ValueError("rrf_k must be positive")
        self._rrf_k = rrf_k
        self._selector = selector or TypedCandidateSelector()

    def fuse(
        self,
        channel_results: dict[RetrievalChannel, tuple[ChannelHit, ...]],
        *,
        limit: int,
    ) -> tuple[FusedCandidate, ...]:
        if limit < 1:
            raise ValueError("fusion limit must be positive")
        grouped: dict[str, list[ChannelHit]] = defaultdict(list)
        units: dict[str, RetrievalUnit] = {}
        for channel, hits in channel_results.items():
            for expected_rank, hit in enumerate(hits, start=1):
                if hit.channel is not channel or hit.channel_rank != expected_rank:
                    raise ValueError("channel results must preserve an independent contiguous rank")
                if hit.candidate_count < len(hits):
                    raise ValueError("channel candidate_count cannot be smaller than returned hits")
                identity = hit.unit.unit_id.root
                grouped[identity].append(hit)
                units[identity] = hit.unit
        scored = sorted(
            (
                (
                    sum(1.0 / (self._rrf_k + hit.channel_rank) for hit in hits),
                    identity,
                    tuple(sorted(hits, key=lambda hit: hit.channel.value)),
                )
                for identity, hits in grouped.items()
            ),
            key=lambda item: (-item[0], item[1]),
        )
        candidates = tuple(
            FusedCandidate(
                unit=units[identity],
                fused_rank=rank,
                rrf_score=score,
                channel_hits=hits,
                selected=rank <= limit or units[identity].mandatory,
                rejection_reason=(
                    None if rank <= limit or units[identity].mandatory else "fusion_limit"
                ),
            )
            for rank, (score, identity, hits) in enumerate(scored, start=1)
        )
        return self._selector.select(candidates, limit=limit)


class RetrievalOrchestrator:
    def __init__(
        self,
        backend: RetrievalBackend,
        fusion: FusionService,
        *,
        per_channel_limit: int = 20,
        fused_limit: int = 20,
        reranker: RerankService | None = None,
    ) -> None:
        if per_channel_limit < 1 or fused_limit < 1:
            raise ValueError("retrieval limits must be positive")
        self._backend = backend
        self._fusion = fusion
        self._per_channel_limit = per_channel_limit
        self._fused_limit = fused_limit
        self._reranker = reranker

    def retrieve(self, need: Stage1MemoryNeed) -> RetrievalTrace:
        route = ROUTES[need.query_intent]
        primary_channels = tuple(
            channel
            for channel in route.channels
            if _pool_for_channel(channel) in need.allowed_candidate_pools
        )
        if not primary_channels:
            raise ValueError("memory need candidate pools forbid every channel for its intent")
        primary = self._run_channels(need, primary_channels)
        candidates = self._combine(primary, fusion=route.fusion)
        rerank_used = (
            route.fusion
            and self._reranker is not None
            and any(
                candidate.selected and candidate.unit.unit_kind in ANCHOR_KINDS
                for candidate in candidates
            )
        )
        if rerank_used:
            assert self._reranker is not None
            candidates = self._reranker.rerank(need, candidates, limit=self._fused_limit)
        selected = tuple(candidate for candidate in candidates if candidate.selected)
        fallback_used = False
        fallback_reason: str | None = None
        all_results = dict(primary)
        fallback_channels = tuple(
            channel
            for channel in route.fallback_channels
            if _pool_for_channel(channel) in need.allowed_candidate_pools
        )
        if not selected and fallback_channels:
            fallback_used = True
            fallback_reason = "primary_anchor_candidates_empty"
            fallback = self._run_channels(need, fallback_channels)
            all_results.update(fallback)
            candidates = self._combine(fallback, fusion=len(fallback) > 1)
            selected = tuple(candidate for candidate in candidates if candidate.selected)
        stop_reason = (
            RetrievalStopReason.EXACT_SATISFIED
            if selected and not route.fusion and not fallback_used
            else RetrievalStopReason.BUDGET_SATISFIED
            if selected
            else RetrievalStopReason.FALLBACK_EXHAUSTED
            if fallback_used
            else RetrievalStopReason.CANDIDATES_EXHAUSTED
        )
        channels = (
            *primary_channels,
            *((RetrievalChannel.RERANK,) if rerank_used else ()),
            *(fallback_channels if fallback_used else ()),
        )
        return RetrievalTrace(
            need_id=need.need_id,
            intent=need.query_intent,
            allowed_channels=channels,
            channel_candidate_counts={
                channel: hits[0].candidate_count if hits else 0
                for channel, hits in all_results.items()
            },
            candidates=candidates,
            fusion_applied=route.fusion or (fallback_used and len(fallback_channels) > 1),
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            stop_reason=stop_reason,
        )

    def _run_channels(
        self,
        need: Stage1MemoryNeed,
        channels: tuple[RetrievalChannel, ...],
    ) -> dict[RetrievalChannel, tuple[ChannelHit, ...]]:
        return {
            channel: self._backend.search(need, channel, self._per_channel_limit)
            for channel in channels
        }

    def _combine(
        self,
        results: dict[RetrievalChannel, tuple[ChannelHit, ...]],
        *,
        fusion: bool,
    ) -> tuple[FusedCandidate, ...]:
        if fusion:
            return self._fusion.fuse(results, limit=self._fused_limit)
        hits: list[ChannelHit] = []
        seen: set[str] = set()
        for channel_hits in results.values():
            for hit in channel_hits:
                if hit.unit.unit_id.root not in seen:
                    seen.add(hit.unit.unit_id.root)
                    hits.append(hit)
        return tuple(
            FusedCandidate(
                unit=hit.unit,
                fused_rank=rank,
                rrf_score=1.0 / rank,
                channel_hits=(hit,),
                selected=rank <= self._fused_limit or hit.unit.mandatory,
                rejection_reason=(
                    None
                    if rank <= self._fused_limit or hit.unit.mandatory
                    else "direct_result_limit"
                ),
            )
            for rank, hit in enumerate(hits, start=1)
        )


class InMemoryRetrievalBackend:
    """Deterministic smoke backend; production adapters implement the same typed contract."""

    def __init__(self, units: tuple[RetrievalUnit, ...]) -> None:
        if len({unit.unit_id for unit in units}) != len(units):
            raise ValueError("retrieval unit ids must be unique")
        self._units = units

    def search(
        self,
        need: Stage1MemoryNeed,
        channel: RetrievalChannel,
        limit: int,
    ) -> tuple[ChannelHit, ...]:
        allowed_kinds = self._allowed_kinds(channel)
        visible_scopes = VISIBLE_SCOPES_BY_NEED_SCOPE.get(need.access_scope)
        if visible_scopes is None:
            return ()
        candidates = [
            unit
            for unit in self._units
            if unit.unit_kind in allowed_kinds and unit.access_scope in visible_scopes
        ]
        scored = [
            (self._score(need, unit, channel), unit)
            for unit in candidates
            if self._score(need, unit, channel) > 0
        ]
        scored.sort(key=lambda item: (-item[0], item[1].unit_id.root))
        return tuple(
            ChannelHit(
                unit=unit,
                channel=channel,
                channel_rank=rank,
                raw_score=score,
                candidate_count=len(scored),
                hit_reason=self._hit_reason(channel),
            )
            for rank, (score, unit) in enumerate(scored[:limit], start=1)
        )

    @staticmethod
    def _allowed_kinds(channel: RetrievalChannel) -> frozenset[RetrievalUnitKind]:
        if channel in {
            RetrievalChannel.GROUNDED_BM25,
            RetrievalChannel.GROUNDED_DENSE,
        }:
            return GROUNDED_KINDS
        return ANCHOR_KINDS

    @staticmethod
    def _score(
        need: Stage1MemoryNeed,
        unit: RetrievalUnit,
        channel: RetrievalChannel,
    ) -> float:
        entity_overlap = len(set(need.entity_ids).intersection(unit.entity_ids))
        query_terms = _terms(need.query_text)
        unit_terms = _terms(unit.text)
        overlap = len(query_terms.intersection(unit_terms))
        if channel in {RetrievalChannel.R1_EXACT, RetrievalChannel.R1_TEMPORAL}:
            return float(entity_overlap * 10 + overlap)
        if channel is RetrievalChannel.TYPED_GRAPH:
            return float(entity_overlap)
        if channel is RetrievalChannel.HIERARCHY:
            return float(overlap + (1 if unit.parent_unit_id is not None else 0))
        if channel in {RetrievalChannel.ANCHOR_DENSE, RetrievalChannel.GROUNDED_DENSE}:
            union = len(query_terms.union(unit_terms))
            return overlap / union if union else 0.0
        return float(overlap)

    @staticmethod
    def _hit_reason(channel: RetrievalChannel) -> str:
        return {
            RetrievalChannel.R1_EXACT: "entity_or_predicate_exact_match",
            RetrievalChannel.R1_TEMPORAL: "current_temporal_record_match",
            RetrievalChannel.ANCHOR_BM25: "anchor_lexical_match",
            RetrievalChannel.ANCHOR_DENSE: "anchor_semantic_smoke_match",
            RetrievalChannel.GROUNDED_BM25: "grounded_lexical_match",
            RetrievalChannel.GROUNDED_DENSE: "grounded_semantic_smoke_match",
            RetrievalChannel.HIERARCHY: "hierarchy_parent_or_text_match",
            RetrievalChannel.TYPED_GRAPH: "typed_entity_edge_match",
            RetrievalChannel.R0: "task_context_match",
            RetrievalChannel.RERANK: "reranker_match",
        }[channel]


def _terms(text: str) -> set[str]:
    normalized = text.casefold()
    words = set(re.findall(r"[a-z0-9_]+", normalized))
    han_runs = re.findall(r"[\u3400-\u9fff]+", normalized)
    for run in han_runs:
        words.update(run[index : index + 2] for index in range(max(1, len(run) - 1)))
        words.update(run)
    return words


def _pool_for_channel(channel: RetrievalChannel) -> CandidatePool:
    if channel is RetrievalChannel.R0:
        return CandidatePool.R0
    if channel in {RetrievalChannel.R1_EXACT, RetrievalChannel.R1_TEMPORAL}:
        return CandidatePool.R1
    if channel in {RetrievalChannel.ANCHOR_BM25, RetrievalChannel.ANCHOR_DENSE}:
        return CandidatePool.ANCHOR
    if channel in {RetrievalChannel.GROUNDED_BM25, RetrievalChannel.GROUNDED_DENSE}:
        return CandidatePool.GROUNDED
    if channel is RetrievalChannel.HIERARCHY:
        return CandidatePool.HIERARCHY
    if channel is RetrievalChannel.TYPED_GRAPH:
        return CandidatePool.GRAPH
    return CandidatePool.ANCHOR
