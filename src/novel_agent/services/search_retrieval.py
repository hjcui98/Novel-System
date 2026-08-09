"""Stage 1 OpenSearch Anchor/Grounded indexing and retrieval adapters."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Protocol

from novel_agent.adapters.opensearch.search_index import OpenSearchIndex
from novel_agent.domain.ids import CommitId, ProjectId, StableId
from novel_agent.domain.memory import (
    ChannelHit,
    RetrievalChannel,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1MemoryNeed,
    Stage1QueryIntent,
)
from novel_agent.domain.planning_memory import RetrievalQueryBundle
from novel_agent.domain.retrieval_routing import L2IndexManifest
from novel_agent.ports.search_index import SearchIndexPort
from novel_agent.services.embedding_cache import (
    CachedEmbeddingService,
    EmbeddingCacheRepository,
    EmbeddingCacheStats,
)
from novel_agent.services.need_query_compiler import compile_need_query
from novel_agent.services.retrieval import ANCHOR_KINDS, GROUNDED_KINDS, RetrievalBackend

FACTUAL_INTENTS = {
    Stage1QueryIntent.CURRENT_STATE,
    Stage1QueryIntent.KNOWN_ID,
    Stage1QueryIntent.MANDATORY_CONSTRAINT,
}


class EmbeddingProvider(Protocol):
    @property
    def dimension(self) -> int: ...

    @property
    def profile(self) -> str: ...

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...


class DeterministicHashEmbedder:
    """Non-semantic deterministic adapter for infrastructure and contract tests only."""

    def __init__(self, *, dimension: int = 8) -> None:
        if dimension < 1:
            raise ValueError("embedding dimension must be positive")
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def profile(self) -> str:
        return f"deterministic-hash-test-only-{self._dimension}d"

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        vectors: list[tuple[float, ...]] = []
        for text in texts:
            seed = hashlib.sha256(text.encode("utf-8")).digest()
            values = tuple(
                (seed[index % len(seed)] / 127.5) - 1.0 for index in range(self._dimension)
            )
            magnitude = sum(value * value for value in values) ** 0.5
            vectors.append(tuple(value / magnitude for value in values))
        return tuple(vectors)


@dataclass(frozen=True, slots=True)
class SearchIndexBuildReceipt:
    anchor_index: str
    grounded_index: str
    anchor_alias: str
    grounded_alias: str
    anchor_document_count: int
    grounded_document_count: int
    mapping_hash: str
    embedding_cache: EmbeddingCacheStats


@dataclass(frozen=True, slots=True)
class Stage2RIndexRetentionPolicy:
    """Delete only unpinned physical indexes; manifests remain durable receipts."""

    checkpoint_chapters: tuple[int, ...] = (0, 20, 40, 60, 80, 95)

    def indexes_to_delete(
        self,
        manifests: tuple[L2IndexManifest, ...],
        *,
        chapter_by_snapshot: dict[StableId, int],
        accepted_snapshot_ids: tuple[StableId, ...] = (),
    ) -> tuple[str, ...]:
        if len(accepted_snapshot_ids) != len(set(accepted_snapshot_ids)):
            raise ValueError("accepted retention snapshot ids must be unique")
        pinned = set(accepted_snapshot_ids)
        pinned.update(
            snapshot_id
            for snapshot_id, chapter in chapter_by_snapshot.items()
            if chapter in self.checkpoint_chapters
        )
        return tuple(
            sorted(
                {
                    manifest.physical_name
                    for manifest in manifests
                    if manifest.snapshot_id not in pinned
                }
            )
        )

    def apply(
        self,
        index: SearchIndexPort,
        manifests: tuple[L2IndexManifest, ...],
        *,
        chapter_by_snapshot: dict[StableId, int],
        accepted_snapshot_ids: tuple[StableId, ...] = (),
    ) -> tuple[str, ...]:
        targets = self.indexes_to_delete(
            manifests,
            chapter_by_snapshot=chapter_by_snapshot,
            accepted_snapshot_ids=accepted_snapshot_ids,
        )
        for physical_name in targets:
            index.delete_index(physical_name)
        return targets


class Stage1SearchIndexer:
    """Reusable OpenSearch projection kernel.

    Legacy callers retain single-document indexing.  Stage 2R callers use the
    receipt-producing bulk method so aliases are published only after both
    physical indexes have been refreshed.
    """

    def __init__(
        self,
        index: OpenSearchIndex,
        embedder: EmbeddingProvider,
        *,
        embedding_cache: EmbeddingCacheRepository | None = None,
        embedding_input_profile: str = "narrative-bge-m3-v0.1",
    ) -> None:
        self._index = index
        self._embedder = embedder
        self._cached_embeddings = (
            None
            if embedding_cache is None
            else CachedEmbeddingService(embedding_cache, input_profile=embedding_input_profile)
        )
        self._embedding_input_profile = embedding_input_profile

    @property
    def embedding_profile(self) -> str:
        return self._embedder.profile

    @property
    def embedding_dimension(self) -> int:
        return self._embedder.dimension

    @property
    def embedding_input_profile(self) -> str:
        return self._embedding_input_profile

    def build_and_publish(
        self,
        project_id: ProjectId,
        source_commit: CommitId,
        snapshot_id: StableId,
        units: tuple[RetrievalUnit, ...],
    ) -> tuple[str, str]:
        receipt = self._build_and_publish(
            project_id,
            source_commit,
            snapshot_id,
            units,
            bulk=False,
        )
        return receipt.anchor_index, receipt.grounded_index

    def build_and_publish_receipt(
        self,
        project_id: ProjectId,
        source_commit: CommitId,
        snapshot_id: StableId,
        units: tuple[RetrievalUnit, ...],
    ) -> SearchIndexBuildReceipt:
        return self._build_and_publish(
            project_id,
            source_commit,
            snapshot_id,
            units,
            bulk=True,
        )

    def _build_and_publish(
        self,
        project_id: ProjectId,
        source_commit: CommitId,
        snapshot_id: StableId,
        units: tuple[RetrievalUnit, ...],
        *,
        bulk: bool,
    ) -> SearchIndexBuildReceipt:
        self._validate_basis(source_commit, snapshot_id, units)
        anchor_index, grounded_index = self._physical_names_for(project_id, snapshot_id)
        mapping = self._mapping(self._embedder.dimension)
        self._index.ensure_index(anchor_index, mapping)
        self._index.ensure_index(grounded_index, mapping)
        vectors, cache_stats = self._vectors(units)
        anchor_documents: list[tuple[str, dict[str, object]]] = []
        grounded_documents: list[tuple[str, dict[str, object]]] = []
        for unit, vector in zip(units, vectors, strict=True):
            target = anchor_index if unit.unit_kind in ANCHOR_KINDS else grounded_index
            document = self._document(
                unit,
                project_id,
                source_commit,
                snapshot_id,
                vector,
            )
            if target == anchor_index:
                anchor_documents.append((unit.unit_id.root, document))
            else:
                grounded_documents.append((unit.unit_id.root, document))
        if bulk:
            self._index.bulk_index(anchor_index, tuple(anchor_documents))
            self._index.bulk_index(grounded_index, tuple(grounded_documents))
            self._index.refresh(anchor_index)
            self._index.refresh(grounded_index)
        else:
            for document_id, document in anchor_documents:
                self._index.index_document(anchor_index, document_id, document)
            for document_id, document in grounded_documents:
                self._index.index_document(grounded_index, document_id, document)
        anchor_alias, grounded_alias = self._aliases_for(project_id)
        if bulk:
            self._index.publish_aliases(
                ((anchor_index, anchor_alias), (grounded_index, grounded_alias))
            )
        else:
            self._index.publish_alias(anchor_index, anchor_alias)
            self._index.publish_alias(grounded_index, grounded_alias)
        return SearchIndexBuildReceipt(
            anchor_index=anchor_index,
            grounded_index=grounded_index,
            anchor_alias=anchor_alias,
            grounded_alias=grounded_alias,
            anchor_document_count=len(anchor_documents),
            grounded_document_count=len(grounded_documents),
            mapping_hash=hashlib.sha256(
                json.dumps(mapping, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            embedding_cache=cache_stats,
        )

    @staticmethod
    def aliases(project_id: ProjectId) -> tuple[str, str]:
        prefix = _safe_name(project_id.root)
        return f"{prefix}-stage1-anchor", f"{prefix}-stage1-grounded"

    @staticmethod
    def _physical_names(project_id: ProjectId, snapshot_id: StableId) -> tuple[str, str]:
        prefix = _safe_name(project_id.root)
        suffix = hashlib.sha256(snapshot_id.root.encode()).hexdigest()[:16]
        return f"{prefix}-anchor-{suffix}", f"{prefix}-grounded-{suffix}"

    def _aliases_for(self, project_id: ProjectId) -> tuple[str, str]:
        return self.aliases(project_id)

    def _physical_names_for(self, project_id: ProjectId, snapshot_id: StableId) -> tuple[str, str]:
        return self._physical_names(project_id, snapshot_id)

    @staticmethod
    def _mapping(dimension: int) -> dict[str, object]:
        return {
            # The local benchmark OpenSearch deployment is single-node.  An
            # unassignable replica still consumes LOCAL_ONLY shard budget and
            # can prevent a long teacher-forced run from creating its next
            # checkpoint index, so projections are intentionally primary-only.
            "settings": {"index": {"knn": True, "number_of_replicas": 0}},
            "mappings": {
                "dynamic": "strict",
                "properties": {
                    "unit": {"type": "object", "enabled": False},
                    "text": {
                        "type": "text",
                        "fields": {
                            "standard": {"type": "text"},
                            "cjk": {"type": "text", "analyzer": "cjk"},
                        },
                    },
                    "exact_terms": {"type": "keyword"},
                    "project_id": {"type": "keyword"},
                    "source_commit": {"type": "keyword"},
                    "snapshot_id": {"type": "keyword"},
                    "retrieval_unit_kind": {"type": "keyword"},
                    "entity_ids": {"type": "keyword"},
                    "evidence_ids": {"type": "keyword"},
                    "parent_unit_id": {"type": "keyword"},
                    "parent_unit_ids": {"type": "keyword"},
                    "predicate": {"type": "keyword"},
                    "worldline": {"type": "keyword"},
                    "access_scope": {"type": "keyword"},
                    "truth_class": {"type": "keyword"},
                    "information_label": {"type": "keyword"},
                    "narrative_start": {"type": "integer"},
                    "narrative_end": {"type": "integer"},
                    "story_time_start": {"type": "integer"},
                    "story_time_end": {"type": "integer"},
                    "embedding_profile": {"type": "keyword"},
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": dimension,
                        "method": {
                            "name": "hnsw",
                            "space_type": "cosinesimil",
                            "engine": "lucene",
                        },
                    },
                },
            },
        }

    @staticmethod
    def _validate_basis(
        source_commit: CommitId,
        snapshot_id: StableId,
        units: tuple[RetrievalUnit, ...],
    ) -> None:
        if any(
            unit.source_commit != source_commit or unit.snapshot_id != snapshot_id for unit in units
        ):
            raise ValueError("search index unit basis mismatch")

    def _vectors(
        self,
        units: tuple[RetrievalUnit, ...],
    ) -> tuple[tuple[tuple[float, ...], ...], EmbeddingCacheStats]:
        if self._cached_embeddings is not None:
            vectors = self._cached_embeddings.embed_units(units, self._embedder)
            return vectors, self._cached_embeddings.last_stats
        vectors = tuple(
            CachedEmbeddingService._normalize(vector, self._embedder.dimension)
            for vector in self._embedder.embed(tuple(unit.text for unit in units))
        )
        return vectors, EmbeddingCacheStats(hits=0, misses=len(units))

    def _document(
        self,
        unit: RetrievalUnit,
        project_id: ProjectId,
        source_commit: CommitId,
        snapshot_id: StableId,
        vector: tuple[float, ...],
    ) -> dict[str, object]:
        exact_terms = [entity.root for entity in unit.entity_ids]
        if unit.predicate is not None:
            exact_terms.append(unit.predicate)
        return {
            "unit": unit.model_dump(mode="json"),
            "text": unit.text,
            "exact_terms": exact_terms,
            "project_id": project_id.root,
            "source_commit": source_commit.root,
            "snapshot_id": snapshot_id.root,
            "retrieval_unit_kind": unit.unit_kind.value,
            "entity_ids": [entity.root for entity in unit.entity_ids],
            "evidence_ids": [item.evidence_id.root for item in unit.evidence_refs],
            "parent_unit_id": None if unit.parent_unit_id is None else unit.parent_unit_id.root,
            "parent_unit_ids": [item.root for item in unit.parent_unit_ids],
            "predicate": unit.predicate,
            "worldline": unit.worldline,
            "access_scope": unit.access_scope,
            "truth_class": None if unit.truth_class is None else unit.truth_class.value,
            "information_label": unit.information_label,
            "narrative_start": unit.narrative_start,
            "narrative_end": unit.narrative_end,
            "story_time_start": unit.story_time_start,
            "story_time_end": unit.story_time_end,
            "embedding": vector,
            "embedding_profile": self._embedder.profile,
        }


class Stage2RSearchIndexer(Stage1SearchIndexer):
    """Stage 2R naming profile with isolated Anchor/Grounded aliases."""

    def __init__(
        self,
        index: OpenSearchIndex,
        embedder: EmbeddingProvider,
        *,
        embedding_cache: EmbeddingCacheRepository | None = None,
        embedding_input_profile: str = "narrative-bge-m3-v0.1",
        index_namespace: str = "default",
    ) -> None:
        super().__init__(
            index,
            embedder,
            embedding_cache=embedding_cache,
            embedding_input_profile=embedding_input_profile,
        )
        self._index_namespace = _safe_name(index_namespace)

    @property
    def index_namespace(self) -> str:
        return self._index_namespace

    @staticmethod
    def aliases(project_id: ProjectId) -> tuple[str, str]:
        prefix = _safe_name(project_id.root)
        return f"{prefix}-stage2r-anchor", f"{prefix}-stage2r-grounded"

    @staticmethod
    def _physical_names(project_id: ProjectId, snapshot_id: StableId) -> tuple[str, str]:
        prefix = _safe_name(project_id.root)
        suffix = hashlib.sha256(snapshot_id.root.encode()).hexdigest()[:16]
        return f"{prefix}-stage2r-anchor-{suffix}", f"{prefix}-stage2r-grounded-{suffix}"

    def _aliases_for(self, project_id: ProjectId) -> tuple[str, str]:
        if self._index_namespace == "default":
            return self.aliases(project_id)
        prefix = _safe_name(project_id.root)
        namespace = self._index_namespace
        return (
            f"{prefix}-stage2r-{namespace}-anchor",
            f"{prefix}-stage2r-{namespace}-grounded",
        )

    def _physical_names_for(self, project_id: ProjectId, snapshot_id: StableId) -> tuple[str, str]:
        if self._index_namespace == "default":
            return self._physical_names(project_id, snapshot_id)
        prefix = _safe_name(project_id.root)
        namespace = self._index_namespace
        suffix = hashlib.sha256(snapshot_id.root.encode()).hexdigest()[:16]
        return (
            f"{prefix}-stage2r-{namespace}-anchor-{suffix}",
            f"{prefix}-stage2r-{namespace}-grounded-{suffix}",
        )


class Stage1OpenSearchBackend:
    def __init__(
        self,
        index: OpenSearchIndex,
        embedder: EmbeddingProvider,
        *,
        project_id: ProjectId,
        source_commit: CommitId,
        snapshot_id: StableId,
        indexer_type: type[Stage1SearchIndexer] = Stage1SearchIndexer,
        access_scopes: tuple[str, ...] = ("writer_safe",),
        anchor_index_name: str | None = None,
        grounded_index_name: str | None = None,
    ) -> None:
        if not access_scopes or any(not scope for scope in access_scopes):
            raise ValueError("OpenSearch retrieval requires at least one non-empty access scope")
        if len(access_scopes) != len(set(access_scopes)):
            raise ValueError("OpenSearch retrieval access scopes must be unique")
        self._index = index
        self._embedder = embedder
        self._project_id = project_id
        self._source_commit = source_commit
        self._snapshot_id = snapshot_id
        default_anchor, default_grounded = indexer_type.aliases(project_id)
        self._anchor_alias = anchor_index_name or default_anchor
        self._grounded_alias = grounded_index_name or default_grounded
        self._access_scopes = access_scopes

    def search(
        self, need: Stage1MemoryNeed, channel: RetrievalChannel, limit: int
    ) -> tuple[ChannelHit, ...]:
        if limit < 1:
            raise ValueError("OpenSearch retrieval limit must be positive")
        if need.base_commit != self._source_commit:
            raise ValueError("OpenSearch query canonical basis mismatch")
        bundle = compile_need_query(need)
        alias, kinds = self._route(channel)
        filters = self._filters(need, kinds, bundle)
        if channel is RetrievalChannel.HIERARCHY and need.hierarchy_parent_unit_ids:
            parent_ids = [item.root for item in need.hierarchy_parent_unit_ids]
            filters.append(
                {
                    "bool": {
                        "should": [
                            {"terms": {"parent_unit_ids": parent_ids}},
                            {"terms": {"parent_unit_id": parent_ids}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            )
        query: dict[str, object]
        if channel in {RetrievalChannel.ANCHOR_DENSE, RetrievalChannel.GROUNDED_DENSE}:
            vector = CachedEmbeddingService._normalize(
                self._embedder.embed((bundle.semantic_query,))[0],
                self._embedder.dimension,
            )
            query = {
                "knn": {
                    "embedding": {
                        "vector": vector,
                        "k": limit,
                        "filter": {"bool": {"filter": filters}},
                    }
                }
            }
        else:
            lexical_clause: dict[str, object]
            if need.query_intent in {
                Stage1QueryIntent.EXACT_QUOTE,
                Stage1QueryIntent.RARE_PHRASE,
            }:
                lexical_clause = {
                    "bool": {
                        "should": [
                            {
                                "match_phrase": {
                                    "text.standard": {"query": bundle.lexical_queries[0]}
                                }
                            },
                            {"match_phrase": {"text.cjk": {"query": bundle.lexical_queries[0]}}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            else:
                hint_clauses = tuple(
                    {
                        "multi_match": {
                            "query": hint,
                            "fields": [
                                "text.standard^0.6",
                                "text.cjk^0.7",
                                "exact_terms^1.8",
                            ],
                        }
                    }
                    for hint in bundle.lexical_queries[1:]
                )
                lexical_clause = (
                    {
                        "multi_match": {
                            "query": bundle.lexical_queries[0],
                            "fields": [
                                "text.standard^1.0",
                                "text.cjk^1.2",
                                "exact_terms^3.0",
                            ],
                        }
                    }
                    if not hint_clauses
                    else {
                        "bool": {
                            "should": [
                                {
                                    "multi_match": {
                                        "query": bundle.lexical_queries[0],
                                        "fields": [
                                            "text.standard^1.0",
                                            "text.cjk^1.2",
                                            "exact_terms^3.0",
                                        ],
                                    }
                                },
                                *hint_clauses,
                            ],
                            "minimum_should_match": 1,
                        }
                    }
                )
            query = {
                "bool": {
                    "must": [lexical_clause],
                    "filter": filters,
                }
            }
        hits, total = self._index.search_with_total(alias, query, size=limit)
        return tuple(self._hit(hit, channel, rank, total) for rank, hit in enumerate(hits, start=1))

    def _filters(
        self,
        need: Stage1MemoryNeed,
        kinds: frozenset[RetrievalUnitKind],
        bundle: RetrievalQueryBundle | None = None,
    ) -> list[dict[str, object]]:
        """Apply basis, access, temporal, and narrative filters before scoring."""

        filters: list[dict[str, object]] = [
            {"term": {"project_id": self._project_id.root}},
            {"term": {"source_commit": self._source_commit.root}},
            {"term": {"snapshot_id": self._snapshot_id.root}},
            {"terms": {"retrieval_unit_kind": [kind.value for kind in kinds]}},
            {"terms": {"access_scope": list(self._visible_access_scopes(need))}},
        ]
        excluded = bundle.excluded_information_labels if bundle is not None else ()
        if "plan" in excluded:
            filters.append({"term": {"information_label": "observed"}})
        if need.query_intent in FACTUAL_INTENTS:
            filters.append(
                {
                    "bool": {
                        "should": [
                            {"bool": {"must_not": {"exists": {"field": "truth_class"}}}},
                            {"term": {"truth_class": "accepted_world_fact"}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            )
        # Grounded blocks are raw text projections and deliberately do not carry
        # curator-owned entity associations. Applying a structured entity filter
        # here makes every legitimate evidence fallback empty; lexical/vector
        # relevance still scopes the raw-text search.
        if need.entity_ids and not kinds.issubset(GROUNDED_KINDS):
            filters.append({"terms": {"entity_ids": [item.root for item in need.entity_ids]}})
        if need.time_scope is not None:
            filters.append({"term": {"worldline": need.time_scope.worldline}})
            if need.time_scope.start_ordinal is not None:
                ordinal = need.time_scope.start_ordinal
                filters.extend(
                    (
                        self._range_or_unspecified("story_time_start", "lte", ordinal),
                        self._range_or_unspecified("story_time_end", "gte", ordinal),
                    )
                )
        # ``horizon_target`` is the future writing range, not a point that
        # historical evidence must overlap. Commit scope already enforces the
        # cutoff. Only an explicit chapter_target requests a chapter-local query.
        narrative_end = need.chapter_target
        if narrative_end is not None:
            filters.extend(
                (
                    self._range_or_unspecified("narrative_start", "lte", narrative_end),
                    self._range_or_unspecified("narrative_end", "gte", narrative_end),
                )
            )
        return filters

    def _visible_access_scopes(self, need: Stage1MemoryNeed) -> tuple[str, ...]:
        required = {
            "writer_safe": ("writer_safe",),
            "author_planning": ("writer_safe", "author_planning"),
            "evaluator": ("writer_safe", "author_planning", "evaluator"),
        }.get(need.access_scope)
        if required is None:
            raise ValueError(f"unsupported retrieval access scope: {need.access_scope}")
        visible = tuple(scope for scope in required if scope in self._access_scopes)
        if not visible:
            raise ValueError("retrieval backend cannot satisfy the need access scope")
        return visible

    @staticmethod
    def _range_or_unspecified(field: str, operator: str, value: int) -> dict[str, object]:
        return {
            "bool": {
                "should": [
                    {"bool": {"must_not": {"exists": {"field": field}}}},
                    {"range": {field: {operator: value}}},
                ],
                "minimum_should_match": 1,
            }
        }

    def _route(self, channel: RetrievalChannel) -> tuple[str, frozenset[RetrievalUnitKind]]:
        if channel in {RetrievalChannel.ANCHOR_BM25, RetrievalChannel.ANCHOR_DENSE}:
            return self._anchor_alias, ANCHOR_KINDS
        if channel is RetrievalChannel.HIERARCHY:
            return self._anchor_alias, frozenset(
                {
                    RetrievalUnitKind.ARC_ANCHOR,
                    RetrievalUnitKind.CHAPTER_ANCHOR,
                    RetrievalUnitKind.SCENE_ANCHOR,
                }
            )
        if channel in {RetrievalChannel.GROUNDED_BM25, RetrievalChannel.GROUNDED_DENSE}:
            return self._grounded_alias, GROUNDED_KINDS
        raise ValueError(f"unsupported OpenSearch retrieval channel: {channel.value}")

    @staticmethod
    def _hit(
        hit: dict[str, object],
        channel: RetrievalChannel,
        rank: int,
        total: int,
    ) -> ChannelHit:
        source = hit.get("_source")
        if not isinstance(source, dict) or not isinstance(source.get("unit"), dict):
            raise TypeError("OpenSearch retrieval hit has no typed unit source")
        score = hit.get("_score", 0.0)
        if not isinstance(score, (int, float)):
            raise TypeError("OpenSearch retrieval score must be numeric")
        return ChannelHit(
            unit=RetrievalUnit.model_validate_json(
                json.dumps(source["unit"], ensure_ascii=False), strict=True
            ),
            channel=channel,
            channel_rank=rank,
            raw_score=float(score),
            candidate_count=max(total, rank),
            hit_reason=f"opensearch_{channel.value}_match",
        )


class Stage2ROpenSearchBackend(Stage1OpenSearchBackend):
    def __init__(
        self,
        index: OpenSearchIndex,
        embedder: EmbeddingProvider,
        *,
        project_id: ProjectId,
        source_commit: CommitId,
        snapshot_id: StableId,
        access_scopes: tuple[str, ...] = ("writer_safe",),
        anchor_index_name: str | None = None,
        grounded_index_name: str | None = None,
    ) -> None:
        super().__init__(
            index,
            embedder,
            project_id=project_id,
            source_commit=source_commit,
            snapshot_id=snapshot_id,
            indexer_type=Stage2RSearchIndexer,
            access_scopes=access_scopes,
            anchor_index_name=anchor_index_name,
            grounded_index_name=grounded_index_name,
        )


class CompositeRetrievalBackend:
    def __init__(self, routes: dict[RetrievalChannel, RetrievalBackend]) -> None:
        self._routes = dict(routes)

    def search(
        self, need: Stage1MemoryNeed, channel: RetrievalChannel, limit: int
    ) -> tuple[ChannelHit, ...]:
        backend = self._routes.get(channel)
        if backend is None:
            raise ValueError(f"no retrieval backend registered for {channel.value}")
        hits = backend.search(need, channel, limit)
        if any(hit.channel is not channel for hit in hits):
            raise ValueError("retrieval backend returned a hit for the wrong channel")
        return hits


def _safe_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.casefold()).strip("-")
    if not normalized:
        raise ValueError("OpenSearch index prefix is empty")
    return normalized[:80]
