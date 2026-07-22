"""Idempotent PostgreSQL registration of an externally verified benchmark basis."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.models import CommitRow, ProjectRow
from novel_agent.domain.artifacts import RootManifest
from novel_agent.domain.ids import CommitId, ProjectId


class BenchmarkWorkspaceConflictError(RuntimeError):
    pass


class BenchmarkWorkspaceRepository:
    """Register an imported read-only basis without synthesizing a new canonical commit id."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def ensure_imported_basis(
        self,
        workspace_project_id: ProjectId,
        source_commit: CommitId,
        manifest: RootManifest,
    ) -> ProjectId:
        if manifest.project_id != workspace_project_id or manifest.parent_commit_ids:
            raise ValueError("benchmark workspace manifest must be a parentless workspace basis")
        with self._session_factory() as session, session.begin():
            existing_commit = session.get(CommitRow, source_commit.root)
            if existing_commit is not None:
                try:
                    stored = RootManifest.model_validate_json(
                        json.dumps(existing_commit.manifest_json)
                    )
                except ValueError as error:
                    raise BenchmarkWorkspaceConflictError(
                        "source commit does not contain a valid RootManifest"
                    ) from error
                if stored.world_root.artifact_id != manifest.world_root.artifact_id:
                    raise BenchmarkWorkspaceConflictError(
                        "source commit already refers to another WorldRoot"
                    )
                return ProjectId(existing_commit.project_id)

            existing_project = session.get(ProjectRow, workspace_project_id.root)
            if existing_project is not None:
                raise BenchmarkWorkspaceConflictError(
                    "benchmark workspace project exists without the imported source commit"
                )

            now = datetime.now(UTC)
            session.add(
                ProjectRow(
                    project_id=workspace_project_id.root,
                    current_commit_id=source_commit.root,
                    created_at=now,
                )
            )
            session.flush()
            session.add(
                CommitRow(
                    commit_id=source_commit.root,
                    project_id=workspace_project_id.root,
                    base_commit_id=None,
                    manifest_json=manifest.model_dump(mode="json"),
                    created_at=now,
                )
            )
            return workspace_project_id
