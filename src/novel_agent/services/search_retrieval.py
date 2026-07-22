"""Stage 1 OpenSearch Anchor/Grounded indexing and retrieval adapters."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Protocol

from novel_agent.adapters.opensearch.search_index import OpenSearchIndex
from novel_agent.domain.ids import CommitId, ProjectId, StableId
from novel_agent.domain.memory import (
    ChannelHit,
    RetrievalChannel,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1MemoryNeed,
)
from novel_agent.services.retrieval import ANCHOR_KINDS, GROUNDED_KINDS, RetrievalBackend


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


class Stage1SearchIndexer:
    def __init__(self, index: OpenSearchIndex, embedder: EmbeddingProvider) -> None:
        self._index = index
        self._embedder = embedder

    @property
    def embedding_profile(self) -> str:
        return self._embedder.profile

    def build_and_publish(
        self,
        project_id: ProjectId,
        source_commit: CommitId,
        snapshot_id: StableId,
        units: tuple[RetrievalUnit, ...],
    ) -> tuple[str, str]:
        self._validate_basis(source_commit, snapshot_id, units)
        anchor_index, grounded_index = self._physical_names(project_id, snapshot_id)
        mapping = self._mapping(self._embedder.dimension)
        self._index.ensure_index(anchor_index, mapping)
        self._index.ensure_index(grounded_index, mapping)
        vectors = self._embedder.embed(tuple(unit.text for unit in units))
        for unit, vector in zip(units, vectors, strict=True):
            target = anchor_index if unit.unit_kind in ANCHOR_KINDS else grounded_index
            self._index.index_document(
                target,
                unit.unit_id.root,
                {
                    "unit": unit.model_dump(mode="json"),
                    "text": unit.text,
                    "source_commit": source_commit.root,
                    "snapshot_id": snapshot_id.root,
                    "retrieval_unit_kind": unit.unit_kind.value,
                    "entity_ids": [entity.root for entity in unit.entity_ids],
                    "parent_unit_id": (
                        None if unit.parent_unit_id is None else unit.parent_unit_id.root
                    ),
                    "embedding": vector,
                    "embedding_profile": self._embedder.profile,
                },
            )
        anchor_alias, grounded_alias = self.aliases(project_id)
        self._index.publish_alias(anchor_index, anchor_alias)
        self._index.publish_alias(grounded_index, grounded_alias)
        return anchor_index, grounded_index

    @staticmethod
    def aliases(project_id: ProjectId) -> tuple[str, str]:
        prefix = _safe_name(project_id.root)
        return f"{prefix}-stage1-anchor", f"{prefix}-stage1-grounded"

    @staticmethod
    def _physical_names(project_id: ProjectId, snapshot_id: StableId) -> tuple[str, str]:
        prefix = _safe_name(project_id.root)
        suffix = hashlib.sha256(snapshot_id.root.encode()).hexdigest()[:16]
        return f"{prefix}-anchor-{suffix}", f"{prefix}-grounded-{suffix}"

    @staticmethod
    def _mapping(dimension: int) -> dict[str, object]:
        return {
            "settings": {"index": {"knn": True}},
            "mappings": {
                "dynamic": "strict",
                "properties": {
                    "unit": {"type": "object", "enabled": False},
                    "text": {"type": "text"},
                    "source_commit": {"type": "keyword"},
                    "snapshot_id": {"type": "keyword"},
                    "retrieval_unit_kind": {"type": "keyword"},
                    "entity_ids": {"type": "keyword"},
                    "parent_unit_id": {"type": "keyword"},
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


class Stage1OpenSearchBackend:
    def __init__(
        self,
        index: OpenSearchIndex,
        embedder: EmbeddingProvider,
        *,
        project_id: ProjectId,
        source_commit: CommitId,
        snapshot_id: StableId,
    ) -> None:
        self._index = index
        self._embedder = embedder
        self._source_commit = source_commit
        self._snapshot_id = snapshot_id
        self._anchor_alias, self._grounded_alias = Stage1SearchIndexer.aliases(project_id)

    def search(
        self, need: Stage1MemoryNeed, channel: RetrievalChannel, limit: int
    ) -> tuple[ChannelHit, ...]:
        if limit < 1:
            raise ValueError("OpenSearch retrieval limit must be positive")
        if need.base_commit != self._source_commit:
            raise ValueError("OpenSearch query canonical basis mismatch")
        alias, kinds = self._route(channel)
        filters: list[dict[str, object]] = [
            {"term": {"source_commit": self._source_commit.root}},
            {"term": {"snapshot_id": self._snapshot_id.root}},
            {"terms": {"retrieval_unit_kind": [kind.value for kind in kinds]}},
        ]
        if need.entity_ids:
            filters.append({"terms": {"entity_ids": [item.root for item in need.entity_ids]}})
        if channel in {RetrievalChannel.ANCHOR_DENSE, RetrievalChannel.GROUNDED_DENSE}:
            vector = self._embedder.embed((need.query_text,))[0]
            query: dict[str, object] = {
                "knn": {
                    "embedding": {
                        "vector": vector,
                        "k": limit,
                        "filter": {"bool": {"filter": filters}},
                    }
                }
            }
        else:
            query = {
                "bool": {
                    "must": [{"match": {"text": need.query_text}}],
                    "filter": filters,
                }
            }
        hits, total = self._index.search_with_total(alias, query, size=limit)
        return tuple(self._hit(hit, channel, rank, total) for rank, hit in enumerate(hits, start=1))

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
