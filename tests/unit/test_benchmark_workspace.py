from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.models import CommitRow, ProjectRow
from novel_agent.domain.ids import CommitId, ProjectId
from novel_agent.services.benchmark_workspace import (
    BenchmarkWorkspaceConflictError,
    BenchmarkWorkspaceRepository,
)
from tests.factories import make_manifest


@pytest.fixture
def workspace_repository() -> Iterator[tuple[BenchmarkWorkspaceRepository, sessionmaker[Session]]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    yield BenchmarkWorkspaceRepository(factory), factory
    engine.dispose()


def test_imported_basis_is_registered_and_idempotent(
    workspace_repository: tuple[BenchmarkWorkspaceRepository, sessionmaker[Session]],
) -> None:
    repository, factory = workspace_repository
    project_id = ProjectId("project.benchmark.workspace")
    source_commit = CommitId("sha256:" + "a" * 64)
    manifest = make_manifest(project_id)

    assert repository.ensure_imported_basis(project_id, source_commit, manifest) == project_id
    assert repository.ensure_imported_basis(project_id, source_commit, manifest) == project_id
    with factory() as session:
        assert session.get(ProjectRow, project_id.root) is not None
        assert session.get(CommitRow, source_commit.root) is not None


def test_existing_source_commit_may_be_reused_only_for_the_same_world(
    workspace_repository: tuple[BenchmarkWorkspaceRepository, sessionmaker[Session]],
) -> None:
    repository, _ = workspace_repository
    first_project = ProjectId("project.benchmark.first")
    other_project = ProjectId("project.benchmark.other")
    source_commit = CommitId("sha256:" + "b" * 64)
    first = make_manifest(first_project)
    repository.ensure_imported_basis(first_project, source_commit, first)

    same_world = first.model_copy(update={"project_id": other_project})
    assert (
        repository.ensure_imported_basis(other_project, source_commit, same_world) == first_project
    )
    different_world = make_manifest(other_project).model_copy(
        update={
            "world_root": first.world_root.model_copy(update={"artifact_id": "sha256:" + "f" * 64})
        }
    )
    with pytest.raises(BenchmarkWorkspaceConflictError, match="another WorldRoot"):
        repository.ensure_imported_basis(other_project, source_commit, different_world)


def test_workspace_rejects_invalid_manifest_and_project_collision(
    workspace_repository: tuple[BenchmarkWorkspaceRepository, sessionmaker[Session]],
) -> None:
    repository, factory = workspace_repository
    project_id = ProjectId("project.benchmark.collision")
    source_commit = CommitId("sha256:" + "c" * 64)
    manifest = make_manifest(project_id)
    with pytest.raises(ValueError, match="parentless"):
        repository.ensure_imported_basis(
            project_id,
            source_commit,
            manifest.model_copy(update={"parent_commit_ids": (source_commit,)}),
        )

    with factory() as session, session.begin():
        session.add(
            ProjectRow(
                project_id=project_id.root,
                current_commit_id=None,
                created_at=datetime.now(UTC),
            )
        )
    with pytest.raises(BenchmarkWorkspaceConflictError, match="exists without"):
        repository.ensure_imported_basis(project_id, source_commit, manifest)


def test_workspace_rejects_corrupt_manifest_for_existing_source_commit(
    workspace_repository: tuple[BenchmarkWorkspaceRepository, sessionmaker[Session]],
) -> None:
    repository, factory = workspace_repository
    project_id = ProjectId("project.benchmark.corrupt")
    source_commit = CommitId("sha256:" + "d" * 64)
    with factory() as session, session.begin():
        now = datetime.now(UTC)
        session.add(
            ProjectRow(
                project_id=project_id.root,
                current_commit_id=source_commit.root,
                created_at=now,
            )
        )
        session.flush()
        session.add(
            CommitRow(
                commit_id=source_commit.root,
                project_id=project_id.root,
                base_commit_id=None,
                manifest_json={"invalid": True},
                created_at=now,
            )
        )

    with pytest.raises(BenchmarkWorkspaceConflictError, match="valid RootManifest"):
        repository.ensure_imported_basis(
            project_id,
            source_commit,
            make_manifest(project_id),
        )
