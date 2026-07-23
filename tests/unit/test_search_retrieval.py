from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import pytest

from novel_agent.adapters.opensearch import OpenSearchIndex
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, StableId
from novel_agent.domain.memory import (
    CandidatePool,
    ChannelHit,
    RetrievalChannel,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1QueryIntent,
)
from novel_agent.domain.retrieval_routing import L2IndexKind, L2IndexManifest
from novel_agent.domain.world import StoryTime
from novel_agent.services.embedding_cache import InMemoryEmbeddingCache
from novel_agent.services.search_retrieval import (
    CompositeRetrievalBackend,
    DeterministicHashEmbedder,
    Stage1OpenSearchBackend,
    Stage1SearchIndexer,
    Stage2RIndexRetentionPolicy,
    Stage2ROpenSearchBackend,
    Stage2RSearchIndexer,
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


def test_stage2r_indexer_bulk_builds_attested_indexes_with_content_cache() -> None:
    adapter = MagicMock(spec=OpenSearchIndex)
    cache = InMemoryEmbeddingCache()
    indexer = Stage2RSearchIndexer(
        cast(OpenSearchIndex, adapter),
        DeterministicHashEmbedder(dimension=4),
        embedding_cache=cache,
    )

    first = indexer.build_and_publish_receipt(PROJECT, COMMIT, SNAPSHOT, _units())
    second = indexer.build_and_publish_receipt(PROJECT, COMMIT, SNAPSHOT, _units())

    assert first.anchor_index != first.grounded_index
    assert first.anchor_document_count == 2 and first.grounded_document_count == 1
    assert first.embedding_cache.hits == 0 and first.embedding_cache.misses == 3
    assert second.embedding_cache.hits == 3 and second.embedding_cache.misses == 0
    assert adapter.bulk_index.call_count == 4
    assert adapter.refresh.call_count == 4
    assert adapter.publish_aliases.call_count == 2
    assert adapter.publish_alias.call_count == 0
    assert adapter.index_document.call_count == 0
    anchor_alias, grounded_alias = Stage2RSearchIndexer.aliases(PROJECT)
    assert "stage2r" in anchor_alias and "stage2r" in grounded_alias
    mapping = adapter.ensure_index.call_args.args[1]
    properties = mapping["mappings"]["properties"]
    assert properties["text"]["fields"]["cjk"]["analyzer"] == "cjk"
    assert properties["exact_terms"]["type"] == "keyword"
    assert properties["evidence_ids"]["type"] == "keyword"
    assert len(first.mapping_hash) == 64


def test_stage2r_indexer_namespaces_aliases_and_physical_indexes_per_experiment() -> None:
    run2_adapter = MagicMock(spec=OpenSearchIndex)
    run3_adapter = MagicMock(spec=OpenSearchIndex)
    run2 = Stage2RSearchIndexer(
        cast(OpenSearchIndex, run2_adapter),
        DeterministicHashEmbedder(dimension=4),
        index_namespace="run2",
    )
    run3 = Stage2RSearchIndexer(
        cast(OpenSearchIndex, run3_adapter),
        DeterministicHashEmbedder(dimension=4),
        index_namespace="run3",
    )

    run2_receipt = run2.build_and_publish_receipt(PROJECT, COMMIT, SNAPSHOT, _units())
    run3_receipt = run3.build_and_publish_receipt(PROJECT, COMMIT, SNAPSHOT, _units())

    assert run2.index_namespace == "run2"
    assert run3.index_namespace == "run3"
    assert run2_receipt.anchor_index != run3_receipt.anchor_index
    assert run2_receipt.anchor_alias != run3_receipt.anchor_alias
    assert "run2" in run2_receipt.anchor_index
    assert "run3" in run3_receipt.anchor_index
    assert "run2" in run2_receipt.anchor_alias
    assert "run3" in run3_receipt.anchor_alias
    run2_aliases = run2_adapter.publish_aliases.call_args.args[0]
    run3_aliases = run3_adapter.publish_aliases.call_args.args[0]
    assert run2_aliases != run3_aliases
    assert all("run2" in alias for _, alias in run2_aliases)
    assert all("run3" in alias for _, alias in run3_aliases)


def test_stage2r_backend_queries_attested_physical_index_not_shared_alias() -> None:
    adapter = MagicMock(spec=OpenSearchIndex)
    adapter.search_with_total.return_value = ((), 0)
    backend = Stage2ROpenSearchBackend(
        cast(OpenSearchIndex, adapter),
        DeterministicHashEmbedder(dimension=4),
        project_id=PROJECT,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        anchor_index_name="project-stage2r-run3-anchor-physical",
        grounded_index_name="project-stage2r-run3-grounded-physical",
    )

    backend.search(
        need(
            Stage1QueryIntent.ANCHOR_INSUFFICIENT,
            "旧誓言",
            (CandidatePool.ANCHOR,),
        ),
        RetrievalChannel.ANCHOR_BM25,
        5,
    )

    assert adapter.search_with_total.call_args.args[0] == ("project-stage2r-run3-anchor-physical")


def test_stage2r_retention_pins_checkpoints_and_accepted_snapshots() -> None:
    snapshots = {chapter: StableId(f"snapshot.chapter.{chapter}") for chapter in (20, 21, 22, 40)}
    manifests = tuple(
        L2IndexManifest(
            index_id=StableId(f"index.chapter.{chapter}"),
            index_kind=L2IndexKind.ANCHOR,
            source_commit=COMMIT,
            snapshot_id=snapshot_id,
            physical_name=f"project-stage2r-anchor-{chapter}",
            alias="project-stage2r-anchor",
            document_count=1,
            mapping_hash=ArtifactId("sha256:" + "f" * 64),
            analyzer_profile="standard-cjk-exact-v0.1",
            embedding_profile="bge-m3",
        )
        for chapter, snapshot_id in snapshots.items()
    )
    policy = Stage2RIndexRetentionPolicy()
    index = MagicMock(spec=OpenSearchIndex)

    deleted = policy.apply(
        cast(OpenSearchIndex, index),
        manifests,
        chapter_by_snapshot={snapshot_id: chapter for chapter, snapshot_id in snapshots.items()},
        accepted_snapshot_ids=(snapshots[21],),
    )

    assert deleted == ("project-stage2r-anchor-22",)
    index.delete_index.assert_called_once_with("project-stage2r-anchor-22")
    with pytest.raises(ValueError, match="unique"):
        policy.indexes_to_delete(
            manifests,
            chapter_by_snapshot={},
            accepted_snapshot_ids=(snapshots[21], snapshots[21]),
        )


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
    with pytest.raises(ValueError, match="non-empty access scope"):
        Stage1OpenSearchBackend(
            cast(OpenSearchIndex, adapter),
            DeterministicHashEmbedder(dimension=4),
            project_id=PROJECT,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scopes=(),
        )
    with pytest.raises(ValueError, match="unique"):
        Stage1OpenSearchBackend(
            cast(OpenSearchIndex, adapter),
            DeterministicHashEmbedder(dimension=4),
            project_id=PROJECT,
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            access_scopes=("writer_safe", "writer_safe"),
        )
    with pytest.raises(ValueError, match="unsupported retrieval access scope"):
        backend.search(
            query_need.model_copy(update={"access_scope": "unknown"}),
            RetrievalChannel.ANCHOR_BM25,
            5,
        )
    author_only = Stage1OpenSearchBackend(
        cast(OpenSearchIndex, adapter),
        DeterministicHashEmbedder(dimension=4),
        project_id=PROJECT,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        access_scopes=("author_planning",),
    )
    with pytest.raises(ValueError, match="cannot satisfy"):
        author_only.search(query_need, RetrievalChannel.ANCHOR_BM25, 5)

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


@pytest.mark.parametrize(
    "time_scope",
    [
        StoryTime(worldline="main", start_ordinal=20),
        StoryTime(worldline="main", label="unknown"),
    ],
)
def test_opensearch_filters_optional_time_scope(time_scope: StoryTime) -> None:
    adapter = MagicMock(spec=OpenSearchIndex)
    backend = _search_backend(adapter, _units()[0])
    query_need = need(
        Stage1QueryIntent.SEMANTIC_HISTORY,
        "旧誓言",
        (CandidatePool.ANCHOR,),
    ).model_copy(
        update={
            "time_scope": time_scope,
            "chapter_target": None,
            "horizon_target": None,
        }
    )

    backend.search(query_need, RetrievalChannel.ANCHOR_BM25, 5)

    _, query = adapter.search_with_total.call_args.args[:2]
    serialized = str(query)
    assert "worldline" in serialized
    assert ("story_time_start" in serialized) is (time_scope.start_ordinal is not None)
    assert "narrative_start" not in serialized


def test_opensearch_lexical_query_uses_phrase_matching_and_pre_score_scope_filters() -> None:
    adapter = MagicMock(spec=OpenSearchIndex)
    backend = _search_backend(adapter, _units()[1])
    quote_need = need(
        Stage1QueryIntent.EXACT_QUOTE,
        "旧誓言",
        (CandidatePool.GROUNDED,),
    )

    backend.search(quote_need, RetrievalChannel.GROUNDED_BM25, 5)

    _, query = adapter.search_with_total.call_args.args[:2]
    serialized = str(query)
    assert "match_phrase" in serialized
    assert PROJECT.root in serialized
    assert "access_scope" in serialized and "writer_safe" in serialized
    assert "information_label" in serialized and "observed" in serialized
    assert "narrative_start" in serialized and "narrative_end" in serialized


def test_author_plan_query_expands_access_without_observed_only_filter() -> None:
    adapter = MagicMock(spec=OpenSearchIndex)
    backend = Stage1OpenSearchBackend(
        cast(OpenSearchIndex, adapter),
        DeterministicHashEmbedder(dimension=4),
        project_id=PROJECT,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        access_scopes=("writer_safe", "author_planning"),
    )
    adapter.search_with_total.return_value = ((), 0)
    plan_need = need(
        Stage1QueryIntent.PLAN_OBLIGATION,
        "未决承诺",
        (CandidatePool.ANCHOR,),
    ).model_copy(update={"access_scope": "author_planning", "allow_plan": True})

    backend.search(plan_need, RetrievalChannel.ANCHOR_BM25, 5)

    _, query = adapter.search_with_total.call_args.args[:2]
    serialized = str(query)
    assert "writer_safe" in serialized and "author_planning" in serialized
    assert "information_label" not in serialized


def test_factual_search_filters_nonaccepted_truth_before_scoring() -> None:
    adapter = MagicMock(spec=OpenSearchIndex)
    backend = _search_backend(adapter, _units()[0])
    factual_need = need(
        Stage1QueryIntent.CURRENT_STATE,
        "林澈伤势",
        (CandidatePool.ANCHOR,),
    )

    backend.search(factual_need, RetrievalChannel.ANCHOR_BM25, 5)

    _, query = adapter.search_with_total.call_args.args[:2]
    serialized = str(query)
    assert "truth_class" in serialized
    assert "accepted_world_fact" in serialized


def test_hierarchy_query_is_bounded_to_declared_parent_path() -> None:
    adapter = MagicMock(spec=OpenSearchIndex)
    backend = _search_backend(adapter, _units()[2])
    query_need = need(
        Stage1QueryIntent.CHAPTER_THREAD,
        "北行",
        (CandidatePool.HIERARCHY,),
    ).model_copy(
        update={
            "hierarchy_parent_unit_ids": (
                StableId("anchor.arc.northern-expedition"),
                StableId("anchor.chapter.20"),
            )
        }
    )

    backend.search(query_need, RetrievalChannel.HIERARCHY, 5)

    _, query = adapter.search_with_total.call_args.args[:2]
    serialized = str(query)
    assert "parent_unit_ids" in serialized
    assert "parent_unit_id" in serialized
    assert "anchor.arc.northern-expedition" in serialized
    assert "anchor.chapter.20" in serialized


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
