from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import pytest

from novel_agent.adapters.opensearch import OpenSearchIndex
from novel_agent.domain.ids import CommitId, ProjectId, StableId
from novel_agent.domain.memory import (
    CandidatePool,
    ChannelHit,
    RetrievalChannel,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1QueryIntent,
)
from novel_agent.services.search_retrieval import (
    CompositeRetrievalBackend,
    DeterministicHashEmbedder,
    Stage1OpenSearchBackend,
    Stage1SearchIndexer,
    _safe_name,
)
from tests.unit.test_stage1_retrieval import need, unit

COMMIT = CommitId("sha256:" + "a" * 64)
SNAPSHOT = StableId("snapshot.search")
PROJECT = ProjectId("project.Search:Example")


def _units() -> tuple[RetrievalUnit, ...]:
    return (
        unit(
            "anchor.search.event",
            RetrievalUnitKind.EVENT_ANCHOR,
            "林澈重申旧誓言",
        ).model_copy(update={"snapshot_id": SNAPSHOT}),
        unit(
            "grounded.search.block",
            RetrievalUnitKind.GROUNDED_BLOCK,
            "林澈在原文中重申旧誓言。",
        ).model_copy(update={"snapshot_id": SNAPSHOT}),
        unit(
            "anchor.search.chapter",
            RetrievalUnitKind.CHAPTER_ANCHOR,
            "第二十一章 北行",
        ).model_copy(update={"snapshot_id": SNAPSHOT}),
    )


def test_hash_embedder_is_deterministic_normalized_and_explicitly_test_only() -> None:
    with pytest.raises(ValueError, match="dimension"):
        DeterministicHashEmbedder(dimension=0)
    embedder = DeterministicHashEmbedder(dimension=4)
    first, second = embedder.embed(("same", "same"))
    assert first == second
    assert len(first) == 4
    assert sum(value * value for value in first) == pytest.approx(1.0)
    assert embedder.dimension == 4
    assert embedder.profile == "deterministic-hash-test-only-4d"


def test_stage1_indexer_builds_separate_physical_indexes_and_publishes_aliases() -> None:
    adapter = MagicMock(spec=OpenSearchIndex)
    indexer = Stage1SearchIndexer(
        cast(OpenSearchIndex, adapter), DeterministicHashEmbedder(dimension=4)
    )
    assert indexer.embedding_profile == "deterministic-hash-test-only-4d"

    anchor_index, grounded_index = indexer.build_and_publish(PROJECT, COMMIT, SNAPSHOT, _units())

    assert anchor_index != grounded_index
    assert adapter.ensure_index.call_count == 2
    assert adapter.index_document.call_count == 3
    indexed_targets = [call.args[0] for call in adapter.index_document.call_args_list]
    assert indexed_targets.count(anchor_index) == 2
    assert indexed_targets.count(grounded_index) == 1
    anchor_alias, grounded_alias = Stage1SearchIndexer.aliases(PROJECT)
    assert adapter.publish_alias.call_args_list[0].args == (anchor_index, anchor_alias)
    assert adapter.publish_alias.call_args_list[1].args == (grounded_index, grounded_alias)
    document = adapter.index_document.call_args_list[0].args[2]
    assert document["source_commit"] == COMMIT.root
    assert document["snapshot_id"] == SNAPSHOT.root
    assert document["retrieval_unit_kind"] == "event_anchor"
    assert document["embedding_profile"].startswith("deterministic-hash-test-only")

    mismatched = _units()[0].model_copy(update={"snapshot_id": StableId("snapshot.other")})
    with pytest.raises(ValueError, match="basis mismatch"):
        indexer.build_and_publish(PROJECT, COMMIT, SNAPSHOT, (mismatched,))
    with pytest.raises(ValueError, match="prefix is empty"):
        _safe_name("___")


def _search_backend(adapter: MagicMock, hit_unit: RetrievalUnit) -> Stage1OpenSearchBackend:
    adapter.search_with_total.return_value = (
        (
            {
                "_id": hit_unit.unit_id.root,
                "_score": 2.5,
                "_source": {"unit": hit_unit.model_dump(mode="json")},
            },
        ),
        7,
    )
    return Stage1OpenSearchBackend(
        cast(OpenSearchIndex, adapter),
        DeterministicHashEmbedder(dimension=4),
        project_id=PROJECT,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
    )


@pytest.mark.parametrize(
    ("channel", "pool", "alias_fragment", "query_kind"),
    [
        (RetrievalChannel.ANCHOR_BM25, CandidatePool.ANCHOR, "anchor", "bool"),
        (RetrievalChannel.ANCHOR_DENSE, CandidatePool.ANCHOR, "anchor", "knn"),
        (RetrievalChannel.HIERARCHY, CandidatePool.HIERARCHY, "anchor", "bool"),
        (RetrievalChannel.GROUNDED_BM25, CandidatePool.GROUNDED, "grounded", "bool"),
        (RetrievalChannel.GROUNDED_DENSE, CandidatePool.GROUNDED, "grounded", "knn"),
    ],
)
def test_opensearch_backend_routes_typed_channels_with_basis_filters(
    channel: RetrievalChannel,
    pool: CandidatePool,
    alias_fragment: str,
    query_kind: str,
) -> None:
    adapter = MagicMock(spec=OpenSearchIndex)
    hit_unit = _units()[1] if pool is CandidatePool.GROUNDED else _units()[0]
    if channel is RetrievalChannel.HIERARCHY:
        hit_unit = _units()[2]
    backend = _search_backend(adapter, hit_unit)
    query_need = need(
        Stage1QueryIntent.ANCHOR_INSUFFICIENT,
        "旧誓言",
        (pool,),
        entity_ids=(StableId("entity.lin-che"),),
    )

    hits = backend.search(query_need, channel, 5)

    assert len(hits) == 1
    assert hits[0].unit == hit_unit
    assert hits[0].channel is channel
    assert hits[0].candidate_count == 7
    alias, query = adapter.search_with_total.call_args.args[:2]
    assert alias_fragment in alias
    assert query_kind in query
    serialized = str(query)
    assert COMMIT.root in serialized
    assert SNAPSHOT.root in serialized


def test_opensearch_backend_fails_closed_on_invalid_queries_and_hits() -> None:
    adapter = MagicMock(spec=OpenSearchIndex)
    backend = _search_backend(adapter, _units()[0])
    query_need = need(
        Stage1QueryIntent.SEMANTIC_HISTORY,
        "旧誓言",
        (CandidatePool.ANCHOR,),
    )
    with pytest.raises(ValueError, match="limit"):
        backend.search(query_need, RetrievalChannel.ANCHOR_BM25, 0)
    wrong_basis = query_need.model_copy(update={"base_commit": CommitId("sha256:" + "b" * 64)})
    with pytest.raises(ValueError, match="basis mismatch"):
        backend.search(wrong_basis, RetrievalChannel.ANCHOR_BM25, 5)
    with pytest.raises(ValueError, match="unsupported"):
        backend.search(query_need, RetrievalChannel.R1_EXACT, 5)

    adapter.search_with_total.return_value = (({"_source": "bad", "_score": 1.0},), 1)
    with pytest.raises(TypeError, match="typed unit"):
        backend.search(query_need, RetrievalChannel.ANCHOR_BM25, 5)
    adapter.search_with_total.return_value = (
        (
            {
                "_source": {"unit": _units()[0].model_dump(mode="json")},
                "_score": "bad",
            },
        ),
        1,
    )
    with pytest.raises(TypeError, match="score"):
        backend.search(query_need, RetrievalChannel.ANCHOR_BM25, 5)


class _StaticBackend:
    def __init__(self, hits: tuple[ChannelHit, ...]) -> None:
        self.hits = hits

    def search(self, need, channel, limit):  # type: ignore[no-untyped-def]
        return self.hits


def test_composite_backend_requires_registration_and_channel_integrity() -> None:
    query_need = need(
        Stage1QueryIntent.SEMANTIC_HISTORY,
        "旧誓言",
        (CandidatePool.ANCHOR,),
    )
    with pytest.raises(ValueError, match="no retrieval backend"):
        CompositeRetrievalBackend({}).search(query_need, RetrievalChannel.ANCHOR_BM25, 5)
    wrong_hit = ChannelHit(
        unit=_units()[0],
        channel=RetrievalChannel.ANCHOR_DENSE,
        channel_rank=1,
        raw_score=1.0,
        candidate_count=1,
        hit_reason="wrong",
    )
    composite = CompositeRetrievalBackend(
        {RetrievalChannel.ANCHOR_BM25: _StaticBackend((wrong_hit,))}
    )
    with pytest.raises(ValueError, match="wrong channel"):
        composite.search(query_need, RetrievalChannel.ANCHOR_BM25, 5)
    correct_hit = wrong_hit.model_copy(update={"channel": RetrievalChannel.ANCHOR_BM25})
    composite = CompositeRetrievalBackend(
        {RetrievalChannel.ANCHOR_BM25: _StaticBackend((correct_hit,))}
    )
    assert composite.search(query_need, RetrievalChannel.ANCHOR_BM25, 5) == (correct_hit,)
