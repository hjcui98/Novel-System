"""Atomic optimistic Project Commit service with idempotent receipts."""

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.models import (
    CommitReceiptRow,
    CommitRow,
    ProjectionOutboxRow,
    ProjectRow,
)
from novel_agent.domain.artifacts import RootManifest
from novel_agent.domain.changes import (
    CommitRequest,
    CommitResult,
    CommitStatus,
    ValidationStatus,
)
from novel_agent.domain.ids import CommitId, ProjectId


class ProjectAlreadyExistsError(RuntimeError):
    pass


class ProjectNotFoundError(LookupError):
    pass


def manifest_commit_id(manifest: RootManifest) -> CommitId:
    payload = json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return CommitId(f"sha256:{hashlib.sha256(payload).hexdigest()}")


class CommitService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def initialize_project(self, manifest: RootManifest) -> CommitId:
        if manifest.parent_commit_ids:
            raise ValueError("genesis manifest cannot have parent commits")
        commit_id = manifest_commit_id(manifest)
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            if session.get(ProjectRow, manifest.project_id.root) is not None:
                raise ProjectAlreadyExistsError(manifest.project_id.root)
            session.add(
                ProjectRow(
                    project_id=manifest.project_id.root,
                    current_commit_id=commit_id.root,
                    created_at=now,
                )
            )
            # ProjectRow and CommitRow have no ORM relationship, so establish
            # the referenced project before PostgreSQL enforces the foreign key.
            session.flush()
            session.add(
                CommitRow(
                    commit_id=commit_id.root,
                    project_id=manifest.project_id.root,
                    base_commit_id=None,
                    manifest_json=manifest.model_dump(mode="json"),
                    created_at=now,
                )
            )
            session.flush()
            self._enqueue_projection(session, manifest.project_id, commit_id, now)
        return commit_id

    def commit(self, request: CommitRequest) -> CommitResult:
        with self._session_factory() as session, session.begin():
            project = session.scalar(
                select(ProjectRow)
                .where(ProjectRow.project_id == request.project_id.root)
                .with_for_update()
            )
            if project is None:
                raise ProjectNotFoundError(request.project_id.root)

            existing = session.scalar(
                select(CommitReceiptRow).where(
                    CommitReceiptRow.project_id == request.project_id.root,
                    CommitReceiptRow.idempotency_key == request.idempotency_key.root,
                )
            )
            if existing is not None:
                return CommitResult.model_validate_json(json.dumps(existing.result_json))

            rejection = self._validate_request(request)
            if rejection is not None:
                return self._record_result(session, request, rejection)

            if project.current_commit_id != request.base_commit.root:
                result = CommitResult(
                    request_id=request.request_id,
                    status=CommitStatus.CONFLICTED,
                    reason="base commit is not the current project commit",
                )
                return self._record_result(session, request, result)

            manifest = request.bundle.proposed_roots
            commit_id = manifest_commit_id(manifest)
            committed_at = datetime.now(UTC)
            session.add(
                CommitRow(
                    commit_id=commit_id.root,
                    project_id=request.project_id.root,
                    base_commit_id=request.base_commit.root,
                    manifest_json=manifest.model_dump(mode="json"),
                    created_at=committed_at,
                )
            )
            session.flush()
            self._enqueue_projection(session, request.project_id, commit_id, committed_at)
            project.current_commit_id = commit_id.root
            result = CommitResult(
                request_id=request.request_id,
                status=CommitStatus.ACCEPTED,
                commit_id=commit_id,
                manifest=manifest,
                committed_at=committed_at,
            )
            return self._record_result(session, request, result)

    @staticmethod
    def _validate_request(request: CommitRequest) -> CommitResult | None:
        bundle = request.bundle
        report = request.validation_report
        invalid_reason: str | None = None
        if bundle.project_id != request.project_id:
            invalid_reason = "bundle project does not match request project"
        elif bundle.base_commit != request.base_commit:
            invalid_reason = "bundle base commit does not match request"
        elif bundle.observed_changes.base_commit != request.base_commit:
            invalid_reason = "observed change base commit does not match request"
        elif report.bundle_id != bundle.bundle_id:
            invalid_reason = "validation report does not reference the bundle"
        elif report.status is not ValidationStatus.PASSED:
            invalid_reason = "validation report has not passed"
        elif bundle.proposed_roots.project_id != request.project_id:
            invalid_reason = "proposed roots belong to another project"
        elif bundle.proposed_roots.parent_commit_ids != (request.base_commit,):
            invalid_reason = "proposed roots must have exactly the base commit as parent"
        if invalid_reason is None:
            return None
        return CommitResult(
            request_id=request.request_id,
            status=CommitStatus.REJECTED,
            reason=invalid_reason,
        )

    @staticmethod
    def _record_result(
        session: Session, request: CommitRequest, result: CommitResult
    ) -> CommitResult:
        session.add(
            CommitReceiptRow(
                project_id=request.project_id.root,
                idempotency_key=request.idempotency_key.root,
                result_json=result.model_dump(mode="json"),
                created_at=datetime.now(UTC),
            )
        )
        return result

    @staticmethod
    def _enqueue_projection(
        session: Session,
        project_id: ProjectId,
        commit_id: CommitId,
        created_at: datetime,
    ) -> None:
        session.add(
            ProjectionOutboxRow(
                outbox_id=f"projection.{commit_id.root.removeprefix('sha256:')}",
                project_id=project_id.root,
                source_commit=commit_id.root,
                status="pending",
                attempt_count=0,
                payload_json={
                    "project_id": project_id.root,
                    "source_commit": commit_id.root,
                },
                last_error=None,
                claimed_by=None,
                lease_expires_at=None,
                created_at=created_at,
                updated_at=created_at,
            )
        )

    def current_commit(self, project_id: ProjectId) -> CommitId:
        with self._session_factory() as session:
            project = session.get(ProjectRow, project_id.root)
            if project is None or project.current_commit_id is None:
                raise ProjectNotFoundError(project_id.root)
            return CommitId(project.current_commit_id)

    def load_manifest(self, commit_id: CommitId) -> RootManifest:
        with self._session_factory() as session:
            row = session.get(CommitRow, commit_id.root)
            if row is None:
                raise ProjectNotFoundError(commit_id.root)
            return RootManifest.model_validate_json(json.dumps(row.manifest_json))
