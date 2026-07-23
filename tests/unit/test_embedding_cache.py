from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.models import EmbeddingCacheRow
from novel_agent.domain.ids import CommitId, StableId
from novel_agent.domain.memory import RetrievalUnit, RetrievalUnitKind
from novel_agent.services.embedding_cache import (
    CachedEmbeddingService,
    EmbeddingCacheKey,
    InMemoryEmbeddingCache,
    SqlEmbeddingCache,
)

COMMIT = CommitId("sha256:" + "a" * 64)
SNAPSHOT = StableId("snapshot.embedding-cache")


class Embedder:
    dimension = 2
    profile = "BAAI/bge-m3@locked;dimension=2"

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.calls.append(texts)
        return tuple((3.0, 4.0) for _ in texts)


def unit(unit_id: str, text: str = "same evidence") -> RetrievalUnit:
    return RetrievalUnit(
        unit_id=StableId(unit_id),
        unit_kind=RetrievalUnitKind.STATE_ANCHOR,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        text=text,
    )


@pytest.fixture
def cache_database() -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine, build_session_factory(engine)
    engine.dispose()


def test_cached_embedding_service_reuses_content_across_changed_unit_identities() -> None:
    cache = InMemoryEmbeddingCache()
    service = CachedEmbeddingService(cache, input_profile="narrative-bge-m3-v0.1")
    embedder = Embedder()

    first = service.embed_units((unit("unit.one"),), embedder)
    assert first == ((0.6, 0.8),)
    assert service.last_stats.hits == 0 and service.last_stats.misses == 1
    second = service.embed_units((unit("unit.two"),), embedder)
    assert second == first
    assert service.last_stats.hits == 1 and service.last_stats.misses == 0
    assert embedder.calls == [("same evidence",)]
    pinned = unit("unit.pinned").model_copy(
        update={"content_hash": CachedEmbeddingService.content_hash(unit("unit.original"))}
    )
    assert CachedEmbeddingService.content_hash(pinned) == pinned.content_hash

    with pytest.raises(ValueError, match="zero vector"):
        CachedEmbeddingService._normalize((0.0, 0.0), 2)
    with pytest.raises(ValueError, match="dimension"):
        CachedEmbeddingService._normalize((1.0,), 2)
    with pytest.raises(ValueError, match="not be empty"):
        CachedEmbeddingService(cache, input_profile="")
    with pytest.raises(ValueError, match="not numeric"):
        CachedEmbeddingService._normalize((True, 1.0), 2)

    class WrongCountEmbedder(Embedder):
        def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            return ()

    with pytest.raises(ValueError, match="result count"):
        CachedEmbeddingService(InMemoryEmbeddingCache(), input_profile="profile").embed_units(
            (unit("unit.missing"),), WrongCountEmbedder()
        )


def test_cached_embedding_service_deduplicates_same_content_inside_one_bulk_build() -> None:
    service = CachedEmbeddingService(
        InMemoryEmbeddingCache(), input_profile="narrative-bge-m3-v0.1"
    )
    embedder = Embedder()

    vectors = service.embed_units((unit("unit.one"), unit("unit.two")), embedder)

    assert vectors == ((0.6, 0.8), (0.6, 0.8))
    assert embedder.calls == [("same evidence",)]
    assert service.last_stats.hits == 0 and service.last_stats.misses == 1


def test_in_memory_cache_rejects_conflicting_or_wrong_dimension_entries() -> None:
    cache = InMemoryEmbeddingCache()
    key = EmbeddingCacheKey(
        content_hash=CachedEmbeddingService.content_hash(unit("unit.key")),
        embedding_profile="profile",
        input_profile="input",
    )
    cache.put(key, (1.0,))
    assert cache.get(key, dimension=1) == (1.0,)
    with pytest.raises(ValueError, match="dimension"):
        cache.get(key, dimension=2)
    with pytest.raises(ValueError, match="different vector"):
        cache.put(key, (2.0,))
    with pytest.raises(ValueError, match="empty"):
        cache.put(key, ())


def test_sql_embedding_cache_is_durable_and_validates_dimension(
    cache_database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = cache_database
    cache = SqlEmbeddingCache(factory)
    key = EmbeddingCacheKey(
        content_hash=CachedEmbeddingService.content_hash(unit("unit.sql")),
        embedding_profile="profile",
        input_profile="input",
    )
    assert cache.get(key, dimension=2) is None
    cache.put(key, (0.6, 0.8))
    assert cache.get(key, dimension=2) == (0.6, 0.8)
    cache.put(key, (0.6, 0.8))
    with pytest.raises(ValueError, match="dimension"):
        cache.get(key, dimension=3)
    with pytest.raises(ValueError, match="different vector"):
        cache.put(key, (0.8, 0.6))
    with pytest.raises(ValueError, match="empty vector"):
        cache.put(key, ())

    with factory() as session, session.begin():
        row = session.query(EmbeddingCacheRow).one()
        row.vector_json = [True, 0.0]
    with pytest.raises(ValueError, match="not numeric"):
        cache.get(key, dimension=2)
