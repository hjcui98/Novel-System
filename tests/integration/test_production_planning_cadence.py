from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import Mock

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
from novel_agent.adapters.runtime.materializers import (
    RECONCILIATION_MEDIA_TYPE,
    WRITING_LOOP_RESULT_MEDIA_TYPE,
    DraftCandidateMaterializer,
)
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.creative_runtime import (
    AcceptanceCommand,
    AcceptanceDecision,
    AcceptedCandidateBinding,
    ActorKind,
    AutomationMode,
    CandidateBinding,
    CandidateKind,
    CreativeRunPolicy,
    CreativeRunRequest,
    CreativeRunTerminal,
)
from novel_agent.domain.generation import (
    WritingLengthPolicy,
    WritingLoopRequest,
    WritingTaskContract,
)
from novel_agent.domain.ids import (
    ArtifactId,
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
from novel_agent.ports.creative_runtime import DraftLengthContractError, WritingLeafPort
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
VERSION = SchemaVersion("1.0.0")


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


class _FixedTextWriter:
    is_fixture = False

    def __init__(self, artifacts: ArtifactRepository, text: str) -> None:
        self._artifacts = artifacts
        self._text = text

    async def run(self, request: WritingLoopRequest) -> WritingLoopResult:
        ref = self._artifacts.put(self._text.encode("utf-8"), "text/plain", VERSION)
        return cast(WritingLoopResult, _WritingResult(ref))


class _LengthRejectingDraftMaterializer:
    is_fixture = True

    def __init__(
        self,
        inner: StrictDeterministicCandidateMaterializer,
        *,
        minimum_characters: int,
        maximum_characters: int,
        artifacts: ArtifactRepository,
    ) -> None:
        self._inner = inner
        self._minimum = minimum_characters
        self._maximum = maximum_characters
        self._artifacts = artifacts

    def materialize(self, accepted: object) -> tuple[object, object]:
        candidate = cast(AcceptedCandidateBinding, accepted).candidate
        text = self._artifacts.read_verified(candidate.artifact_ref).decode("utf-8")
        writing_task = WritingTaskContract(
            contract_id=StableId("writing-contract.length-gate"),
            target_chapter=1,
            target_scenes=(StableId("scene.length-gate"),),
            pov="Lin",
            narrative_person="third person limited",
            chapter_goal="Keep the chapter inside the trusted length contract.",
            length_policy=WritingLengthPolicy(
                minimum_characters=self._minimum,
                target_characters=max(self._minimum, min(self._maximum, self._minimum + 1)),
                maximum_characters=self._maximum,
            ),
        )
        DraftCandidateMaterializer._enforce_length_contract(text, writing_task)
        return self._inner.materialize(cast(AcceptedCandidateBinding, accepted))


@pytest.fixture
def cadence_kernel(
    tmp_path: Path,
) -> Iterator[
    tuple[
        CreativeRuntimeService,
        RuntimeCommandService,
        RuntimeTaskQueryRepository,
        CreativeRunPolicy,
        CommitId,
        CommitService,
        ArtifactRepository,
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
        planning_horizon=5,
        enable_planner_lookahead=False,
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
    yield runtime, commands, tasks, policy, base, commits, artifacts
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


def test_non_lookahead_run_consumes_full_horizon_then_plans_next(
    cadence_kernel: tuple[
        CreativeRuntimeService,
        RuntimeCommandService,
        RuntimeTaskQueryRepository,
        CreativeRunPolicy,
        CommitId,
        CommitService,
        ArtifactRepository,
    ],
) -> None:
    runtime, commands, tasks, policy, base, _commits, _artifacts = cadence_kernel
    assert policy.enable_planner_lookahead is False
    start = runtime.start(
        CreativeRunRequest(
            run_id=RunId("run.cadence"),
            project_id=ProjectId("project.test"),
            basis_commit=base,
            policy=policy,
            target_chapters=10,
        )
    )
    assert start.current_task_id is not None
    initial = commands.get_task(start.current_task_id)
    assert initial.kind is TaskKind.PLAN_CANDIDATE
    assert initial.horizon_start == 1
    assert initial.horizon_end == 5

    waiting_plan = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))
    assert waiting_plan.current_task_id is not None
    commit_task = _accept(
        runtime, commands, policy, waiting_plan.current_task_id, kind=CandidateKind.PLAN, number=0
    )
    projection = asyncio.run(runtime.advance(commit_task, worker_id="plan-commit"))
    assert projection.current_task_id is not None
    draft = asyncio.run(runtime.advance(projection.current_task_id, worker_id="plan-freshness"))
    assert draft.current_task_id is not None
    assert commands.get_task(draft.current_task_id).kind is TaskKind.DRAFT_CANDIDATE
    assert commands.get_task(draft.current_task_id).chapter_index == 1

    next_task = draft.current_task_id
    for chapter in range(1, 6):
        waiting_draft = asyncio.run(runtime.advance(next_task, worker_id=f"writer.{chapter}"))
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
        projection = asyncio.run(runtime.advance(commit_task, worker_id=f"draft-commit.{chapter}"))
        assert projection.current_task_id is not None
        progressed = asyncio.run(
            runtime.advance(projection.current_task_id, worker_id=f"draft-freshness.{chapter}")
        )
        assert progressed.current_task_id is not None
        next_task = progressed.current_task_id
        if chapter < 5:
            current = commands.get_task(next_task)
            assert current.kind is TaskKind.DRAFT_CANDIDATE
            assert current.chapter_index == chapter + 1
        else:
            current = commands.get_task(next_task)
            assert current.kind is TaskKind.PLAN_CANDIDATE
            assert current.horizon_start == 6
            assert current.horizon_end == 10
            assert progressed.reason_code == "planning_horizon_advanced"

    run_tasks = tasks.list_run(RunId("run.cadence"))
    draft_chapters = [
        task.chapter_index
        for task in run_tasks
        if task.kind is TaskKind.DRAFT_CANDIDATE and task.purpose is TaskPurpose.NORMAL
    ]
    plan_horizons = [
        (task.horizon_start, task.horizon_end)
        for task in run_tasks
        if task.kind is TaskKind.PLAN_CANDIDATE and task.purpose is TaskPurpose.NORMAL
    ]
    assert draft_chapters == [1, 2, 3, 4, 5]
    assert plan_horizons == [(1, 5), (6, 10)]
    assert policy.enable_planner_lookahead is False


def test_blocked_plan_replacement_supersedes_and_does_not_reuse_task_id(
    cadence_kernel: tuple[
        CreativeRuntimeService,
        RuntimeCommandService,
        RuntimeTaskQueryRepository,
        CreativeRunPolicy,
        CommitId,
        CommitService,
        ArtifactRepository,
    ],
) -> None:
    runtime, commands, tasks, policy, base, _commits, _artifacts = cadence_kernel
    start = runtime.start(
        CreativeRunRequest(
            run_id=RunId("run.replace"),
            project_id=ProjectId("project.test"),
            basis_commit=base,
            policy=policy,
            target_chapters=10,
        )
    )
    assert start.current_task_id is not None
    waiting_plan = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))
    assert waiting_plan.current_task_id is not None
    commit_task = _accept(
        runtime, commands, policy, waiting_plan.current_task_id, kind=CandidateKind.PLAN, number=0
    )
    projection = asyncio.run(runtime.advance(commit_task, worker_id="plan-commit"))
    assert projection.current_task_id is not None
    draft = asyncio.run(runtime.advance(projection.current_task_id, worker_id="plan-freshness"))
    assert draft.current_task_id is not None
    waiting_draft = asyncio.run(runtime.advance(draft.current_task_id, worker_id="writer.1"))
    assert waiting_draft.current_task_id is not None
    commit_task = _accept(
        runtime,
        commands,
        policy,
        waiting_draft.current_task_id,
        kind=CandidateKind.DRAFT,
        number=1,
    )
    draft_projection = asyncio.run(runtime.advance(commit_task, worker_id="draft-commit.1"))
    assert draft_projection.current_task_id is not None
    next_draft = asyncio.run(
        runtime.advance(draft_projection.current_task_id, worker_id="draft-freshness.1")
    )
    assert next_draft.current_task_id is not None
    projection_task = commands.get_task(draft_projection.current_task_id)
    assert projection_task.status is TaskStatus.SUCCEEDED
    assert projection_task.projection_after == "draft"

    owner_inputs = commands.get_task(start.current_task_id).input_artifact_refs
    blocked = TaskRecord(
        task_id=TaskId("run.replace.plan.1-5"),
        run_id=RunId("run.replace"),
        project_id=ProjectId("project.test"),
        kind=TaskKind.PLAN_CANDIDATE,
        purpose=TaskPurpose.NORMAL,
        task_revision=0,
        status=TaskStatus.BLOCKED,
        basis_commit=projection_task.basis_commit,
        basis_snapshot=projection_task.basis_snapshot,
        policy_hash=policy.policy_hash,
        permission_hash=policy.permission_hash,
        input_artifact_refs=owner_inputs,
        dependency_task_ids=(projection_task.task_id,),
        failure_budget=3,
        retry_tranche_size=3,
        chapter_index=1,
        target_chapters=10,
        horizon_start=1,
        horizon_end=5,
    )
    commands.create_task(blocked)
    recovered = runtime.recover_boundary(projection_task.task_id)
    assert recovered is not None
    assert recovered.reason_code == "blocked_plan_replaced"
    assert recovered.current_task_id is not None
    assert recovered.current_task_id != blocked.task_id
    superseded = commands.get_task(blocked.task_id)
    assert superseded.superseded is True
    assert superseded.status is TaskStatus.CANCELLED
    replacement = commands.get_task(recovered.current_task_id)
    assert replacement.kind is TaskKind.PLAN_CANDIDATE
    assert replacement.status is TaskStatus.READY
    assert replacement.task_id.root == "plan.chapter-set.1-5.g1"
    assert replacement.planning_generation == 1
    assert replacement.horizon_start == 1
    assert replacement.horizon_end == 5
    assert tasks.list_run(RunId("run.replace"))


@pytest.mark.parametrize("path", ["local_repair", "major_rewrite"])
@pytest.mark.parametrize("bound", ["below_min", "above_max"])
def test_final_draft_outside_length_policy_cannot_mutate_text_root(
    path: Literal["local_repair", "major_rewrite"],
    bound: Literal["below_min", "above_max"],
    tmp_path: Path,
) -> None:
    policy = WritingLengthPolicy(
        minimum_characters=20,
        target_characters=40,
        maximum_characters=60,
    )
    writing_task = WritingTaskContract(
        contract_id=StableId(f"writing-contract.{path}.{bound}"),
        target_chapter=1,
        target_scenes=(StableId("scene.length-gate"),),
        pov="Lin",
        narrative_person="third person limited",
        chapter_goal="Honor the trusted length contract.",
        length_policy=policy,
    )
    text = "x" * 10 if bound == "below_min" else "x" * 80
    timeline = Mock()
    with pytest.raises(DraftLengthContractError, match="trusted WritingTask"):
        DraftCandidateMaterializer._enforce_length_contract(text, writing_task)
    timeline.append.assert_not_called()

    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "length-objects"))
    digest = "a" if bound == "below_min" else "b"
    text_ref = artifacts.put(text.encode("utf-8"), "text/plain", VERSION)
    result_ref = ArtifactRef(
        artifact_id=ArtifactId("sha256:" + digest * 64),
        media_type=WRITING_LOOP_RESULT_MEDIA_TYPE,
        byte_length=1,
        schema_version=VERSION,
    )
    recon_ref = ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "c" * 64),
        media_type=RECONCILIATION_MEDIA_TYPE,
        byte_length=1,
        schema_version=VERSION,
    )
    observation_ref = ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "d" * 64),
        media_type="application/json",
        byte_length=1,
        schema_version=VERSION,
    )
    final_id = text_ref.artifact_id
    task_id = TaskId(f"task.{path}.{bound}")
    run_id = RunId("run.length-gate")
    candidate_hash = text_ref.artifact_id.root
    candidate_id = StableId("draft-candidate." + final_id.root.removeprefix("sha256:")[:48])
    accepted_task = TaskId(f"{task_id.root}.accept")
    candidate = CandidateBinding(
        candidate_id=candidate_id,
        kind=CandidateKind.DRAFT,
        artifact_ref=text_ref,
        candidate_hash=candidate_hash,
        basis_commit=CommitId("sha256:" + "1" * 64),
        basis_snapshot=StableId("snapshot.length"),
        lineage_artifact_refs=(result_ref, recon_ref, observation_ref),
        affects_future_plan=False,
    )
    accepted = AcceptedCandidateBinding(
        acceptance_id=StableId(f"acceptance.{path}.{bound}"),
        command_id=StableId(f"command.{path}.{bound}"),
        project_id=ProjectId("project.test"),
        run_id=run_id,
        task_id=accepted_task,
        candidate=candidate,
        actor_kind=ActorKind.AUTHOR,
        actor_id="author",
        accepted_at=NOW,
        expected_project_commit=CommitId("sha256:" + "1" * 64),
    )
    commits = Mock()
    commits.current_commit.return_value = accepted.expected_project_commit
    manifest = make_manifest()
    commits.load_manifest.return_value = manifest
    materializer = DraftCandidateMaterializer(artifacts, commits, schema_version=VERSION)
    materializer._timeline = timeline
    basis = SimpleNamespace(
        project_id=accepted.project_id,
        base_commit=accepted.expected_project_commit,
        plan_artifact=SimpleNamespace(artifact_id=manifest.plan_root.artifact_id),
        project_profile_artifact=SimpleNamespace(
            artifact_id=manifest.project_profile_root.artifact_id
        ),
        snapshot_id=candidate.basis_snapshot,
        writing_contract_artifact=text_ref,
    )
    loop_result = SimpleNamespace(
        status=WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY,
        run_id=run_id,
        task_id=task_id,
        final_text_artifact=text_ref,
        final_candidate_id=final_id,
        initial_draft=SimpleNamespace(basis=basis),
        observation=SimpleNamespace(changes=()),
        observation_artifact=observation_ref,
        reconciliation=SimpleNamespace(path=path),
        repaired_draft=object() if path == "local_repair" else None,
        rewritten_draft=object() if path == "major_rewrite" else None,
    )
    materializer._read = Mock(  # type: ignore[method-assign]
        side_effect=[loop_result, loop_result.reconciliation, writing_task, SimpleNamespace()]
    )
    with pytest.raises(DraftLengthContractError):
        materializer.materialize(accepted)
    timeline.append.assert_not_called()

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = build_session_factory(engine)
    real_commits = CommitService(factory)
    base = real_commits.initialize_project(make_manifest())
    before = real_commits.current_commit(ProjectId("project.test"))
    original_text_root = real_commits.load_manifest(before).text_root
    commands = RuntimeCommandService(
        factory, RunEventLogRepository(factory), lambda _project_id: HASH
    )
    run_policy = CreativeRunPolicy(
        automation_mode=AutomationMode.MANUAL,
        policy_hash=HASH,
        permission_hash=HASH,
        enable_planner_lookahead=False,
    )
    inner = StrictDeterministicCandidateMaterializer(
        real_commits, candidate_kind=CandidateKind.DRAFT
    )
    runtime = CreativeRuntimeService(
        commands,
        RuntimeAcceptanceService(commands, real_commits, artifacts),
        real_commits,
        artifacts,
        StrictFakePlanningLeaf(artifacts),
        cast(WritingLeafPort, _FixedTextWriter(artifacts, text)),
        lambda task: cast(WritingLoopRequest, _WritingRequest(task)),
        StrictDeterministicCandidateMaterializer(real_commits, candidate_kind=CandidateKind.PLAN),
        cast(
            object,
            _LengthRejectingDraftMaterializer(
                inner,
                minimum_characters=policy.minimum_characters,
                maximum_characters=policy.maximum_characters,
                artifacts=artifacts,
            ),
        ),
        DerivedProjectionService(ProjectionOutboxRepository(factory), _ProjectionBuilder()),
        DerivedSnapshotRepository(factory),
        lambda policy_hash: run_policy
        if policy_hash == run_policy.policy_hash
        else (_ for _ in ()).throw(KeyError(policy_hash)),
        RuntimeTaskQueryRepository(factory),
    )
    start = runtime.start(
        CreativeRunRequest(
            run_id=RunId(f"run.length.{path}.{bound}"),
            project_id=ProjectId("project.test"),
            basis_commit=base,
            policy=run_policy,
            target_chapters=1,
        )
    )
    assert start.current_task_id is not None
    waiting_plan = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))
    assert waiting_plan.current_task_id is not None
    plan_commit = _accept(
        runtime,
        commands,
        run_policy,
        waiting_plan.current_task_id,
        kind=CandidateKind.PLAN,
        number=0,
    )
    plan_projection = asyncio.run(runtime.advance(plan_commit, worker_id="plan-commit"))
    assert plan_projection.current_task_id is not None
    draft = asyncio.run(
        runtime.advance(plan_projection.current_task_id, worker_id="plan-freshness")
    )
    assert draft.current_task_id is not None
    waiting_draft = asyncio.run(runtime.advance(draft.current_task_id, worker_id="writer"))
    assert waiting_draft.current_task_id is not None
    draft_commit = _accept(
        runtime,
        commands,
        run_policy,
        waiting_draft.current_task_id,
        kind=CandidateKind.DRAFT,
        number=1,
    )
    blocked = asyncio.run(runtime.advance(draft_commit, worker_id="draft-commit"))
    assert blocked.terminal is CreativeRunTerminal.REVIEW_REQUIRED
    assert blocked.reason_code == "draft_length_contract_rejected"
    assert commands.get_task(draft_commit).status is TaskStatus.BLOCKED
    current = real_commits.current_commit(ProjectId("project.test"))
    assert current != before
    assert real_commits.load_manifest(current).text_root == original_text_root
    engine.dispose()
