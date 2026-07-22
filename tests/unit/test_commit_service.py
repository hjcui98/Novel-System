from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.database import (
    Base,
    build_engine,
    build_session_factory,
    transactional_session,
)
from novel_agent.adapters.postgres.models import (
    CommitReceiptRow,
    CommitRow,
    ProjectionOutboxRow,
    ProjectRow,
)
from novel_agent.domain.changes import CommitStatus, ValidationStatus
from novel_agent.domain.ids import CommitId, ProjectId, StableId
from novel_agent.services.commits import (
    CommitService,
    ProjectAlreadyExistsError,
    ProjectNotFoundError,
    manifest_commit_id,
)
from tests.factories import make_commit_request, make_manifest


@pytest.fixture
def database() -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine, build_session_factory(engine)
    engine.dispose()


def test_database_helpers_create_sessions_and_transactions() -> None:
    engine = build_engine("sqlite+pysqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)

    with transactional_session(factory) as session:
        session.add(
            ProjectRow(project_id="project.helper", current_commit_id=None, created_at=_now())
        )

    with factory() as session:
        assert session.get(ProjectRow, "project.helper") is not None
    engine.dispose()


def _now() -> datetime:
    return datetime(2026, 7, 20, tzinfo=UTC)


def test_initialize_project_creates_deterministic_genesis_and_reads_it(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database
    service = CommitService(factory)
    manifest = make_manifest()

    commit_id = service.initialize_project(manifest)

    assert commit_id == manifest_commit_id(manifest)
    assert service.current_commit(manifest.project_id) == commit_id
    assert service.load_manifest(commit_id) == manifest
    with factory() as session:
        outbox = session.scalar(select(ProjectionOutboxRow))
        assert outbox is not None
        assert outbox.source_commit == commit_id.root
        assert outbox.status == "pending"

    with pytest.raises(ProjectAlreadyExistsError):
        service.initialize_project(manifest)
    with pytest.raises(ValueError, match="genesis"):
        service.initialize_project(make_manifest(parent_commit_ids=(commit_id,)))


def test_commit_is_atomic_and_idempotent(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database
    service = CommitService(factory)
    genesis = service.initialize_project(make_manifest())
    request = make_commit_request(genesis)

    first = service.commit(request)
    second = service.commit(request)

    assert first == second
    assert first.status is CommitStatus.ACCEPTED
    assert first.commit_id is not None
    assert service.current_commit(ProjectId("project.test")) == first.commit_id
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(CommitRow)) == 2
        assert session.scalar(select(func.count()).select_from(CommitReceiptRow)) == 1
        assert session.scalar(select(func.count()).select_from(ProjectionOutboxRow)) == 2


def test_stale_base_is_conflicted_and_receipt_is_replayed(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database
    service = CommitService(factory)
    genesis = service.initialize_project(make_manifest())
    accepted = service.commit(make_commit_request(genesis, idempotency_key="commit.key.first"))
    assert accepted.status is CommitStatus.ACCEPTED
    stale = make_commit_request(genesis, idempotency_key="commit.key.stale", root_offset=10)

    result = service.commit(stale)

    assert result.status is CommitStatus.CONFLICTED
    assert service.commit(stale) == result


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("bundle_project", "bundle project"),
        ("bundle_base", "bundle base"),
        ("observed_base", "observed change base"),
        ("report_bundle", "validation report"),
        ("report_status", "has not passed"),
        ("root_project", "proposed roots"),
        ("root_parent", "exactly the base"),
    ],
)
def test_invalid_commit_contracts_are_rejected_idempotently(
    database: tuple[Engine, sessionmaker[Session]], case: str, expected_reason: str
) -> None:
    _, factory = database
    service = CommitService(factory)
    genesis = service.initialize_project(make_manifest())
    request = make_commit_request(genesis, idempotency_key=f"invalid.{case}")
    other_project = ProjectId("project.other")
    other_commit = CommitId("sha256:" + "9" * 64)

    if case == "bundle_project":
        request = request.model_copy(
            update={"bundle": request.bundle.model_copy(update={"project_id": other_project})}
        )
    elif case == "bundle_base":
        request = request.model_copy(
            update={"bundle": request.bundle.model_copy(update={"base_commit": other_commit})}
        )
    elif case == "observed_base":
        observed = request.bundle.observed_changes.model_copy(update={"base_commit": other_commit})
        request = request.model_copy(
            update={"bundle": request.bundle.model_copy(update={"observed_changes": observed})}
        )
    elif case == "report_bundle":
        report = request.validation_report.model_copy(
            update={"bundle_id": StableId("bundle.other")}
        )
        request = request.model_copy(update={"validation_report": report})
    elif case == "report_status":
        report = request.validation_report.model_copy(update={"status": ValidationStatus.FAILED})
        request = request.model_copy(update={"validation_report": report})
    elif case == "root_project":
        roots = request.bundle.proposed_roots.model_copy(update={"project_id": other_project})
        request = request.model_copy(
            update={"bundle": request.bundle.model_copy(update={"proposed_roots": roots})}
        )
    elif case == "root_parent":
        roots = request.bundle.proposed_roots.model_copy(update={"parent_commit_ids": ()})
        request = request.model_copy(
            update={"bundle": request.bundle.model_copy(update={"proposed_roots": roots})}
        )

    result = service.commit(request)

    assert result.status is CommitStatus.REJECTED
    assert result.reason is not None and expected_reason in result.reason
    assert service.commit(request) == result


def test_missing_projects_and_commits_are_reported(
    database: tuple[Engine, sessionmaker[Session]],
) -> None:
    _, factory = database
    service = CommitService(factory)
    missing_commit = CommitId("sha256:" + "0" * 64)

    with pytest.raises(ProjectNotFoundError):
        service.commit(make_commit_request(missing_commit, project_id=ProjectId("project.missing")))
    with pytest.raises(ProjectNotFoundError):
        service.current_commit(ProjectId("project.missing"))
    with pytest.raises(ProjectNotFoundError):
        service.load_manifest(missing_commit)

    with factory.begin() as session:
        session.add(
            ProjectRow(project_id="project.empty", current_commit_id=None, created_at=_now())
        )
    with pytest.raises(ProjectNotFoundError):
        service.current_commit(ProjectId("project.empty"))
