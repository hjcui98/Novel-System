from __future__ import annotations

import pytest

from novel_agent.domain.ids import CommitId, RunId, StableId, TaskId
from novel_agent.domain.memory import (
    CandidatePool,
    ChannelHit,
    NeedRisk,
    RequirementLevel,
    ResolutionPath,
    RetrievalChannel,
    RetrievalStopReason,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1MemoryNeed,
    Stage1QueryIntent,
)
from novel_agent.services.retrieval import (
    FusionService,
    InMemoryRetrievalBackend,
    RerankService,
    RetrievalOrchestrator,
    _pool_for_channel,
)

COMMIT = CommitId("sha256:" + "a" * 64)
SNAPSHOT = StableId("snapshot.test")
CHARACTER = StableId("entity.lin-che")


def unit(
    identity: str,
    kind: RetrievalUnitKind,
    text: str,
    *,
    entity_ids: tuple[StableId, ...] = (),
) -> RetrievalUnit:
    return RetrievalUnit(
        unit_id=StableId(identity),
        unit_kind=kind,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        text=text,
        entity_ids=entity_ids,
    )


def need(
    intent: Stage1QueryIntent,
    query: str,
    pools: tuple[CandidatePool, ...],
    *,
    entity_ids: tuple[StableId, ...] = (),
) -> Stage1MemoryNeed:
    return Stage1MemoryNeed(
        need_id=StableId(f"need.{intent.value}"),
        run_id=RunId("run.stage1.test"),
        task_id=TaskId("task.stage1.test"),
        base_commit=COMMIT,
        chapter_target=21,
        need_type="test",
        query_intent=intent,
        query_text=query,
        entity_ids=entity_ids,
        why_needed="synthetic retrieval contract",
        risk_level=NeedRisk.HIGH,
        requirement=RequirementLevel.MANDATORY,
        preferred_resolution_path=(
            ResolutionPath.EXACT_TEMPORAL
            if CandidatePool.R1 in pools
            else ResolutionPath.ANCHOR_FIRST
        ),
        allowed_candidate_pools=pools,
        stop_condition="one supported result",
    )


def orchestrator() -> RetrievalOrchestrator:
    units = (
        unit(
            "anchor.state.injury",
            RetrievalUnitKind.STATE_ANCHOR,
            "林澈 当前 受伤 仍未痊愈",
            entity_ids=(CHARACTER,),
        ),
        unit(
            "anchor.event.promise",
            RetrievalUnitKind.EVENT_ANCHOR,
            "林澈 记得 旧誓言 并计划北行",
            entity_ids=(CHARACTER,),
        ),
        unit(
            "grounded.block.promise",
            RetrievalUnitKind.GROUNDED_BLOCK,
            "林澈在山路上再次想起旧誓言。",
            entity_ids=(CHARACTER,),
        ),
        unit(
            "grounded.block.rare",
            RetrievalUnitKind.GROUNDED_BLOCK,
            "石门刻着罕见短语星落无声。",
        ),
    )
    return RetrievalOrchestrator(InMemoryRetrievalBackend(units), FusionService())


class _ReverseReranker:
    profile = "reverse-test-v1"

    def score(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        return tuple(float(index) for index, _ in enumerate(passages, start=1))


class _BadReranker:
    profile = "bad-test-v1"

    def score(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        return ()


def test_exact_current_state_bypasses_anchor_and_rrf() -> None:
    trace = orchestrator().retrieve(
        need(
            Stage1QueryIntent.CURRENT_STATE,
            "林澈 受伤",
            (CandidatePool.R1,),
            entity_ids=(CHARACTER,),
        )
    )

    assert trace.allowed_channels == (
        RetrievalChannel.R1_EXACT,
        RetrievalChannel.R1_TEMPORAL,
    )
    assert trace.fusion_applied is False
    assert trace.stop_reason is RetrievalStopReason.EXACT_SATISFIED
    assert trace.candidates[0].unit.unit_id == StableId("anchor.state.injury")
    assert all(
        hit.channel in {RetrievalChannel.R1_EXACT, RetrievalChannel.R1_TEMPORAL}
        for candidate in trace.candidates
        for hit in candidate.channel_hits
    )


def test_semantic_history_is_anchor_first_with_application_rrf_diagnostics() -> None:
    trace = orchestrator().retrieve(
        need(
            Stage1QueryIntent.SEMANTIC_HISTORY,
            "旧誓言 北行",
            (CandidatePool.ANCHOR, CandidatePool.GROUNDED),
        )
    )

    assert trace.allowed_channels == (
        RetrievalChannel.ANCHOR_BM25,
        RetrievalChannel.ANCHOR_DENSE,
    )
    assert trace.fusion_applied is True
    assert trace.fallback_used is False
    assert trace.candidates[0].unit.unit_kind is RetrievalUnitKind.EVENT_ANCHOR
    assert {hit.channel for hit in trace.candidates[0].channel_hits} == {
        RetrievalChannel.ANCHOR_BM25,
        RetrievalChannel.ANCHOR_DENSE,
    }
    assert all(hit.channel_rank >= 1 for hit in trace.candidates[0].channel_hits)


def test_optional_anchor_reranker_runs_after_rrf_and_preserves_diagnostics() -> None:
    base = orchestrator()
    reranked = RetrievalOrchestrator(
        base._backend,
        FusionService(),
        reranker=RerankService(_ReverseReranker()),
    )
    trace = reranked.retrieve(
        need(
            Stage1QueryIntent.SEMANTIC_HISTORY,
            "林澈",
            (CandidatePool.ANCHOR,),
        )
    )

    assert RetrievalChannel.RERANK in trace.allowed_channels
    assert trace.candidates[0].unit.unit_id == StableId("anchor.state.injury")
    rerank_hit = trace.candidates[0].channel_hits[-1]
    assert rerank_hit.channel is RetrievalChannel.RERANK
    assert rerank_hit.hit_reason == "reranker:reverse-test-v1"


def test_rerank_service_fails_closed_and_only_reranks_selected_anchors() -> None:
    anchor_one = unit("anchor.rerank.one", RetrievalUnitKind.EVENT_ANCHOR, "one")
    anchor_two = unit("anchor.rerank.two", RetrievalUnitKind.EVENT_ANCHOR, "two")
    grounded = unit("grounded.rerank", RetrievalUnitKind.GROUNDED_BLOCK, "raw")

    def candidate(item: RetrievalUnit, rank: int) -> object:
        hit = ChannelHit(
            unit=item,
            channel=RetrievalChannel.ANCHOR_BM25,
            channel_rank=rank,
            raw_score=1.0,
            candidate_count=3,
            hit_reason="test",
        )
        from novel_agent.domain.memory import FusedCandidate

        return FusedCandidate(
            unit=item,
            fused_rank=rank,
            rrf_score=1.0 / rank,
            channel_hits=(hit,),
        )

    candidates = (
        candidate(anchor_one, 1),
        candidate(anchor_two, 2),
    )
    from typing import cast

    from novel_agent.domain.memory import FusedCandidate

    typed = cast(tuple[FusedCandidate, ...], candidates)
    service = RerankService(_ReverseReranker())
    result = service.rerank(
        need(Stage1QueryIntent.SEMANTIC_HISTORY, "query", (CandidatePool.ANCHOR,)),
        typed,
        limit=1,
    )
    assert result[0].unit == anchor_two
    assert result[0].selected is True
    assert result[1].selected is False and result[1].rejection_reason == "rerank_limit"
    grounded_candidate = cast(FusedCandidate, candidate(grounded, 1))
    assert service.rerank(
        need(Stage1QueryIntent.STYLE_VOICE, "query", (CandidatePool.GROUNDED,)),
        (grounded_candidate,),
        limit=1,
    ) == (grounded_candidate,)
    with pytest.raises(ValueError, match="rerank limit"):
        service.rerank(
            need(Stage1QueryIntent.SEMANTIC_HISTORY, "query", (CandidatePool.ANCHOR,)),
            typed,
            limit=0,
        )
    with pytest.raises(ValueError, match="score count"):
        RerankService(_BadReranker()).rerank(
            need(Stage1QueryIntent.SEMANTIC_HISTORY, "query", (CandidatePool.ANCHOR,)),
            typed,
            limit=2,
        )


def test_exact_quote_routes_directly_to_grounded_pool() -> None:
    trace = orchestrator().retrieve(
        need(
            Stage1QueryIntent.EXACT_QUOTE,
            "星落无声",
            (CandidatePool.GROUNDED,),
        )
    )

    assert trace.allowed_channels == (RetrievalChannel.GROUNDED_BM25,)
    assert trace.fusion_applied is False
    assert trace.candidates[0].unit.unit_kind is RetrievalUnitKind.GROUNDED_BLOCK
    assert trace.candidates[0].unit.unit_id == StableId("grounded.block.rare")


def test_anchor_empty_uses_one_bounded_grounded_fallback() -> None:
    trace = orchestrator().retrieve(
        need(
            Stage1QueryIntent.SEMANTIC_HISTORY,
            "星落无声",
            (CandidatePool.ANCHOR, CandidatePool.GROUNDED),
        )
    )

    assert trace.fallback_used is True
    assert trace.fallback_reason == "primary_anchor_candidates_empty"
    assert trace.allowed_channels[-2:] == (
        RetrievalChannel.GROUNDED_BM25,
        RetrievalChannel.GROUNDED_DENSE,
    )
    assert trace.candidates[0].unit.unit_id == StableId("grounded.block.rare")


def test_need_cannot_silently_use_a_forbidden_candidate_pool() -> None:
    semantic_need = need(
        Stage1QueryIntent.SEMANTIC_HISTORY,
        "旧誓言",
        (CandidatePool.R1,),
    )

    try:
        orchestrator().retrieve(semantic_need)
    except ValueError as error:
        assert str(error) == "memory need candidate pools forbid every channel for its intent"
    else:
        raise AssertionError("forbidden route unexpectedly executed")


def test_fusion_and_backend_reject_malformed_inputs() -> None:
    with pytest.raises(ValueError, match="rrf_k"):
        FusionService(rrf_k=0)
    with pytest.raises(ValueError, match="fusion limit"):
        FusionService().fuse({}, limit=0)
    with pytest.raises(ValueError, match="retrieval limits"):
        RetrievalOrchestrator(InMemoryRetrievalBackend(()), FusionService(), fused_limit=0)
    duplicate = unit("anchor.duplicate", RetrievalUnitKind.FACT_ANCHOR, "duplicate")
    with pytest.raises(ValueError, match="unit ids"):
        InMemoryRetrievalBackend((duplicate, duplicate))

    hit = ChannelHit(
        unit=duplicate,
        channel=RetrievalChannel.ANCHOR_BM25,
        channel_rank=2,
        raw_score=1.0,
        candidate_count=1,
        hit_reason="malformed",
    )
    with pytest.raises(ValueError, match="contiguous rank"):
        FusionService().fuse({RetrievalChannel.ANCHOR_BM25: (hit,)}, limit=1)
    wrong_count = hit.model_copy(update={"channel_rank": 1, "candidate_count": 0})
    with pytest.raises(ValueError, match="candidate_count"):
        FusionService().fuse({RetrievalChannel.ANCHOR_BM25: (wrong_count,)}, limit=1)


def test_hierarchy_graph_empty_dense_and_pool_mapping_paths() -> None:
    parented = unit(
        "anchor.parented",
        RetrievalUnitKind.ARC_ANCHOR,
        "林澈 北塔",
        entity_ids=(CHARACTER,),
    ).model_copy(update={"parent_unit_id": StableId("anchor.parent")})
    punctuation = unit("anchor.punctuation", RetrievalUnitKind.FACT_ANCHOR, "!!!")
    backend = InMemoryRetrievalBackend((parented, punctuation))

    hierarchy = backend.search(
        need(
            Stage1QueryIntent.GLOBAL_ARC,
            "北塔",
            (CandidatePool.HIERARCHY,),
        ),
        RetrievalChannel.HIERARCHY,
        5,
    )
    graph = backend.search(
        need(
            Stage1QueryIntent.RELATION_CHAIN,
            "林澈",
            (CandidatePool.GRAPH,),
            entity_ids=(CHARACTER,),
        ),
        RetrievalChannel.TYPED_GRAPH,
        5,
    )
    empty_dense = backend.search(
        need(
            Stage1QueryIntent.SEMANTIC_HISTORY,
            "!!!",
            (CandidatePool.ANCHOR,),
        ),
        RetrievalChannel.ANCHOR_DENSE,
        5,
    )
    assert hierarchy[0].hit_reason == "hierarchy_parent_or_text_match"
    assert graph[0].hit_reason == "typed_entity_edge_match"
    assert empty_dense == ()
    assert backend._hit_reason(RetrievalChannel.R0) == "task_context_match"
    assert backend._hit_reason(RetrievalChannel.RERANK) == "reranker_match"
    assert _pool_for_channel(RetrievalChannel.R0) is CandidatePool.R0
    assert _pool_for_channel(RetrievalChannel.HIERARCHY) is CandidatePool.HIERARCHY
    assert _pool_for_channel(RetrievalChannel.TYPED_GRAPH) is CandidatePool.GRAPH
    assert _pool_for_channel(RetrievalChannel.RERANK) is CandidatePool.ANCHOR


def test_empty_primary_and_fallback_report_explicit_stop_reasons() -> None:
    empty = RetrievalOrchestrator(InMemoryRetrievalBackend(()), FusionService())
    exhausted = empty.retrieve(
        need(
            Stage1QueryIntent.PLAN_OBLIGATION,
            "missing",
            (CandidatePool.ANCHOR,),
        )
    )
    fallback_exhausted = empty.retrieve(
        need(
            Stage1QueryIntent.SEMANTIC_HISTORY,
            "missing",
            (CandidatePool.ANCHOR, CandidatePool.GROUNDED),
        )
    )
    assert exhausted.stop_reason is RetrievalStopReason.CANDIDATES_EXHAUSTED
    assert fallback_exhausted.stop_reason is RetrievalStopReason.FALLBACK_EXHAUSTED
