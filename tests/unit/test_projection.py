from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.opensearch import OpenSearchIndex
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.models import (
    DerivedSnapshotRow,
    ProjectionOutboxRow,
    R1RecordRow,
)
from novel_agent.domain.artifacts import ArtifactRef, PlanRootRef, TextRootRef, WorldRootRef
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, StableId
from novel_agent.domain.memory import (
    DerivedBuildStatus,
    DerivedSnapshotLite,
    FreshnessMode,
    FreshnessRequest,
    FreshnessStatus,
)
from novel_agent.domain.retrieval_routing import ProjectionAttestation, RetrievalBackendProfile
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.benchmark_importer import canonical_json_bytes
from novel_agent.services.commits import CommitService
from novel_agent.services.projection import (
    ArtifactProjectionSourceLoader,
    DerivedProjectionService,
    DerivedSnapshotRepository,
    FreshnessGate,
    FullDerivedProjectionBuilder,
    ProjectionOutboxRepository,
    ProjectionSource,
)
from novel_agent.services.r1 import R1WorldRepository
from novel_agent.services.search_retrieval import Stage1SearchIndexer, Stage2RSearchIndexer
from novel_agent.services.stage2_retrieval_backend import (
    Stage2RetrievalBackendError,
    build_real_hybrid_backend,
)
from tests.factories import make_manifest
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


@pytest.fixture
def projection_database() -> Iterator[tuple[Engine, sessionmaker[Session], CommitId]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    commit_id = CommitService(factory).initialize_project(make_manifest())
    yield engine, factory, commit_id
    engine.dispose()


def _snapshot(
    commit_id: CommitId,
    *,
    snapshot_id: str = "snapshot.exact",
    status: DerivedBuildStatus = DerivedBuildStatus.EXACT,
    published: bool = True,
) -> DerivedSnapshotLite:
    return DerivedSnapshotLite(
        snapshot_id=StableId(snapshot_id),
        source_commit=commit_id,
        anchor_build_id=StableId("build.anchor"),
        anchor_index_version="anchor-v1",
        grounded_index_version="grounded-v1",
        embedding_profile="embedding-v1",
        fusion_profile="rrf-v1",
        build_status=status,
        published_at=datetime(2026, 7, 21, tzinfo=UTC) if published else None,
    )


class _Builder:
    def __init__(self, snapshot: DerivedSnapshotLite | Exception) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[ProjectId, CommitId]] = []

    def build(self, project_id: ProjectId, source_commit: CommitId) -> DerivedSnapshotLite:
        self.calls.append((project_id, source_commit))
        if isinstance(self.snapshot, Exception):
            raise self.snapshot
        return self.snapshot


def test_projection_worker_publishes_exact_snapshot_idempotently(
    projection_database: tuple[Engine, sessionmaker[Session], CommitId],
) -> None:
    _, factory, commit_id = projection_database
    outbox = ProjectionOutboxRepository(factory)
    snapshot = _snapshot(commit_id)
    builder = _Builder(snapshot)
    service = DerivedProjectionService(outbox, builder)

    assert service.process_one() is True
    assert service.process_one() is False
    assert builder.calls == [(ProjectId("project.test"), commit_id)]
    assert DerivedSnapshotRepository(factory).get_for_commit(commit_id) == snapshot

    with factory() as session:
        row = session.scalar(select(ProjectionOutboxRow))
        stored = session.scalar(select(DerivedSnapshotRow))
        assert row is not None and row.status == "completed" and row.attempt_count == 1
        assert stored is not None and stored.source_commit == commit_id.root


def test_projection_failure_is_recorded_and_retried(
    projection_database: tuple[Engine, sessionmaker[Session], CommitId],
) -> None:
    _, factory, commit_id = projection_database
    outbox = ProjectionOutboxRepository(factory)
    failing = DerivedProjectionService(outbox, _Builder(RuntimeError("index unavailable")))

    with pytest.raises(RuntimeError, match="index unavailable"):
        failing.process_one()
    with factory() as session:
        row = session.scalar(select(ProjectionOutboxRow))
        assert row is not None
        assert (row.status, row.attempt_count, row.last_error) == (
            "failed",
            1,
            "index unavailable",
        )

    assert DerivedProjectionService(outbox, _Builder(_snapshot(commit_id))).process_one()
    with factory() as session:
        row = session.scalar(select(ProjectionOutboxRow))
        assert row is not None and row.status == "completed" and row.attempt_count == 2


def test_rebuilt_snapshot_can_supersede_legacy_derived_metadata(
    projection_database: tuple[Engine, sessionmaker[Session], CommitId],
) -> None:
    _, factory, commit_id = projection_database
    repository = DerivedSnapshotRepository(factory)
    first = _snapshot(commit_id)
    assert repository.publish_rebuilt(ProjectId("project.test"), first) is False
    rebuilt = first.model_copy(
        update={
            "build_profile": "stage2r-hybrid-v0.1",
            "retrieval_backend_profile": "real_hybrid",
        }
    )
    assert repository.publish_rebuilt(ProjectId("project.test"), rebuilt) is True
    assert repository.get_for_commit(commit_id) == rebuilt
    assert repository.get_attestation_for_commit(commit_id) is None

    with pytest.raises(ValueError, match="exact and published"):
        repository.publish_rebuilt(
            ProjectId("project.test"),
            _snapshot(commit_id, status=DerivedBuildStatus.PARTIAL),
        )
    with pytest.raises(ValueError, match="project does not match"):
        repository.publish_rebuilt(ProjectId("project.other"), rebuilt)


def test_missing_projection_attestation_lookup_is_empty(
    projection_database: tuple[Engine, sessionmaker[Session], CommitId],
) -> None:
    _, factory, _ = projection_database

    assert (
        DerivedSnapshotRepository(factory).get_attestation_for_commit(
            CommitId("sha256:" + "8" * 64)
        )
        is None
    )


def test_projection_claim_lease_recovers_abandoned_processing_work(
    projection_database: tuple[Engine, sessionmaker[Session], CommitId],
) -> None:
    _, factory, _ = projection_database
    outbox = ProjectionOutboxRepository(factory)
    with pytest.raises(ValueError, match="positive lease"):
        outbox.claim_next(worker_id="", lease_seconds=0)
    assert (
        outbox.claim_next(project_id=ProjectId("project.other"), worker_id="worker.other") is None
    )
    claimed = outbox.claim_next(
        worker_id="worker.one",
        lease_seconds=300,
        project_id=ProjectId("project.test"),
    )
    assert claimed is not None
    assert outbox.claim_next(worker_id="worker.two", lease_seconds=300) is None
    with factory.begin() as session:
        row = session.get(ProjectionOutboxRow, claimed[0])
        assert row is not None
        assert row.claimed_by == "worker.one" and row.lease_expires_at is not None
        row.lease_expires_at = datetime(2020, 1, 1, tzinfo=UTC)
    reclaimed = outbox.claim_next(worker_id="worker.two", lease_seconds=300)
    assert reclaimed == claimed
    with pytest.raises(RuntimeError, match="another worker"):
        outbox.fail(claimed[0], RuntimeError("late worker"), worker_id="worker.one")
    with factory() as session:
        row = session.get(ProjectionOutboxRow, claimed[0])
        assert row is not None
        assert row.claimed_by == "worker.two" and row.attempt_count == 2

    with pytest.raises(ValueError, match="positive lease"):
        DerivedProjectionService(outbox, _Builder(RuntimeError("unused")), worker_id="")


def test_projection_repository_rejects_invalid_publication_states(
    projection_database: tuple[Engine, sessionmaker[Session], CommitId],
) -> None:
    _, factory, commit_id = projection_database
    outbox = ProjectionOutboxRepository(factory)
    claimed = outbox.claim_next()
    assert claimed is not None
    outbox_id = claimed[0]

    with pytest.raises(ValueError, match="only exact"):
        outbox.complete(outbox_id, _snapshot(commit_id, status=DerivedBuildStatus.PARTIAL))
    with pytest.raises(ValueError, match="published_at"):
        outbox.complete(outbox_id, _snapshot(commit_id, published=False))
    with pytest.raises(ValueError, match="source commit"):
        outbox.complete(outbox_id, _snapshot(CommitId("sha256:" + "9" * 64)))
    with pytest.raises(LookupError, match="unknown"):
        outbox.fail("outbox.missing", RuntimeError("missing"))

    outbox.complete(outbox_id, _snapshot(commit_id))
    with pytest.raises(RuntimeError, match="not processing"):
        outbox.fail(outbox_id, RuntimeError("late"))

    with factory.begin() as session:
        row = session.scalar(select(ProjectionOutboxRow))
        assert row is not None
        row.status = "processing"
        row.claimed_by = "projection-worker.default"
    outbox.complete(outbox_id, _snapshot(commit_id))

    with factory.begin() as session:
        row = session.scalar(select(ProjectionOutboxRow))
        assert row is not None
        row.status = "processing"
        row.claimed_by = "projection-worker.default"
    with pytest.raises(ValueError, match="different snapshot"):
        outbox.complete(outbox_id, _snapshot(commit_id, snapshot_id="snapshot.other"))

    assert DerivedSnapshotRepository(factory).get_for_commit(CommitId("sha256:" + "8" * 64)) is None


def _freshness_request(
    commit_id: CommitId,
    *,
    mode: FreshnessMode = FreshnessMode.BLOCK_ON_MISMATCH,
    r1: CommitId | None = None,
    alias: CommitId | None = None,
    snapshot: DerivedSnapshotLite | None = None,
    required_snapshot: str = "snapshot.exact",
    approval: str | None = None,
) -> FreshnessRequest:
    return FreshnessRequest(
        canonical_commit=commit_id,
        r1_basis_commit=r1 or commit_id,
        required_snapshot_id=StableId(required_snapshot),
        actual_alias_commit=alias,
        actual_snapshot=snapshot,
        mode=mode,
        manual_approval_id=None if approval is None else StableId(approval),
    )


def test_freshness_gate_accepts_only_an_exact_aligned_snapshot() -> None:
    commit_id = CommitId("sha256:" + "1" * 64)
    snapshot = _snapshot(commit_id)
    decision = FreshnessGate.evaluate(
        _freshness_request(commit_id, alias=commit_id, snapshot=snapshot)
    )

    assert decision.status is FreshnessStatus.READY
    assert decision.reason == "exact snapshot ready"
    assert decision.actual_snapshot_id == snapshot.snapshot_id


@pytest.mark.parametrize(
    ("request_factory", "status", "reason"),
    [
        (
            lambda commit, other, snap: _freshness_request(
                commit, r1=other, alias=commit, snapshot=snap
            ),
            FreshnessStatus.BLOCKED,
            "R1 basis",
        ),
        (
            lambda commit, other, snap: _freshness_request(commit, alias=other, snapshot=snap),
            FreshnessStatus.BLOCKED,
            "alias commit",
        ),
        (
            lambda commit, other, snap: _freshness_request(
                commit, mode=FreshnessMode.WAIT_FOR_EXACT, alias=commit
            ),
            FreshnessStatus.WAITING,
            "not published",
        ),
        (
            lambda commit, other, snap: _freshness_request(
                commit,
                mode=FreshnessMode.DEGRADED_CANONICAL,
                alias=commit,
                snapshot=snap,
                required_snapshot="snapshot.other",
            ),
            FreshnessStatus.DEGRADED,
            "snapshot id",
        ),
        (
            lambda commit, other, snap: _freshness_request(
                commit, alias=commit, snapshot=_snapshot(other)
            ),
            FreshnessStatus.BLOCKED,
            "source commit",
        ),
        (
            lambda commit, other, snap: _freshness_request(
                commit,
                alias=commit,
                snapshot=_snapshot(commit, status=DerivedBuildStatus.PARTIAL),
            ),
            FreshnessStatus.BLOCKED,
            "not exact",
        ),
        (
            lambda commit, other, snap: _freshness_request(
                commit, alias=commit, snapshot=_snapshot(commit, published=False)
            ),
            FreshnessStatus.BLOCKED,
            "not been published",
        ),
        (
            lambda commit, other, snap: _freshness_request(
                commit,
                mode=FreshnessMode.MANUAL_OVERRIDE,
                alias=other,
                snapshot=snap,
                approval="approval.ops-17",
            ),
            FreshnessStatus.OVERRIDDEN,
            "alias commit",
        ),
    ],
)
def test_freshness_mismatches_are_explicit(
    request_factory: object, status: FreshnessStatus, reason: str
) -> None:
    commit_id = CommitId("sha256:" + "1" * 64)
    other = CommitId("sha256:" + "2" * 64)
    snapshot = _snapshot(commit_id)
    factory = request_factory
    assert callable(factory)
    decision = FreshnessGate.evaluate(factory(commit_id, other, snapshot))
    assert decision.status is status
    assert reason in decision.reason


def test_manual_override_contract_requires_exclusive_approval() -> None:
    commit_id = CommitId("sha256:" + "1" * 64)
    with pytest.raises(ValueError, match="requires an approval"):
        _freshness_request(commit_id, mode=FreshnessMode.MANUAL_OVERRIDE)
    with pytest.raises(ValueError, match="only valid"):
        _freshness_request(commit_id, approval="approval.unexpected")


def _root_ref(
    model_type: type[TextRootRef] | type[PlanRootRef] | type[WorldRootRef],
    artifact: ArtifactRef,
) -> TextRootRef | PlanRootRef | WorldRootRef:
    return model_type(
        artifact_id=artifact.artifact_id,
        media_type=artifact.media_type,
        byte_length=artifact.byte_length,
        schema_version=artifact.schema_version,
    )


def test_full_projection_loads_verified_artifacts_and_materializes_r1_and_indexes(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    bundle = make_synthetic_bundle()
    history = next(root for root in bundle.text_roots if len(root.chapters) == 20)
    plan = bundle.plan_roots[0]
    world = bundle.world_roots[0]
    text_artifact = artifacts.put(
        canonical_json_bytes(history.model_dump(mode="json")),
        "application/vnd.novel-agent.text-root+json",
        history.schema_version,
    )
    plan_artifact = artifacts.put(
        canonical_json_bytes(plan.model_dump(mode="json")),
        "application/vnd.novel-agent.plan-root+json",
        plan.schema_version,
    )
    world_artifact = artifacts.put(
        canonical_json_bytes(world.model_dump(mode="json")),
        "application/vnd.novel-agent.world-root+json",
        world.schema_version,
    )
    manifest = make_manifest().model_copy(
        update={
            "text_root": _root_ref(TextRootRef, text_artifact),
            "plan_root": _root_ref(PlanRootRef, plan_artifact),
            "world_root": _root_ref(WorldRootRef, world_artifact),
        }
    )
    commits = CommitService(factory)
    commit_id = commits.initialize_project(manifest)
    loader = ArtifactProjectionSourceLoader(commits, artifacts)
    loaded = loader.load(commit_id)
    assert loaded == ProjectionSource(manifest=manifest, text=history, plan=plan, world=world)
    search = MagicMock(spec=Stage1SearchIndexer)
    search.embedding_profile = "deterministic-hash-test-only-8d"
    search.build_and_publish.return_value = ("anchor-physical-v1", "grounded-physical-v1")
    builder = FullDerivedProjectionBuilder(
        loader,
        R1WorldRepository(factory),
        cast(Stage1SearchIndexer, search),
        fusion_profile="application-rrf-test-v1",
    )

    snapshot = builder.build(manifest.project_id, commit_id)

    assert snapshot.source_commit == commit_id
    assert snapshot.anchor_index_version == "anchor-physical-v1"
    assert snapshot.grounded_index_version == "grounded-physical-v1"
    assert snapshot.embedding_profile == "deterministic-hash-test-only-8d"
    assert snapshot.fusion_profile == "application-rrf-test-v1"
    units = search.build_and_publish.call_args.args[3]
    assert units and all(unit.source_commit == commit_id for unit in units)
    with factory() as session:
        assert session.scalar(select(R1RecordRow).limit(1)) is not None

    wrong_manifest = manifest.model_copy(update={"project_id": ProjectId("project.other")})
    wrong_loader = MagicMock()
    wrong_loader.load.return_value = ProjectionSource(
        manifest=wrong_manifest,
        text=history,
        plan=plan,
        world=world,
    )
    wrong_builder = FullDerivedProjectionBuilder(
        wrong_loader,
        R1WorldRepository(factory),
        cast(Stage1SearchIndexer, search),
    )
    with pytest.raises(ValueError, match="project mismatch"):
        wrong_builder.build(manifest.project_id, commit_id)
    engine.dispose()


class _RealProjectionEmbedder:
    dimension = 1024
    profile = "BAAI/bge-m3@5617a9f61b028005a4858fdac845db406aefb181;normalized"

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple((1.0,) * self.dimension for _ in texts)


class _RealProjectionReranker:
    profile = "locked-test-reranker"

    def score(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        return tuple(float(len(passages) - index) for index, _ in enumerate(passages))


class _WrongDimensionEmbedder(_RealProjectionEmbedder):
    dimension = 8
    profile = "real-but-wrong-dimension"


class _DeterministicProjectionEmbedder(_RealProjectionEmbedder):
    profile = "deterministic-test-1024d"


def test_real_hybrid_projection_rejects_wrong_indexer_and_embedding_profile(
    projection_database: tuple[Engine, sessionmaker[Session], CommitId],
) -> None:
    _, factory, _ = projection_database
    loader = MagicMock()
    stage1 = MagicMock(spec=Stage1SearchIndexer)
    stage1.embedding_dimension = 1024
    stage1.embedding_profile = "real"
    builder = FullDerivedProjectionBuilder(
        loader,
        R1WorldRepository(factory),
        cast(Stage1SearchIndexer, stage1),
        retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
    )
    with pytest.raises(TypeError, match="isolated Stage2R"):
        builder._require_real_hybrid_indexer()

    index = cast(OpenSearchIndex, MagicMock(spec=OpenSearchIndex))
    for embedder, message in [
        (_WrongDimensionEmbedder(), "1024-dimensional"),
        (_DeterministicProjectionEmbedder(), "deterministic test"),
    ]:
        invalid = FullDerivedProjectionBuilder(
            loader,
            R1WorldRepository(factory),
            Stage2RSearchIndexer(index, embedder),
            retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
        )
        with pytest.raises(ValueError, match=message):
            invalid._require_real_hybrid_indexer()


def test_real_hybrid_projection_requires_isolated_bulk_index_and_stores_attestation() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    manifest = make_manifest()
    commit_id = CommitService(factory).initialize_project(manifest)
    bundle = make_synthetic_bundle()
    source = ProjectionSource(
        manifest=manifest,
        text=next(root for root in bundle.text_roots if len(root.chapters) == 20),
        plan=bundle.plan_roots[0],
        world=bundle.world_roots[0],
    )
    loader = MagicMock()
    loader.load.return_value = source
    index = MagicMock(spec=OpenSearchIndex)
    search = Stage2RSearchIndexer(
        cast(OpenSearchIndex, index),
        _RealProjectionEmbedder(),
        index_namespace="run3-test",
    )
    builder = FullDerivedProjectionBuilder(
        loader,
        R1WorldRepository(factory),
        search,
        retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
        embedding_model="BAAI/bge-m3",
        embedding_revision="5617a9f61b028005a4858fdac845db406aefb181",
        embedding_runtime_fingerprint=ArtifactId("sha256:" + "f" * 64),
        reranker_model="BAAI/bge-reranker-v2-m3",
        reranker_revision="953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
    )

    snapshot = builder.build(manifest.project_id, commit_id)

    assert snapshot.retrieval_backend_profile == "real_hybrid"
    assert snapshot.projection_attestation is not None
    attestation = ProjectionAttestation.model_validate_json(
        json.dumps(snapshot.projection_attestation)
    )
    assert attestation.quality_eligible is True
    assert all("stage2r" in item.alias for item in attestation.indexes)
    assert all("run3-test" in item.alias for item in attestation.indexes)
    assert index.bulk_index.call_count == 2
    assert index.refresh.call_count == 2
    repository = DerivedSnapshotRepository(factory)
    assert repository.publish_rebuilt(manifest.project_id, snapshot) is False
    assert repository.get_attestation_for_commit(commit_id) == attestation

    missing_runtime = FullDerivedProjectionBuilder(
        loader,
        R1WorldRepository(factory),
        search,
        retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
        embedding_model="BAAI/bge-m3",
        embedding_revision="locked",
    )
    with pytest.raises(ValueError, match="locked embedding runtime"):
        missing_runtime.build(manifest.project_id, commit_id)

    backend = build_real_hybrid_backend(
        r1=R1WorldRepository(factory),
        search_index=cast(OpenSearchIndex, index),
        embedder=_RealProjectionEmbedder(),
        project_id=manifest.project_id,
        source_commit=commit_id,
        snapshot_id=snapshot.snapshot_id,
        attestation=attestation,
        reranker=_RealProjectionReranker(),
    )
    assert "anchor_dense" in {channel.value for channel in backend.allowed_channels}
    with pytest.raises(Stage2RetrievalBackendError, match="basis"):
        build_real_hybrid_backend(
            r1=R1WorldRepository(factory),
            search_index=cast(OpenSearchIndex, index),
            embedder=_RealProjectionEmbedder(),
            project_id=manifest.project_id,
            source_commit=CommitId("sha256:" + "0" * 64),
            snapshot_id=snapshot.snapshot_id,
            attestation=attestation,
            reranker=_RealProjectionReranker(),
        )
    engine.dispose()
