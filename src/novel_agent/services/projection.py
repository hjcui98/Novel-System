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
from novel_agent.domain.ids import CommitId, ProjectId, StableId
from novel_agent.domain.memory import (
    DerivedBuildStatus,
    DerivedSnapshotLite,
    FreshnessDecision,
    FreshnessMode,
    FreshnessRequest,
    FreshnessStatus,
    WorldRootDocument,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.memory_pipeline import AnchorBuilder
from novel_agent.services.r1 import R1WorldRepository
from novel_agent.services.search_retrieval import Stage1SearchIndexer


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
    ) -> None:
        self._loader = loader
        self._r1 = r1
        self._search = search
        self._fusion_profile = fusion_profile

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
        self._r1.materialize(project_id, source_commit, source.world)
        anchor_index, grounded_index = self._search.build_and_publish(
            project_id, source_commit, snapshot_id, units
        )
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
            published_at=datetime.now(UTC),
        )


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
