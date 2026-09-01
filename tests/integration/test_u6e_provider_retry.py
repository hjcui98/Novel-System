from __future__ import annotations

from pathlib import Path

import pytest
from scripts.run_u6b_production_baseline import _retry_provider_transient_task
from sqlalchemy import create_engine

from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.domain.creative_runtime import (
    AutomationMode,
    CreativeRunPolicy,
    CreativeRunRequest,
)
from novel_agent.domain.ids import CommitId, ProjectId, RunId, TaskId
from novel_agent.domain.runtime import (
    AttemptOutcome,
    FailureClass,
    TaskKind,
    TaskRecord,
    TaskStatus,
)
from novel_agent.services.commits import CommitService
from novel_agent.services.event_log import RunEventLogRepository
from novel_agent.services.runtime_commands import RuntimeCommandService
from tests.factories import make_manifest

PERMISSION_HASH = "sha256:" + "2" * 64
POLICY_HASH = "sha256:" + "1" * 64


def _request(project_id: ProjectId, run_id: RunId, basis: CommitId) -> CreativeRunRequest:
    return CreativeRunRequest(
        run_id=run_id,
        project_id=project_id,
        basis_commit=basis,
        policy=CreativeRunPolicy(
            automation_mode=AutomationMode.AUTO,
            policy_hash=POLICY_HASH,
            permission_hash=PERMISSION_HASH,
            auto_accept_plan=True,
            auto_accept_draft=True,
        ),
        current_chapter=40,
        target_chapters=90,
    )


@pytest.mark.integration  # type: ignore[untyped-decorator]
def test_provider_transient_retry_uses_typed_runtime_command(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'u6e-retry.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    project_id = ProjectId("project.u6e.retry.integration")
    run_id = RunId("u6e-retry-integration")
    basis = CommitService(factory).initialize_project(make_manifest(project_id))
    events = RunEventLogRepository(factory)
    commands = RuntimeCommandService(factory, events, lambda _project_id: PERMISSION_HASH)
    commands.create_run_and_initial_task(_request(project_id, run_id, basis))
    task = TaskRecord(
        task_id=TaskId(f"{run_id.root}.draft.41.accept.commit"),
        run_id=run_id,
        project_id=project_id,
        kind=TaskKind.DRAFT_COMMIT,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=basis,
        policy_hash=POLICY_HASH,
        permission_hash=PERMISSION_HASH,
        chapter_index=41,
        target_chapters=90,
    )
    commands.create_task(task)
    _attempt, fence = commands.claim(task.task_id, worker_id="integration")
    settled = commands.settle_attempt(
        fence,
        outcome=AttemptOutcome.SUSPENDED,
        terminal_status=TaskStatus.WAITING_RETRY,
        failure_class=FailureClass.PROVIDER_TRANSIENT,
    )

    retried = _retry_provider_transient_task(
        database_url=database_url,
        task=settled,
        recovery_index=1,
    )

    assert retried.status is TaskStatus.READY
    assert retried.task_revision == settled.task_revision + 1
    assert commands.get_task(task.task_id).status is TaskStatus.READY
    assert any(
        event.event_type.value == "runtime.control.recorded" and event.task_id == task.task_id
        for event in events.replay(run_id)
    )
    engine.dispose()
