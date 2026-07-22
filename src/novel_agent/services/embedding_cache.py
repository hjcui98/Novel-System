"""Content-addressed embedding cache used only for rebuildable retrieval projections."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.models import EmbeddingCacheRow
from novel_agent.domain.ids import ArtifactId
from novel_agent.domain.memory import RetrievalUnit


class EmbeddingProviderPort(Protocol):
    @property
    def dimension(self) -> int: ...

    @property
    def profile(self) -> str: ...

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...


@dataclass(frozen=True, slots=True)
class EmbeddingCacheKey:
    content_hash: ArtifactId
    embedding_profile: str
    input_profile: str


@dataclass(frozen=True, slots=True)
class EmbeddingCacheStats:
    hits: int
    misses: int


class EmbeddingCacheRepository(Protocol):
    def get(self, key: EmbeddingCacheKey, *, dimension: int) -> tuple[float, ...] | None: ...

    def put(self, key: EmbeddingCacheKey, vector: tuple[float, ...]) -> None: ...


class InMemoryEmbeddingCache:
    def __init__(self) -> None:
        self._values: dict[EmbeddingCacheKey, tuple[float, ...]] = {}

    def get(self, key: EmbeddingCacheKey, *, dimension: int) -> tuple[float, ...] | None:
        vector = self._values.get(key)
        if vector is not None and len(vector) != dimension:
            raise ValueError("embedding cache dimension mismatch")
        return vector

    def put(self, key: EmbeddingCacheKey, vector: tuple[float, ...]) -> None:
        if not vector:
            raise ValueError("embedding cache cannot store an empty vector")
        existing = self._values.get(key)
        if existing is not None and existing != vector:
            raise ValueError("embedding cache key already has a different vector")
        self._values[key] = vector


class SqlEmbeddingCache:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self, key: EmbeddingCacheKey, *, dimension: int) -> tuple[float, ...] | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(EmbeddingCacheRow).where(
                    EmbeddingCacheRow.content_hash == key.content_hash.root,
                    EmbeddingCacheRow.embedding_profile == key.embedding_profile,
                    EmbeddingCacheRow.input_profile == key.input_profile,
                )
            )
            if row is None:
                return None
            if row.dimension != dimension or len(row.vector_json) != dimension:
                raise ValueError("embedding cache dimension mismatch")
            if any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in row.vector_json
            ):
                raise ValueError("embedding cache vector is not numeric")
            return tuple(float(value) for value in row.vector_json)

    def put(self, key: EmbeddingCacheKey, vector: tuple[float, ...]) -> None:
        if not vector:
            raise ValueError("embedding cache cannot store an empty vector")
        with self._session_factory() as session, session.begin():
            row = session.scalar(
                select(EmbeddingCacheRow).where(
                    EmbeddingCacheRow.content_hash == key.content_hash.root,
                    EmbeddingCacheRow.embedding_profile == key.embedding_profile,
                    EmbeddingCacheRow.input_profile == key.input_profile,
                )
            )
            if row is None:
                session.add(
                    EmbeddingCacheRow(
                        content_hash=key.content_hash.root,
                        embedding_profile=key.embedding_profile,
                        input_profile=key.input_profile,
                        dimension=len(vector),
                        vector_json=list(vector),
                        created_at=datetime.now(UTC),
                    )
                )
            elif row.dimension != len(vector) or tuple(row.vector_json) != vector:
                raise ValueError("embedding cache key already has a different vector")


class CachedEmbeddingService:
    def __init__(self, cache: EmbeddingCacheRepository, *, input_profile: str) -> None:
        if not input_profile:
            raise ValueError("embedding input profile must not be empty")
        self._cache = cache
        self._input_profile = input_profile
        self.last_stats = EmbeddingCacheStats(hits=0, misses=0)

    @staticmethod
    def content_hash(unit: RetrievalUnit) -> ArtifactId:
        if unit.content_hash is not None:
            return unit.content_hash
        return ArtifactId("sha256:" + hashlib.sha256(unit.text.encode("utf-8")).hexdigest())

    def embed_units(
        self,
        units: Iterable[RetrievalUnit],
        provider: EmbeddingProviderPort,
    ) -> tuple[tuple[float, ...], ...]:
        items = tuple(units)
        keys = tuple(
            EmbeddingCacheKey(
                content_hash=self.content_hash(unit),
                embedding_profile=provider.profile,
                input_profile=self._input_profile,
            )
            for unit in items
        )
        vectors: list[tuple[float, ...] | None] = [
            self._cache.get(key, dimension=provider.dimension) for key in keys
        ]
        missing_by_key: dict[EmbeddingCacheKey, list[int]] = {}
        for index, (key, vector) in enumerate(zip(keys, vectors, strict=True)):
            if vector is None:
                missing_by_key.setdefault(key, []).append(index)
        if missing_by_key:
            missing_items = tuple(indexes[0] for indexes in missing_by_key.values())
            embedded = provider.embed(tuple(items[index].text for index in missing_items))
            if len(embedded) != len(missing_items):
                raise ValueError("embedding provider result count differs from cache misses")
            for indexes, vector in zip(missing_by_key.values(), embedded, strict=True):
                normalized = self._normalize(vector, provider.dimension)
                key = keys[indexes[0]]
                self._cache.put(key, normalized)
                for index in indexes:
                    vectors[index] = normalized
        self.last_stats = EmbeddingCacheStats(
            hits=len(items) - sum(len(indexes) for indexes in missing_by_key.values()),
            misses=len(missing_by_key),
        )
        if any(vector is None for vector in vectors):
            raise RuntimeError("embedding cache did not resolve every vector")
        return tuple(vector for vector in vectors if vector is not None)

    @staticmethod
    def _normalize(vector: tuple[float, ...], dimension: int) -> tuple[float, ...]:
        if len(vector) != dimension:
            raise ValueError("embedding provider dimension mismatch")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in vector):
            raise ValueError("embedding provider vector is not numeric")
        squared = sum(float(value) * float(value) for value in vector)
        if squared <= 0:
            raise ValueError("embedding provider returned a zero vector")
        magnitude = squared**0.5
        return tuple(float(value) / magnitude for value in vector)
