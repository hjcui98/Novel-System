"""SQLite integration proof for U6-D project/run query isolation."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.run_u6d_two_project_smoke import (
    _cross_project_integrity_counts,
    _database_counts,
    _project_snapshot,
)

from novel_agent.adapters.postgres.database import Base, build_engine, build_session_factory
from novel_agent.domain.artifacts import (
    PlanRootRef,
    ProjectProfileRootRef,
    ReferenceRootRef,
    RootManifest,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.creative_runtime import (
    AutomationMode,
    CreativeRunPolicy,
    CreativeRunRequest,
)
from novel_agent.domain.ids import ArtifactId, ProjectId, RunId, SchemaVersion
from novel_agent.services.commits import CommitService
from novel_agent.services.event_log import RunEventLogRepository
from novel_agent.services.runtime_commands import RuntimeCommandService

pytestmark = pytest.mark.integration


def _root_ref(ref_type: type[TextRootRef], digit: str) -> TextRootRef:
    return ref_type(
        artifact_id=ArtifactId("sha256:" + digit * 64),
        media_type="application/json",
        byte_length=1,
        schema_version=SchemaVersion("1.0.0"),
    )


def _manifest(project_id: ProjectId, offset: int) -> RootManifest:
    digits = [format(offset + index, "x") for index in range(5)]
    return RootManifest(
        project_id=project_id,
        schema_version=SchemaVersion("1.0.0"),
        text_root=_root_ref(TextRootRef, digits[0]),
        plan_root=_root_ref(PlanRootRef, digits[1]),
        world_root=_root_ref(WorldRootRef, digits[2]),
        reference_root=_root_ref(ReferenceRootRef, digits[3]),
        project_profile_root=_root_ref(ProjectProfileRootRef, digits[4]),
    )


def test_u6d_project_queries_do_not_cross_project(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'u6d.db'}"
    engine = build_engine(database_url)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    commits = CommitService(factory)
    policy_hash = "sha256:" + "1" * 64
    commands = RuntimeCommandService(
        factory,
        RunEventLogRepository(factory),
        lambda _project_id: policy_hash,
    )
    requests: list[CreativeRunRequest] = []
    try:
        for suffix, offset in (("one", 1), ("two", 6)):
            project_id = ProjectId(f"project.u6d.integration.{suffix}")
            run_id = RunId(f"run.u6d.integration.{suffix}")
            basis = commits.initialize_project(_manifest(project_id, offset))
            request = CreativeRunRequest(
                run_id=run_id,
                project_id=project_id,
                basis_commit=basis,
                policy=CreativeRunPolicy(
                    automation_mode=AutomationMode.AUTO,
                    policy_hash=policy_hash,
                    permission_hash=policy_hash,
                    auto_accept_plan=True,
                    auto_accept_draft=True,
                ),
            )
            commands.create_run_and_initial_task(request)
            requests.append(request)

        first, second = requests
        first_tasks = _project_snapshot(
            database_url, project_id=first.project_id, run_id=first.run_id
        )
        second_tasks = _project_snapshot(
            database_url, project_id=second.project_id, run_id=second.run_id
        )
        assert first_tasks and second_tasks
        assert {task.project_id for task in first_tasks} == {first.project_id}
        assert {task.project_id for task in second_tasks} == {second.project_id}
        assert _database_counts(database_url, project_id=first.project_id, run_id=first.run_id)[
            "tasks"
        ] == len(first_tasks)
        assert _cross_project_integrity_counts(
            database_url,
            ((first.project_id, first.run_id), (second.project_id, second.run_id)),
        ) == (0, 0)
    finally:
        engine.dispose()
