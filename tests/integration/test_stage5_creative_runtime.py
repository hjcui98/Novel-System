from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.runtime import RuntimeTaskQueryRepository
from novel_agent.adapters.runtime.isolated import (
    StrictDeterministicCandidateMaterializer,
    StrictFakePlanningLeaf,
)
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.creative_runtime import (
    AcceptanceCommand,
    AcceptanceDecision,
    ActorKind,
    AutomationMode,
    CandidateKind,
    CreativeRunPolicy,
    CreativeRunRequest,
    CreativeRunTerminal,
)
from novel_agent.domain.generation import WritingLoopRequest
from novel_agent.domain.ids import (
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import DerivedBuildStatus, DerivedSnapshotLite
from novel_agent.domain.runtime import TaskKind, TaskPurpose, TaskRecord, TaskStatus
from novel_agent.domain.writing_loop import WritingLoopResult, WritingLoopTerminalStatus
from novel_agent.ports.creative_runtime import WritingLeafPort
from novel_agent.runtime.creative_dispatcher import CreativeDispatcher
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.creative_runtime import CreativeRuntimeService
from novel_agent.services.event_log import RunEventLogRepository
from novel_agent.services.projection import (
    DerivedProjectionService,
    DerivedSnapshotRepository,
    ProjectionOutboxRepository,
)
from novel_agent.services.runtime_acceptance import RuntimeAcceptanceService
from novel_agent.services.runtime_commands import RuntimeCommandService
from tests.factories import make_manifest

HASH = "sha256:" + "1" * 64
NOW = datetime(2026, 8, 10, tzinfo=UTC)


class _WritingRequest:
    def __init__(self, task: TaskRecord) -> None:
        self.run_id = task.run_id
        self.task_id = task.task_id
        self.base_commit = task.basis_commit
        self.snapshot_id = task.basis_snapshot


class _WritingResult:
    def __init__(self, ref: ArtifactRef) -> None:
        self.status = WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY
        self.final_candidate_id = ref.artifact_id
        self.final_text_artifact = ref
        self.artifacts = (ref,)
        self.observation = type("Observation", (), {"changes": ()})()


class _Writer:
    is_fixture = False

    def __init__(self, artifacts: ArtifactRepository) -> None:
        self._artifacts = artifacts

    async def run(self, request: WritingLoopRequest) -> WritingLoopResult:
        ref = self._artifacts.put(
            request.task_id.root.encode(), "text/plain", SchemaVersion("1.0.0")
        )
        return cast(WritingLoopResult, _WritingResult(ref))


class _ProjectionBuilder:
    def build(self, project_id: ProjectId, source_commit: CommitId) -> DerivedSnapshotLite:
        assert project_id == ProjectId("project.test")
        suffix = source_commit.root.removeprefix("sha256:")
        return DerivedSnapshotLite(
            snapshot_id=StableId(f"snapshot.{suffix}"),
            source_commit=source_commit,
            anchor_build_id=StableId(f"anchor.{suffix[:24]}"),
            anchor_index_version="anchor-v1",
            grounded_index_version="grounded-v1",
            embedding_profile="offline-v1",
            fusion_profile="rrf-v1",
            build_status=DerivedBuildStatus.EXACT,
            published_at=NOW,
        )


@pytest.fixture
def creative_kernel(
    tmp_path: Path,
) -> Iterator[
    tuple[
        CreativeRuntimeService,
        RuntimeCommandService,
        CreativeRunPolicy,
        CommitId,
    ]
]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = build_session_factory(engine)
    commits = CommitService(factory)
    base = commits.initialize_project(make_manifest())
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    commands = RuntimeCommandService(
        factory, RunEventLogRepository(factory), lambda _project_id: HASH
    )
    policy = CreativeRunPolicy(
        automation_mode=AutomationMode.MANUAL,
        policy_hash=HASH,
        permission_hash=HASH,
    )
    runtime = CreativeRuntimeService(
        commands,
        RuntimeAcceptanceService(commands, commits, artifacts),
        commits,
        artifacts,
        StrictFakePlanningLeaf(artifacts),
        cast(WritingLeafPort, _Writer(artifacts)),
        lambda task: cast(WritingLoopRequest, _WritingRequest(task)),
        StrictDeterministicCandidateMaterializer(commits, candidate_kind=CandidateKind.PLAN),
        StrictDeterministicCandidateMaterializer(commits, candidate_kind=CandidateKind.DRAFT),
        DerivedProjectionService(ProjectionOutboxRepository(factory), _ProjectionBuilder()),
        DerivedSnapshotRepository(factory),
        lambda policy_hash: policy
        if policy_hash == policy.policy_hash
        else (_ for _ in ()).throw(KeyError(policy_hash)),
    )
    yield runtime, commands, policy, base
    engine.dispose()


def _accept(
    runtime: CreativeRuntimeService,
    commands: RuntimeCommandService,
    policy: CreativeRunPolicy,
    task_id: TaskId,
    *,
    kind: CandidateKind,
    number: int,
) -> TaskId:
    task = commands.get_task(task_id)
    assert len(task.input_artifact_refs) == 1
    candidate = runtime._candidate_for_task(task)
    assert candidate.kind is kind
    result = runtime.submit_acceptance(
        AcceptanceCommand(
            command_id=StableId(f"accept.command.{number}"),
            project_id=task.project_id,
            run_id=task.run_id,
            task_id=task.task_id,
            candidate=candidate,
            acceptance_policy_hash=policy.policy_hash,
            actor_kind=ActorKind.AUTHOR,
            actor_id="author",
            decision=AcceptanceDecision.ACCEPT,
            reason="approved",
            expected_project_commit=task.basis_commit,
            idempotency_identity=StableId(f"accept.identity.{number}"),
            issued_at=NOW,
        ),
        policy=policy,
    )
    assert result.current_task_id is not None
    return result.current_task_id


def test_three_chapter_fixed_topology_uses_accept_commit_and_exact_freshness(
    creative_kernel: tuple[
        CreativeRuntimeService,
        RuntimeCommandService,
        CreativeRunPolicy,
        CommitId,
    ],
) -> None:
    runtime, commands, policy, base = creative_kernel
    start = runtime.start(
        CreativeRunRequest(
            run_id=RunId("run.e2e"),
            project_id=ProjectId("project.test"),
            basis_commit=base,
            policy=policy,
            target_chapters=3,
        )
    )
    assert start.current_task_id is not None
    waiting_plan = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))
    assert waiting_plan.terminal is CreativeRunTerminal.WAITING_PLAN_ACCEPTANCE
    assert waiting_plan.current_task_id is not None
    assert (
        asyncio.run(runtime.advance(waiting_plan.current_task_id, worker_id="idle")).terminal
        is CreativeRunTerminal.WAITING_PLAN_ACCEPTANCE
    )
    commit_task = _accept(
        runtime,
        commands,
        policy,
        waiting_plan.current_task_id,
        kind=CandidateKind.PLAN,
        number=0,
    )
    projection = asyncio.run(runtime.advance(commit_task, worker_id="commit"))
    assert projection.current_task_id is not None
    draft = asyncio.run(runtime.advance(projection.current_task_id, worker_id="projection"))
    assert draft.current_task_id is not None

    for chapter in range(1, 4):
        waiting_draft = asyncio.run(
            runtime.advance(draft.current_task_id, worker_id=f"writer.{chapter}")
        )
        assert waiting_draft.terminal is CreativeRunTerminal.WAITING_DRAFT_ACCEPTANCE
        assert waiting_draft.current_task_id is not None
        commit_task = _accept(
            runtime,
            commands,
            policy,
            waiting_draft.current_task_id,
            kind=CandidateKind.DRAFT,
            number=chapter,
        )
        projection = asyncio.run(runtime.advance(commit_task, worker_id=f"commit.{chapter}"))
        assert projection.current_task_id is not None
        draft = asyncio.run(
            runtime.advance(projection.current_task_id, worker_id=f"projection.{chapter}")
        )
        if chapter < 3:
            assert draft.terminal is CreativeRunTerminal.PROGRESSED
            assert draft.current_task_id is not None
            assert commands.get_task(draft.current_task_id).kind is TaskKind.DRAFT_CANDIDATE
        else:
            assert draft.terminal is CreativeRunTerminal.COMPLETED


def test_two_lane_lookahead_is_revalidated_before_plan_acceptance(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = build_session_factory(engine)
    commits = CommitService(factory)
    base = commits.initialize_project(make_manifest())
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "lookahead-objects"))
    policy = CreativeRunPolicy(
        automation_mode=AutomationMode.AUTO,
        policy_hash=HASH,
        permission_hash=HASH,
        auto_accept_plan=True,
        auto_accept_draft=True,
        runtime_parallelism=2,
        enable_planner_lookahead=True,
        lookahead_horizon=2,
        max_tasks_per_advance=2,
    )
    commands = RuntimeCommandService(
        factory, RunEventLogRepository(factory), lambda _project_id: HASH
    )
    tasks = RuntimeTaskQueryRepository(factory)
    runtime = CreativeRuntimeService(
        commands,
        RuntimeAcceptanceService(commands, commits, artifacts),
        commits,
        artifacts,
        StrictFakePlanningLeaf(artifacts),
        cast(WritingLeafPort, _Writer(artifacts)),
        lambda task: cast(WritingLoopRequest, _WritingRequest(task)),
        StrictDeterministicCandidateMaterializer(commits, candidate_kind=CandidateKind.PLAN),
        StrictDeterministicCandidateMaterializer(commits, candidate_kind=CandidateKind.DRAFT),
        DerivedProjectionService(ProjectionOutboxRepository(factory), _ProjectionBuilder()),
        DerivedSnapshotRepository(factory),
        lambda policy_hash: policy
        if policy_hash == policy.policy_hash
        else (_ for _ in ()).throw(KeyError(policy_hash)),
        tasks,
    )
    start = runtime.start(
        CreativeRunRequest(
            run_id=RunId("run.lookahead"),
            project_id=ProjectId("project.test"),
            basis_commit=base,
            policy=policy,
            target_chapters=2,
        )
    )
    assert start.current_task_id is not None
    plan_commit = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))
    assert plan_commit.current_task_id is not None
    plan_projection = asyncio.run(
        runtime.advance(plan_commit.current_task_id, worker_id="plan-commit")
    )
    assert plan_projection.current_task_id is not None
    draft_ready = asyncio.run(
        runtime.advance(plan_projection.current_task_id, worker_id="plan-freshness")
    )
    assert draft_ready.reason_code == "freshness_ready_with_lookahead"

    dispatcher = CreativeDispatcher(
        tasks,
        runtime,
        worker_id="two-lane",
        run_id=RunId("run.lookahead"),
        parallelism=2,
    )
    overlapped = asyncio.run(dispatcher.run_bounded(max_tasks=2))
    assert len(overlapped) == 2
    run_tasks = tasks.list_run(RunId("run.lookahead"))
    draft_commit = next(
        task
        for task in run_tasks
        if task.kind is TaskKind.DRAFT_COMMIT and task.status is TaskStatus.READY
    )
    lookahead_waiting = next(
        task
        for task in run_tasks
        if task.kind is TaskKind.PLAN_ACCEPTANCE and task.purpose is TaskPurpose.LOOKAHEAD
    )
    assert lookahead_waiting.status is TaskStatus.WAITING_INPUT

    draft_projection = asyncio.run(runtime.advance(draft_commit.task_id, worker_id="draft-commit"))
    assert draft_projection.current_task_id is not None
    promoted = asyncio.run(
        runtime.advance(draft_projection.current_task_id, worker_id="draft-freshness")
    )
    assert promoted.current_task_id is not None
    assert commands.get_task(promoted.current_task_id).kind is TaskKind.PLAN_COMMIT
    superseded = commands.get_task(lookahead_waiting.task_id)
    assert superseded.status is TaskStatus.CANCELLED
    assert superseded.superseded
    promoted_acceptance = next(
        task
        for task in tasks.list_run(RunId("run.lookahead"))
        if task.kind is TaskKind.PLAN_ACCEPTANCE
        and task.purpose is TaskPurpose.NORMAL
        and task.task_id.root.endswith(".promoted")
    )
    assert promoted_acceptance.status is TaskStatus.SUCCEEDED
    engine.dispose()
