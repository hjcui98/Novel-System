from __future__ import annotations

from novel_agent.domain.ids import ArtifactId, CommitId, StableId
from novel_agent.domain.memory import (
    ChannelHit,
    FusedCandidate,
    GraphPathDereferenceStatus,
    GraphPathReceipt,
    RetrievalChannel,
    RetrievalUnit,
    RetrievalUnitKind,
)
from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextSpanRef
from novel_agent.domain.world import StoryTime, TruthClass
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import quote_hash
from novel_agent.services.retrieval import FusionService, retain_related_graph_paths

COMMIT = CommitId("sha256:" + "a" * 64)
SNAPSHOT = StableId("snapshot.r3")
SEED_A = StableId("entity.seed.a")
SEED_B = StableId("entity.seed.b")
OTHER = StableId("entity.other")


def _evidence() -> EvidenceRef:
    text = "甲与乙结盟。"
    return EvidenceRef(
        evidence_id=StableId("evidence.r3"),
        root_hash=ArtifactId("sha256:" + "c" * 64),
        object_hash=sha256_id(text.encode("utf-8")),
        chapter_id=StableId("chapter.r3.1"),
        span=TextSpanRef(block_id=StableId("block.r3.1"), start=0, end=len(text)),
        quote_hash=quote_hash(text),
        support_status=EvidenceSupportStatus.CURRENT,
        resolved_at_commit=COMMIT,
    )


def _path(*, path: str, seeds: tuple[StableId, ...], other: StableId) -> GraphPathReceipt:
    return GraphPathReceipt(
        path_id=StableId(f"graph-path.{path}"),
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        seed_entity_ids=seeds,
        relation_row_ids=(StableId(f"row.{path}"),),
        relation_ids=(StableId(f"relation.{path}"),),
        entity_path=(seeds[0], other),
        predicates=("related_to",),
        directions=("forward",),
        valid_time=(StoryTime(worldline="main", start_ordinal=1),),
        edge_semantics=("canonical",),
        evidence_refs=(_evidence(),),
        dereference_status=GraphPathDereferenceStatus.RELATION_ROWS_VERIFIED,
    )


def _unit(
    identity: str,
    *,
    kind: RetrievalUnitKind,
    entities: tuple[StableId, ...],
    truth: TruthClass = TruthClass.ACCEPTED_WORLD_FACT,
) -> RetrievalUnit:
    return RetrievalUnit(
        unit_id=StableId(identity),
        unit_kind=kind,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        text=identity,
        entity_ids=entities,
        truth_class=truth,
        evidence_refs=(_evidence(),),
    )


def _hit(
    unit: RetrievalUnit,
    channel: RetrievalChannel,
    rank: int,
    count: int,
    paths: tuple[GraphPathReceipt, ...] = (),
) -> ChannelHit:
    return ChannelHit(
        unit=unit,
        channel=channel,
        channel_rank=rank,
        raw_score=float(count - rank + 1),
        candidate_count=count,
        hit_reason="test",
        graph_path_receipts=paths,
    )


def test_related_graph_path_survives_anchor_score_mass() -> None:
    anchors = tuple(
        _unit(
            f"anchor.{index}",
            kind=RetrievalUnitKind.FACT_ANCHOR,
            entities=(SEED_A,),
        )
        for index in range(22)
    )
    related_unit = _unit(
        "graph.related",
        kind=RetrievalUnitKind.RELATION_ANCHOR,
        entities=(SEED_A, SEED_B),
    )
    unrelated_unit = _unit(
        "graph.unrelated",
        kind=RetrievalUnitKind.RELATION_ANCHOR,
        entities=(OTHER,),
    )
    related_path = _path(path="related", seeds=(SEED_A,), other=SEED_B)
    unrelated_path = _path(path="unrelated", seeds=(OTHER,), other=StableId("entity.stranger"))
    bm25 = tuple(
        _hit(unit, RetrievalChannel.ANCHOR_BM25, rank, 22)
        for rank, unit in enumerate(anchors, start=1)
    )
    dense = tuple(
        _hit(unit, RetrievalChannel.ANCHOR_DENSE, rank, 22)
        for rank, unit in enumerate(anchors, start=1)
    )
    graph = (
        _hit(related_unit, RetrievalChannel.TYPED_GRAPH, 1, 2, (related_path,)),
        _hit(unrelated_unit, RetrievalChannel.TYPED_GRAPH, 2, 2, (unrelated_path,)),
    )
    selected = FusionService().fuse(
        {
            RetrievalChannel.ANCHOR_BM25: bm25,
            RetrievalChannel.ANCHOR_DENSE: dense,
            RetrievalChannel.TYPED_GRAPH: graph,
        },
        limit=20,
        seed_entity_ids=(SEED_A, SEED_B),
    )
    selected_ids = {candidate.unit.unit_id for candidate in selected if candidate.selected}
    assert related_unit.unit_id in selected_ids
    assert unrelated_unit.unit_id not in selected_ids
    assert any(
        candidate.selected and candidate.unit.truth_class is TruthClass.ACCEPTED_WORLD_FACT
        for candidate in selected
        if candidate.unit.unit_id == related_unit.unit_id
    )


def test_graph_exhausted_when_no_admissible_related_path() -> None:
    anchors = tuple(
        _unit(f"anchor.{index}", kind=RetrievalUnitKind.FACT_ANCHOR, entities=(SEED_A,))
        for index in range(8)
    )
    unrelated_unit = _unit(
        "graph.unrelated",
        kind=RetrievalUnitKind.RELATION_ANCHOR,
        entities=(OTHER,),
    )
    unrelated_path = _path(path="only-unrelated", seeds=(OTHER,), other=StableId("entity.x"))
    fused = FusionService().fuse(
        {
            RetrievalChannel.ANCHOR_BM25: tuple(
                _hit(unit, RetrievalChannel.ANCHOR_BM25, rank, 8)
                for rank, unit in enumerate(anchors, start=1)
            ),
            RetrievalChannel.TYPED_GRAPH: (
                _hit(unrelated_unit, RetrievalChannel.TYPED_GRAPH, 1, 1, (unrelated_path,)),
            ),
        },
        limit=8,
        seed_entity_ids=(SEED_A,),
    )
    retained = retain_related_graph_paths(fused, seed_entity_ids=(SEED_A,))
    assert not any(
        candidate.selected and candidate.unit.unit_id == unrelated_unit.unit_id
        for candidate in retained
    )
    related_selected = [
        candidate
        for candidate in retained
        if candidate.selected and _has_related_path(candidate, {SEED_A})
    ]
    assert related_selected == []


def test_single_seed_graph_path_is_not_retained_for_two_seed_need() -> None:
    anchor = _unit("anchor.seed-a", kind=RetrievalUnitKind.FACT_ANCHOR, entities=(SEED_A,))
    partial_unit = _unit(
        "graph.partial-seed",
        kind=RetrievalUnitKind.RELATION_ANCHOR,
        entities=(SEED_A, OTHER),
    )
    partial_path = _path(path="partial-seed", seeds=(SEED_A,), other=OTHER)
    fused = FusionService().fuse(
        {
            RetrievalChannel.ANCHOR_BM25: (_hit(anchor, RetrievalChannel.ANCHOR_BM25, 1, 1),),
            RetrievalChannel.ANCHOR_DENSE: (_hit(anchor, RetrievalChannel.ANCHOR_DENSE, 1, 1),),
            RetrievalChannel.TYPED_GRAPH: (
                _hit(partial_unit, RetrievalChannel.TYPED_GRAPH, 1, 1, (partial_path,)),
            ),
        },
        limit=1,
        seed_entity_ids=(SEED_A, SEED_B),
    )

    partial = next(
        candidate for candidate in fused if candidate.unit.unit_id == partial_unit.unit_id
    )
    assert partial.selected is False
    assert partial.rejection_reason == "optional_candidate_limit"


def _has_related_path(candidate: FusedCandidate, seeds: set[StableId]) -> bool:
    for hit in candidate.channel_hits:
        for receipt in hit.graph_path_receipts:
            if seeds.intersection(receipt.entity_path):
                return True
    return False


def test_off_seed_graph_hops_do_not_occupy_selected_set() -> None:
    """2-hop landings whose unit misses Need seeds must not fill L0 (C95 天海家/摘星学院)."""

    hop = StableId("entity.hop.chen")
    landing = StableId("entity.hop.luoluo")
    seed_anchor = _unit("anchor.seed-b", kind=RetrievalUnitKind.FACT_ANCHOR, entities=(SEED_B,))
    pair_unit = _unit(
        "graph.seed-pair",
        kind=RetrievalUnitKind.RELATION_ANCHOR,
        entities=(SEED_A, SEED_B),
    )
    off_unit = _unit(
        "graph.off-seed-hop",
        kind=RetrievalUnitKind.RELATION_ANCHOR,
        entities=(hop, landing),
    )
    pair_path = _path(path="pair", seeds=(SEED_A, SEED_B), other=SEED_B)
    off_path = GraphPathReceipt(
        path_id=StableId("graph-path.off-hop"),
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        seed_entity_ids=(SEED_A, SEED_B),
        relation_row_ids=(StableId("row.off-hop.1"), StableId("row.off-hop.2")),
        relation_ids=(StableId("relation.off-hop.1"), StableId("relation.off-hop.2")),
        entity_path=(SEED_B, hop, landing),
        predicates=("related_to", "protects"),
        directions=("forward", "forward"),
        valid_time=(
            StoryTime(worldline="main", start_ordinal=1),
            StoryTime(worldline="main", start_ordinal=1),
        ),
        edge_semantics=("canonical", "canonical"),
        evidence_refs=(_evidence(),),
        dereference_status=GraphPathDereferenceStatus.RELATION_ROWS_VERIFIED,
    )
    fused = FusionService().fuse(
        {
            RetrievalChannel.ANCHOR_BM25: (_hit(seed_anchor, RetrievalChannel.ANCHOR_BM25, 1, 1),),
            RetrievalChannel.TYPED_GRAPH: (
                _hit(off_unit, RetrievalChannel.TYPED_GRAPH, 1, 2, (off_path,)),
                _hit(pair_unit, RetrievalChannel.TYPED_GRAPH, 2, 2, (pair_path,)),
            ),
        },
        limit=8,
        seed_entity_ids=(SEED_A, SEED_B),
    )
    selected_ids = {candidate.unit.unit_id for candidate in fused if candidate.selected}
    rejected = {
        candidate.unit.unit_id: candidate.rejection_reason
        for candidate in fused
        if not candidate.selected
    }
    assert pair_unit.unit_id in selected_ids
    assert seed_anchor.unit_id in selected_ids
    assert off_unit.unit_id not in selected_ids
    assert rejected[off_unit.unit_id] == "off_seed"
