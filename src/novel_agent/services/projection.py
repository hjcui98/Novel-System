"""Outbox-driven derived projection and explicit freshness decisions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.models import DerivedSnapshotRow, ProjectionOutboxRow
from novel_agent.domain.artifacts import RootManifest
from novel_agent.domain.benchmark import PlanRootDocument, TextRootDocument
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, StableId
from novel_agent.domain.memory import (
    DerivedBuildStatus,
    DerivedSnapshotLite,
    FreshnessDecision,
    FreshnessMode,
    FreshnessRequest,
    FreshnessStatus,
    RetrievalChannel,
    WorldRootDocument,
)
from novel_agent.domain.retrieval_routing import (
    ChannelCoverage,
    L2IndexKind,
    L2IndexManifest,
    ProjectionAttestation,
    RetrievalBackendProfile,
    SnapshotCapability,
    SnapshotCapabilityStatus,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.memory_pipeline import AnchorBuilder
from novel_agent.services.r1 import R1WorldRepository
from novel_agent.services.search_retrieval import (
    SearchIndexBuildReceipt,
    Stage1SearchIndexer,
    Stage2RSearchIndexer,
)


class ProjectionBuilder(Protocol):
    def build(self, project_id: ProjectId, source_commit: CommitId) -> DerivedSnapshotLite: ...


def snapshot_id_for_commit(commit_id: CommitId) -> StableId:
    return StableId(f"snapshot.{commit_id.root.removeprefix('sha256:')}")


@dataclass(frozen=True, slots=True)
class ProjectionSource:
    manifest: RootManifest
    text: TextRootDocument
    plan: PlanRootDocument | None
    world: WorldRootDocument


class ProjectionSourceLoader(Protocol):
    def load(self, source_commit: CommitId) -> ProjectionSource: ...


class ArtifactProjectionSourceLoader:
    def __init__(self, commits: CommitService, artifacts: ArtifactRepository) -> None:
        self._commits = commits
        self._artifacts = artifacts

    def load(self, source_commit: CommitId) -> ProjectionSource:
        manifest = self._commits.load_manifest(source_commit)
        text = TextRootDocument.model_validate_json(
            self._artifacts.read_verified(manifest.text_root), strict=True
        )
        plan = PlanRootDocument.model_validate_json(
            self._artifacts.read_verified(manifest.plan_root), strict=True
        )
        world = WorldRootDocument.model_validate_json(
            self._artifacts.read_verified(manifest.world_root), strict=True
        )
        return ProjectionSource(manifest=manifest, text=text, plan=plan, world=world)


class FullDerivedProjectionBuilder:
    """Build R1 plus both search indexes before publishing an exact snapshot."""

    def __init__(
        self,
        loader: ProjectionSourceLoader,
        r1: R1WorldRepository,
        search: Stage1SearchIndexer,
        *,
        fusion_profile: str = "application-rrf-v1",
        retrieval_backend_profile: RetrievalBackendProfile = RetrievalBackendProfile.SCRIPTED_SMOKE,
        build_profile: str = "stage2r-hybrid-v0.1",
        embedding_model: str | None = None,
        embedding_revision: str | None = None,
        embedding_runtime_fingerprint: ArtifactId | None = None,
        reranker_model: str | None = None,
        reranker_revision: str | None = None,
    ) -> None:
        self._loader = loader
        self._r1 = r1
        self._search = search
        self._fusion_profile = fusion_profile
        self._retrieval_backend_profile = retrieval_backend_profile
        self._build_profile = build_profile
        self._embedding_model = embedding_model
        self._embedding_revision = embedding_revision
        self._embedding_runtime_fingerprint = embedding_runtime_fingerprint
        self._reranker_model = reranker_model
        self._reranker_revision = reranker_revision

    def build(self, project_id: ProjectId, source_commit: CommitId) -> DerivedSnapshotLite:
        source = self._loader.load(source_commit)
        if source.manifest.project_id != project_id:
            raise ValueError("projection source project mismatch")
        snapshot_id = snapshot_id_for_commit(source_commit)
        units = AnchorBuilder().build(
            source.world,
            source.text,
            source.plan,
            snapshot_id=snapshot_id,
            canonical_commit=source_commit,
        )
        self._r1.materialize(project_id, source_commit, source.world, source.plan)
        if self._retrieval_backend_profile is RetrievalBackendProfile.REAL_HYBRID:
            self._require_real_hybrid_indexer()
            r1_records, r1_associations, graph_edges = self._r1.counts(source_commit)
            search_receipt = self._search.build_and_publish_receipt(
                project_id, source_commit, snapshot_id, units
            )
            attestation = self._attestation(
                project_id,
                source_commit,
                snapshot_id,
                r1_records,
                r1_associations,
                graph_edges,
                search_receipt,
            )
            anchor_index = search_receipt.anchor_index
            grounded_index = search_receipt.grounded_index
        else:
            anchor_index, grounded_index = self._search.build_and_publish(
                project_id, source_commit, snapshot_id, units
            )
            attestation = None
        digest = hashlib.sha256(
            canonical_json_bytes([unit.model_dump(mode="json") for unit in units])
        ).hexdigest()[:24]
        return DerivedSnapshotLite(
            snapshot_id=snapshot_id,
            source_commit=source_commit,
            anchor_build_id=StableId(f"anchor.{digest}"),
            anchor_index_version=anchor_index,
            grounded_index_version=grounded_index,
            embedding_profile=self._search.embedding_profile,
            fusion_profile=self._fusion_profile,
            build_status=DerivedBuildStatus.EXACT,
            build_profile=self._build_profile,
            retrieval_backend_profile=self._retrieval_backend_profile.value,
            projection_attestation=(
                None if attestation is None else attestation.model_dump(mode="json")
            ),
            published_at=datetime.now(UTC),
        )

    def _attestation(
        self,
        project_id: ProjectId,
        source_commit: CommitId,
        snapshot_id: StableId,
        r1_records: int,
        r1_associations: int,
        graph_edges: int,
        search_receipt: SearchIndexBuildReceipt,
    ) -> ProjectionAttestation:
        if not (
            self._embedding_model
            and self._embedding_revision
            and self._embedding_runtime_fingerprint is not None
        ):
            raise ValueError("real-hybrid projection requires locked embedding runtime attestation")
        if self._search.embedding_dimension != 1024:
            raise ValueError("real-hybrid projection requires 1024-dimensional embeddings")
        if "deterministic" in self._search.embedding_profile:
            raise ValueError("real-hybrid projection cannot use deterministic test embeddings")
        anchor_count = search_receipt.anchor_document_count
        grounded_count = search_receipt.grounded_document_count
        channels = (
            RetrievalChannel.R1_EXACT,
            RetrievalChannel.R1_TEMPORAL,
            RetrievalChannel.ANCHOR_BM25,
            RetrievalChannel.ANCHOR_DENSE,
            RetrievalChannel.GROUNDED_BM25,
            RetrievalChannel.GROUNDED_DENSE,
            RetrievalChannel.HIERARCHY,
            RetrievalChannel.TYPED_GRAPH,
        )
        coverage = (
            ChannelCoverage(
                channel=RetrievalChannel.R1_EXACT,
                expected_units=r1_records,
                ready_units=r1_records,
            ),
            ChannelCoverage(
                channel=RetrievalChannel.R1_TEMPORAL,
                expected_units=r1_records,
                ready_units=r1_records,
            ),
            ChannelCoverage(
                channel=RetrievalChannel.ANCHOR_BM25,
                expected_units=anchor_count,
                ready_units=anchor_count,
            ),
            ChannelCoverage(
                channel=RetrievalChannel.ANCHOR_DENSE,
                expected_units=anchor_count,
                ready_units=anchor_count,
            ),
            ChannelCoverage(
                channel=RetrievalChannel.GROUNDED_BM25,
                expected_units=grounded_count,
                ready_units=grounded_count,
            ),
            ChannelCoverage(
                channel=RetrievalChannel.GROUNDED_DENSE,
                expected_units=grounded_count,
                ready_units=grounded_count,
            ),
            ChannelCoverage(
                channel=RetrievalChannel.HIERARCHY,
                expected_units=anchor_count,
                ready_units=anchor_count,
            ),
            ChannelCoverage(
                channel=RetrievalChannel.TYPED_GRAPH,
                expected_units=graph_edges,
                ready_units=graph_edges,
            ),
        )
        mapping_hash = ArtifactId(f"sha256:{search_receipt.mapping_hash}")
        anchor_alias, grounded_alias = self._search.aliases(project_id)
        return ProjectionAttestation(
            attestation_id=StableId(
                f"attestation.stage2r.{source_commit.root.removeprefix('sha256:')[:24]}"
            ),
            retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
            source_commit=source_commit,
            snapshot_id=snapshot_id,
            capability=SnapshotCapability(
                source_commit=source_commit,
                snapshot_id=snapshot_id,
                status=SnapshotCapabilityStatus.EXACT,
                available_channels=channels,
                coverage_by_channel=coverage,
                embedding_profile=self._search.embedding_profile,
                graph_profile="postgresql-r1-versioned-edges-v0.1",
            ),
            r1_record_count=r1_records,
            r1_entity_association_count=r1_associations,
            graph_node_count=r1_records,
            graph_edge_count=graph_edges,
            embedding_cache_hits=search_receipt.embedding_cache.hits,
            embedding_cache_misses=search_receipt.embedding_cache.misses,
            indexes=(
                L2IndexManifest(
                    index_id=StableId(f"index.stage2r.anchor.{snapshot_id.root[-16:]}"),
                    index_kind=L2IndexKind.ANCHOR,
                    source_commit=source_commit,
                    snapshot_id=snapshot_id,
                    physical_name=search_receipt.anchor_index,
                    alias=anchor_alias,
                    document_count=anchor_count,
                    mapping_hash=mapping_hash,
                    analyzer_profile="standard-cjk-exact-v0.1",
                    embedding_profile=self._search.embedding_input_profile,
                ),
                L2IndexManifest(
                    index_id=StableId(f"index.stage2r.grounded.{snapshot_id.root[-16:]}"),
                    index_kind=L2IndexKind.GROUNDED,
                    source_commit=source_commit,
                    snapshot_id=snapshot_id,
                    physical_name=search_receipt.grounded_index,
                    alias=grounded_alias,
                    document_count=grounded_count,
                    mapping_hash=mapping_hash,
                    analyzer_profile="standard-cjk-exact-v0.1",
                    embedding_profile=self._search.embedding_input_profile,
                ),
            ),
            embedding_model=self._embedding_model,
            embedding_revision=self._embedding_revision,
            embedding_dimension=self._search.embedding_dimension,
            embedding_normalized=True,
            embedding_runtime_fingerprint=self._embedding_runtime_fingerprint,
            reranker_model=self._reranker_model,
            reranker_revision=self._reranker_revision,
        )

    def _require_real_hybrid_indexer(self) -> None:
        if not isinstance(self._search, Stage2RSearchIndexer):
            raise TypeError("real-hybrid projection requires the isolated Stage2RSearchIndexer")
        if self._search.embedding_dimension != 1024:
            raise ValueError("real-hybrid projection requires 1024-dimensional embeddings")
        if "deterministic" in self._search.embedding_profile:
            raise ValueError("real-hybrid projection cannot use deterministic test embeddings")


class ProjectionOutboxRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def claim_next(
        self,
        *,
        worker_id: str = "projection-worker.default",
        lease_seconds: int = 300,
        project_id: ProjectId | None = None,
    ) -> tuple[str, ProjectId, CommitId] | None:
        if not worker_id or lease_seconds < 1:
            raise ValueError("projection claim requires worker id and positive lease")
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            statement = select(ProjectionOutboxRow).where(
                or_(
                    ProjectionOutboxRow.status.in_(("pending", "failed")),
                    and_(
                        ProjectionOutboxRow.status == "processing",
                        ProjectionOutboxRow.lease_expires_at.is_not(None),
                        ProjectionOutboxRow.lease_expires_at < now,
                    ),
                )
            )
            if project_id is not None:
                statement = statement.where(ProjectionOutboxRow.project_id == project_id.root)
            row = session.scalar(
                statement.order_by(
                    ProjectionOutboxRow.created_at, ProjectionOutboxRow.outbox_id
                ).with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            row.status = "processing"
            row.attempt_count += 1
            row.last_error = None
            row.claimed_by = worker_id
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            row.updated_at = now
            return row.outbox_id, ProjectId(row.project_id), CommitId(row.source_commit)

    def complete(
        self,
        outbox_id: str,
        snapshot: DerivedSnapshotLite,
        *,
        worker_id: str = "projection-worker.default",
    ) -> None:
        if snapshot.build_status is not DerivedBuildStatus.EXACT:
            raise ValueError("only exact snapshots may be published")
        if snapshot.published_at is None:
            raise ValueError("published snapshot requires published_at")
        with self._session_factory() as session, session.begin():
            row = self._require_processing(session, outbox_id, worker_id)
            if snapshot.source_commit.root != row.source_commit:
                raise ValueError("snapshot source commit does not match outbox")
            existing = session.scalar(
                select(DerivedSnapshotRow).where(
                    DerivedSnapshotRow.source_commit == row.source_commit
                )
            )
            payload = snapshot.model_dump(mode="json")
            if existing is None:
                session.add(
                    DerivedSnapshotRow(
                        snapshot_id=snapshot.snapshot_id.root,
                        project_id=row.project_id,
                        source_commit=row.source_commit,
                        build_status=snapshot.build_status.value,
                        snapshot_json=payload,
                        published_at=snapshot.published_at,
                    )
                )
            elif existing.snapshot_id != snapshot.snapshot_id.root:
                raise ValueError("source commit already has a different snapshot")
            else:
                existing.build_status = snapshot.build_status.value
                existing.snapshot_json = payload
                existing.published_at = snapshot.published_at
            row.status = "completed"
            row.claimed_by = None
            row.lease_expires_at = None
            row.updated_at = datetime.now(UTC)

    def fail(
        self,
        outbox_id: str,
        error: Exception,
        *,
        worker_id: str = "projection-worker.default",
    ) -> None:
        with self._session_factory() as session, session.begin():
            row = self._require_processing(session, outbox_id, worker_id)
            row.status = "failed"
            row.last_error = str(error)[:2048] or type(error).__name__
            row.claimed_by = None
            row.lease_expires_at = None
            row.updated_at = datetime.now(UTC)

    @staticmethod
    def _require_processing(
        session: Session, outbox_id: str, worker_id: str
    ) -> ProjectionOutboxRow:
        row = session.get(ProjectionOutboxRow, outbox_id)
        if row is None:
            raise LookupError(f"unknown projection outbox: {outbox_id}")
        if row.status != "processing":
            raise RuntimeError(f"projection outbox is not processing: {outbox_id}")
        if row.claimed_by != worker_id:
            raise RuntimeError(f"projection outbox lease is owned by another worker: {outbox_id}")
        return row


class DerivedSnapshotRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get_for_commit(self, source_commit: CommitId) -> DerivedSnapshotLite | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(DerivedSnapshotRow).where(
                    DerivedSnapshotRow.source_commit == source_commit.root
                )
            )
            if row is None:
                return None
            return DerivedSnapshotLite.model_validate_json(json.dumps(row.snapshot_json))

    def get_attestation_for_commit(self, source_commit: CommitId) -> ProjectionAttestation | None:
        """Load the typed Stage 2R receipt without treating it as a source of truth."""

        with self._session_factory() as session:
            row = session.scalar(
                select(DerivedSnapshotRow).where(
                    DerivedSnapshotRow.source_commit == source_commit.root
                )
            )
            if row is None:
                return None
            raw = row.snapshot_json.get("projection_attestation")
            if raw is None:
                return None
            return ProjectionAttestation.model_validate_json(json.dumps(raw))

    def publish_rebuilt(self, project_id: ProjectId, snapshot: DerivedSnapshotLite) -> bool:
        """Atomically replace a metadata-only snapshot after a verified rebuild.

        The row remains a derived cache entry keyed by the immutable source commit;
        replacing it never changes a Canonical Root or the commit chain.
        """

        if snapshot.build_status is not DerivedBuildStatus.EXACT or snapshot.published_at is None:
            raise ValueError("rebuilt snapshot must be exact and published")
        payload = snapshot.model_dump(mode="json")
        with self._session_factory() as session, session.begin():
            existing = session.scalar(
                select(DerivedSnapshotRow).where(
                    DerivedSnapshotRow.source_commit == snapshot.source_commit.root
                )
            )
            if existing is None:
                session.add(
                    DerivedSnapshotRow(
                        snapshot_id=snapshot.snapshot_id.root,
                        project_id=project_id.root,
                        source_commit=snapshot.source_commit.root,
                        build_status=snapshot.build_status.value,
                        snapshot_json=payload,
                        published_at=snapshot.published_at,
                    )
                )
                return False
            if existing.project_id != project_id.root:
                raise ValueError("rebuilt snapshot project does not match existing source commit")
            existing.snapshot_id = snapshot.snapshot_id.root
            existing.build_status = snapshot.build_status.value
            existing.snapshot_json = payload
            existing.published_at = snapshot.published_at
            return True


class DerivedProjectionService:
    def __init__(
        self,
        outbox: ProjectionOutboxRepository,
        builder: ProjectionBuilder,
        *,
        worker_id: str = "projection-worker.default",
        lease_seconds: int = 300,
        project_id: ProjectId | None = None,
    ) -> None:
        if not worker_id or lease_seconds < 1:
            raise ValueError("projection worker requires id and positive lease")
        self._outbox = outbox
        self._builder = builder
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._project_id = project_id

    def process_one(self) -> bool:
        claimed = self._outbox.claim_next(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            project_id=self._project_id,
        )
        if claimed is None:
            return False
        outbox_id, project_id, source_commit = claimed
        try:
            snapshot = self._builder.build(project_id, source_commit)
            self._outbox.complete(outbox_id, snapshot, worker_id=self._worker_id)
        except Exception as exc:
            self._outbox.fail(outbox_id, exc, worker_id=self._worker_id)
            raise
        return True

    def process_all(self, *, max_items: int = 1000) -> int:
        if max_items < 1:
            raise ValueError("max_items must be positive")
        processed = 0
        while processed < max_items and self.process_one():
            processed += 1
        return processed


class FreshnessGate:
    @staticmethod
    def evaluate(request: FreshnessRequest) -> FreshnessDecision:
        snapshot = request.actual_snapshot
        exact = (
            request.canonical_commit == request.r1_basis_commit
            and request.actual_alias_commit == request.canonical_commit
            and snapshot is not None
            and snapshot.snapshot_id == request.required_snapshot_id
            and snapshot.source_commit == request.canonical_commit
            and snapshot.build_status is DerivedBuildStatus.EXACT
            and snapshot.published_at is not None
        )
        if exact:
            return FreshnessGate._decision(request, FreshnessStatus.READY, "exact snapshot ready")

        reason = FreshnessGate._mismatch_reason(request)
        status_by_mode = {
            FreshnessMode.WAIT_FOR_EXACT: FreshnessStatus.WAITING,
            FreshnessMode.DEGRADED_CANONICAL: FreshnessStatus.DEGRADED,
            FreshnessMode.BLOCK_ON_MISMATCH: FreshnessStatus.BLOCKED,
            FreshnessMode.MANUAL_OVERRIDE: FreshnessStatus.OVERRIDDEN,
        }
        return FreshnessGate._decision(request, status_by_mode[request.mode], reason)

    @staticmethod
    def _mismatch_reason(request: FreshnessRequest) -> str:
        snapshot = request.actual_snapshot
        if request.canonical_commit != request.r1_basis_commit:
            return "R1 basis commit differs from canonical commit"
        if request.actual_alias_commit != request.canonical_commit:
            return "index alias commit differs from canonical commit"
        if snapshot is None:
            return "required derived snapshot is not published"
        if snapshot.snapshot_id != request.required_snapshot_id:
            return "published snapshot id differs from required snapshot"
        if snapshot.source_commit != request.canonical_commit:
            return "snapshot source commit differs from canonical commit"
        if snapshot.build_status is not DerivedBuildStatus.EXACT:
            return "derived snapshot is not exact"
        return "derived snapshot has not been published"

    @staticmethod
    def _decision(
        request: FreshnessRequest, status: FreshnessStatus, reason: str
    ) -> FreshnessDecision:
        snapshot = request.actual_snapshot
        return FreshnessDecision(
            status=status,
            canonical_commit=request.canonical_commit,
            r1_basis_commit=request.r1_basis_commit,
            required_snapshot_id=request.required_snapshot_id,
            actual_alias_commit=request.actual_alias_commit,
            actual_snapshot_id=None if snapshot is None else snapshot.snapshot_id,
            actual_snapshot_commit=None if snapshot is None else snapshot.source_commit,
            reason=reason,
            manual_approval_id=request.manual_approval_id,
        )
