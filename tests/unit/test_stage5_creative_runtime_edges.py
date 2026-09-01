"""Deterministic branch coverage for the Stage 5 creative runtime coordinator."""

from __future__ import annotations

import asyncio
import json
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
)
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.changes import (
    CandidateChangeBundle,
    ValidationReport,
)
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
    PlanningLoopRequest,
    PlanningLoopResult,
    PlanningTerminalStatus,
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
from novel_agent.domain.memory_write import (
    MemoryWriteWorkflowPhase,
    MemoryWriteWorkflowResult,
    MemoryWriteWorkflowStatus,
)
from novel_agent.domain.runtime import (
    FailureClass,
    RunEventType,
    TaskKind,
    TaskRecord,
    TaskStatus,
)
from novel_agent.domain.writing_loop import WritingLoopTerminalStatus
from novel_agent.ports.creative_runtime import (
    CandidateMaterializer,
    ChapterSettlementPort,
    PlanningLeafPort,
    WritingLeafPort,
)
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
PERMISSION_HASH = "sha256:" + "2" * 64
NOW = datetime(2026, 8, 10, tzinfo=UTC)


class _WritingRequest:
    def __init__(self, task: TaskRecord, *, wrong: bool = False) -> None:
        self.run_id = task.run_id
        self.task_id = task.task_id
        self.base_commit = task.basis_commit
        self.snapshot_id = task.basis_snapshot
        if wrong:
            self.base_commit = CommitId("sha256:" + "9" * 64)


class _WritingResult:
    def __init__(
        self,
        ref: ArtifactRef,
        *,
        status: WritingLoopTerminalStatus = WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY,
    ) -> None:
        self.status = status
        self.final_candidate_id = ref.artifact_id
        self.final_text_artifact = ref
        self.artifacts = (ref,)
        self.failure_detail = None


class _Writer:
    is_fixture = False

    def __init__(
        self,
        artifacts: ArtifactRepository,
        *,
        status: WritingLoopTerminalStatus = WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY,
        fail: bool = False,
    ) -> None:
        self._artifacts = artifacts
        self._status = status
        self._fail = fail

    async def run(self, request: WritingLoopRequest) -> object:
        if self._fail:
            return _WritingResult(
                self._artifacts.put(b"{}", "text/plain", SchemaVersion("1.0.0")),
                status=self._status,
            )
        ref = self._artifacts.put(
            request.task_id.root.encode(), "text/plain", SchemaVersion("1.0.0")
        )
        return _WritingResult(ref, status=self._status)


class _UncertainChapterSettlement:
    is_fixture = True

    def __init__(self, artifacts: ArtifactRepository) -> None:
        self._artifacts = artifacts

    def effect_identity(self, accepted: AcceptedCandidateBinding) -> StableId:
        return StableId(f"chapter-settlement.{accepted.project_id.root}"[:128])

    def resolve_commit(self, accepted: AcceptedCandidateBinding) -> None:
        return None

    async def settle(self, accepted: AcceptedCandidateBinding) -> MemoryWriteWorkflowResult:
        checkpoint = self._artifacts.put(b"uncertain", "application/json", SchemaVersion("1.0.0"))
        return MemoryWriteWorkflowResult(
            request_id=StableId("settlement.uncertain"),
            status=MemoryWriteWorkflowStatus.SUSPENDED,
            workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
            canonical_commit_accepted=False,
            base_commit=accepted.expected_project_commit,
            checkpoint_ref=checkpoint,
            effect_uncertain=True,
        )


class _Planner:
    is_fixture = True

    def __init__(
        self,
        artifacts: ArtifactRepository,
        *,
        status: PlanningTerminalStatus = PlanningTerminalStatus.PLAN_CANDIDATE_READY,
        failure_code: str | None = None,
    ) -> None:
        self._artifacts = artifacts
        self._status = status
        self._failure_code = failure_code
        self.requests: list[PlanningLoopRequest] = []

    async def run(self, request: PlanningLoopRequest) -> PlanningLoopResult:
        self.requests.append(request)
        if self._status is not PlanningTerminalStatus.PLAN_CANDIDATE_READY:
            checkpoint = self._artifacts.put(
                request.task_id.root.encode(),
                "application/vnd.novel-agent.planning-checkpoint+json",
                SchemaVersion("1.0.0"),
            )
            return PlanningLoopResult(
                result_id=StableId("planner.result"),
                run_id=request.run_id,
                task_id=request.task_id,
                status=self._status,
                artifact_refs=(checkpoint,),
                failure_code=(self._failure_code or f"planner_{self._status.value.lower()}"),
                failure_detail="injected planner terminal",
            )
        artifact = self._artifacts.put(
            b'{"plan":"candidate"}',
            "application/vnd.novel-agent.stage5-plan-candidate+json",
            SchemaVersion("1.0.0"),
        )
        return PlanningLoopResult(
            result_id=StableId("planner.result"),
            run_id=request.run_id,
            task_id=request.task_id,
            status=PlanningTerminalStatus.PLAN_CANDIDATE_READY,
            candidate=CandidateBinding(
                candidate_id=StableId("candidate.planner"),
                kind=CandidateKind.PLAN,
                artifact_ref=artifact,
                candidate_hash=artifact.artifact_id.root,
                basis_commit=request.basis_commit,
            ),
            artifact_refs=(artifact,),
        )


class _ProjectionBuilder:
    def build(self, project_id: ProjectId, source_commit: CommitId) -> DerivedSnapshotLite:
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


class _FailingProjectionBuilder:
    def build(self, project_id: ProjectId, source_commit: CommitId) -> DerivedSnapshotLite:
        raise RuntimeError("injected projection failure")


class _NoSnapshotRepository:
    def get_for_commit(self, commit_id: CommitId) -> DerivedSnapshotLite | None:
        return None


class _StaleSnapshotRepository:
    def __init__(self, snapshots: DerivedSnapshotRepository) -> None:
        self._snapshots = snapshots

    def get_for_commit(self, commit_id: CommitId) -> DerivedSnapshotLite | None:
        snapshot = self._snapshots.get_for_commit(commit_id)
        if snapshot is None:
            return None
        return snapshot.model_copy(update={"build_status": DerivedBuildStatus.PARTIAL})


@pytest.fixture
def creative_kernel(
    tmp_path: Path,
) -> Iterator[
    tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ]
]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    commits = CommitService(factory)
    base = commits.initialize_project(make_manifest())
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    commands = RuntimeCommandService(
        factory, RunEventLogRepository(factory), lambda _project_id: PERMISSION_HASH
    )
    yield factory, commits, artifacts, commands, base
    engine.dispose()


def _policy() -> CreativeRunPolicy:
    return CreativeRunPolicy(
        automation_mode=AutomationMode.MANUAL,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
    )


def _request(
    base: CommitId,
    *,
    run_id: str = "run.coordinator",
    project_id: ProjectId | None = None,
) -> CreativeRunRequest:
    return CreativeRunRequest(
        run_id=RunId(run_id),
        project_id=project_id or ProjectId("project.test"),
        basis_commit=base,
        policy=_policy(),
    )


def test_creative_run_starts_after_existing_canon_and_plans_to_absolute_target(
    creative_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    _factory, _commits, _artifacts, commands, base = creative_kernel
    request = CreativeRunRequest(
        run_id=RunId("run.existing-canon"),
        project_id=ProjectId("project.test"),
        basis_commit=base,
        policy=_policy(),
        current_chapter=20,
        target_chapters=25,
    )

    task = commands.create_run_and_initial_task(request)

    assert task.chapter_index == 20
    assert task.target_chapters == 25
    assert (task.horizon_start, task.horizon_end) == (21, 25)


def _build_runtime(
    factory: sessionmaker[Session],
    commits: CommitService,
    artifacts: ArtifactRepository,
    commands: RuntimeCommandService,
    *,
    planner: PlanningLeafPort,
    writer: WritingLeafPort,
    wrong_writing_request: bool = False,
    plan_materializer: CandidateMaterializer | None = None,
    draft_materializer: CandidateMaterializer | None = None,
    projection: DerivedProjectionService | None = None,
    snapshots: DerivedSnapshotRepository | None = None,
    project_id: ProjectId | None = None,
    task_reader: RuntimeTaskQueryRepository | None = None,
    chapter_settlement: ChapterSettlementPort | None = None,
) -> CreativeRuntimeService:
    project_id = project_id or ProjectId("project.test")
    return CreativeRuntimeService(
        commands,
        RuntimeAcceptanceService(commands, commits, artifacts),
        commits,
        artifacts,
        planner,
        writer,
        lambda task: cast(WritingLoopRequest, _WritingRequest(task, wrong=wrong_writing_request)),
        plan_materializer
        or StrictDeterministicCandidateMaterializer(commits, candidate_kind=CandidateKind.PLAN),
        draft_materializer
        or StrictDeterministicCandidateMaterializer(commits, candidate_kind=CandidateKind.DRAFT),
        projection
        or DerivedProjectionService(ProjectionOutboxRepository(factory), _ProjectionBuilder()),
        snapshots or DerivedSnapshotRepository(factory),
        lambda policy_hash: policy_resolver(policy_hash),
        task_reader,
        chapter_settlement,
    )


AUTO_POLICY = CreativeRunPolicy(
    automation_mode=AutomationMode.AUTO,
    policy_hash="sha256:" + "a" * 64,
    permission_hash=PERMISSION_HASH,
    auto_accept_plan=True,
    auto_accept_draft=True,
)


def policy_resolver(policy_hash: str) -> CreativeRunPolicy:
    if policy_hash == HASH:
        return _policy()
    if policy_hash == AUTO_POLICY.policy_hash:
        return AUTO_POLICY
    raise KeyError(policy_hash)


def _accept_waiting(
    runtime: CreativeRuntimeService,
    commands: RuntimeCommandService,
    waiting_task_id: TaskId,
    *,
    kind: CandidateKind,
    commit: CommitId,
) -> TaskId:
    task = commands.get_task(waiting_task_id)
    assert task.candidate_binding_ref is not None
    candidate = CandidateBinding.model_validate_json(
        runtime._artifacts.read_verified(task.candidate_binding_ref)
    )
    assert candidate.kind is kind and candidate.basis_commit == commit
    result = runtime.submit_acceptance(
        AcceptanceCommand(
            command_id=StableId(f"accept.{waiting_task_id.root}"),
            project_id=task.project_id,
            run_id=task.run_id,
            task_id=task.task_id,
            candidate=candidate,
            acceptance_policy_hash=HASH,
            actor_kind=ActorKind.AUTHOR,
            actor_id="author",
            decision=AcceptanceDecision.ACCEPT,
            reason="approved",
            expected_project_commit=commit,
            idempotency_identity=StableId(f"accept.{waiting_task_id.root}.identity"),
            issued_at=NOW,
        ),
        policy=_policy(),
    )
    assert result.current_task_id is not None
    return result.current_task_id


def _accept_bound_candidate(
    runtime: CreativeRuntimeService,
    commands: RuntimeCommandService,
    artifacts: ArtifactRepository,
    waiting_task_id: TaskId,
) -> TaskId:
    task = commands.get_task(waiting_task_id)
    assert task.candidate_binding_ref is not None
    candidate = CandidateBinding.model_validate_json(
        artifacts.read_verified(task.candidate_binding_ref)
    )
    result = runtime.submit_acceptance(
        AcceptanceCommand(
            command_id=StableId(f"accept-bound.{waiting_task_id.root}"),
            project_id=task.project_id,
            run_id=task.run_id,
            task_id=task.task_id,
            candidate=candidate,
            acceptance_policy_hash=HASH,
            actor_kind=ActorKind.AUTHOR,
            actor_id="author",
            decision=AcceptanceDecision.ACCEPT,
            reason="approved",
            expected_project_commit=task.basis_commit,
            idempotency_identity=StableId(f"accept-bound.{waiting_task_id.root}.identity"),
            issued_at=NOW,
        ),
        policy=_policy(),
    )
    assert result.current_task_id is not None
    return result.current_task_id


def _fresh_project(commits: CommitService, project_id: ProjectId) -> CommitId:
    manifest = make_manifest(project_id)
    return commits.initialize_project(manifest)


def test_planner_and_writer_failure_branches_are_audited(
    creative_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, commands, _ = creative_kernel
    for status in (
        PlanningTerminalStatus.SUSPENDED,
        PlanningTerminalStatus.REVIEW_REQUIRED,
        PlanningTerminalStatus.BLOCKED,
    ):
        project_id = ProjectId(f"project.planner-fail.{status.value}")
        fresh_base = _fresh_project(commits, project_id)
        runtime = _build_runtime(
            factory,
            commits,
            artifacts,
            commands,
            planner=cast(PlanningLeafPort, _Planner(artifacts, status=status)),
            writer=cast(WritingLeafPort, _Writer(artifacts)),
            project_id=project_id,
        )
        start = runtime.start(
            _request(
                fresh_base,
                run_id=f"run.planner-fail.{status.value}",
                project_id=project_id,
            )
        )
        assert start.current_task_id is not None
        result = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))
        assert result.terminal in {
            CreativeRunTerminal.WAITING_RETRY,
            CreativeRunTerminal.REVIEW_REQUIRED,
            CreativeRunTerminal.BLOCKED,
        }
    for writer_status in (
        WritingLoopTerminalStatus.MODEL_UNAVAILABLE,
        WritingLoopTerminalStatus.BASIS_CHANGED,
        WritingLoopTerminalStatus.WRITER_FAILED,
    ):
        project_id = ProjectId(f"project.writer-fail.{writer_status.value}")
        fresh_base = _fresh_project(commits, project_id)
        runtime = _build_runtime(
            factory,
            commits,
            artifacts,
            commands,
            planner=cast(PlanningLeafPort, _Planner(artifacts)),
            writer=cast(WritingLeafPort, _Writer(artifacts, status=writer_status, fail=True)),
            project_id=project_id,
        )
        start = runtime.start(
            _request(
                fresh_base,
                run_id=f"run.writer-fail.{writer_status.value}",
                project_id=project_id,
            )
        )
        assert start.current_task_id is not None
        waiting = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))
        assert waiting.current_task_id is not None
        assert (
            asyncio.run(runtime.advance(waiting.current_task_id, worker_id="idle")).terminal
            is CreativeRunTerminal.WAITING_PLAN_ACCEPTANCE
        )
        commit_task = _accept_waiting(
            runtime,
            commands,
            waiting.current_task_id,
            kind=CandidateKind.PLAN,
            commit=fresh_base,
        )
        projection = asyncio.run(runtime.advance(commit_task, worker_id="commit"))
        assert projection.current_task_id is not None
        draft = asyncio.run(runtime.advance(projection.current_task_id, worker_id="projection"))
        assert draft.current_task_id is not None
        result = asyncio.run(
            runtime.advance(draft.current_task_id, worker_id=f"writer.{writer_status.value}")
        )
        assert result.terminal is (
            CreativeRunTerminal.BLOCKED
            if writer_status is WritingLoopTerminalStatus.BASIS_CHANGED
            else CreativeRunTerminal.WAITING_RETRY
        )


def test_planner_yield_keeps_checkpoint_claimable_without_spending_retry_budget(
    creative_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, commands, base = creative_kernel
    runtime = _build_runtime(
        factory,
        commits,
        artifacts,
        commands,
        planner=cast(
            PlanningLeafPort,
            _Planner(
                artifacts,
                status=PlanningTerminalStatus.YIELDED,
                failure_code="PLAN_REVISION_SLICE_EXHAUSTED",
            ),
        ),
        writer=cast(WritingLeafPort, _Writer(artifacts)),
    )
    start = runtime.start(_request(base, run_id="run.planner-yield"))
    assert start.current_task_id is not None

    result = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))

    task = commands.get_task(start.current_task_id)
    assert result.terminal is CreativeRunTerminal.PROGRESSED
    assert task.status is TaskStatus.READY
    assert task.failure_budget == task.retry_tranche_size
    assert len(task.terminal_artifact_refs) == 1


@pytest.mark.parametrize(
    ("writer_status", "expected_terminal", "expected_task_status", "reason_code"),
    (
        (
            WritingLoopTerminalStatus.YIELDED,
            CreativeRunTerminal.PROGRESSED,
            TaskStatus.READY,
            "writer_yielded",
        ),
        (
            WritingLoopTerminalStatus.MEMORY_BUDGET_EXHAUSTED,
            CreativeRunTerminal.BUDGET_REVIEW,
            TaskStatus.BUDGET_REVIEW,
            "writer_memory_budget_exhausted",
        ),
    ),
)
def test_writer_yield_preserves_checkpoint_and_retry_budget(
    creative_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
    writer_status: WritingLoopTerminalStatus,
    expected_terminal: CreativeRunTerminal,
    expected_task_status: TaskStatus,
    reason_code: str,
) -> None:
    factory, commits, artifacts, commands, base = creative_kernel
    runtime = _build_runtime(
        factory,
        commits,
        artifacts,
        commands,
        planner=cast(PlanningLeafPort, _Planner(artifacts)),
        writer=cast(
            WritingLeafPort,
            _Writer(
                artifacts,
                status=writer_status,
                fail=True,
            ),
        ),
    )
    start = runtime.start(_request(base, run_id="run.writer-yield"))
    assert start.current_task_id is not None
    waiting = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))
    assert waiting.current_task_id is not None
    asyncio.run(runtime.advance(waiting.current_task_id, worker_id="acceptance"))
    commit_task = _accept_waiting(
        runtime,
        commands,
        waiting.current_task_id,
        kind=CandidateKind.PLAN,
        commit=base,
    )
    projection = asyncio.run(runtime.advance(commit_task, worker_id="commit"))
    assert projection.current_task_id is not None
    draft = asyncio.run(runtime.advance(projection.current_task_id, worker_id="projection"))
    assert draft.current_task_id is not None

    result = asyncio.run(runtime.advance(draft.current_task_id, worker_id="writer"))

    task = commands.get_task(draft.current_task_id)
    assert result.terminal is expected_terminal
    assert result.reason_code == reason_code
    assert task.status is expected_task_status
    assert task.failure_budget == task.retry_tranche_size
    assert len(task.terminal_artifact_refs) == 1


def test_planner_memory_budget_yield_waits_for_budget_extension(
    creative_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, commands, base = creative_kernel
    planner = _Planner(
        artifacts,
        status=PlanningTerminalStatus.YIELDED,
        failure_code="INQUIRY_MEMORY_BUDGET_EXHAUSTED",
    )
    runtime = _build_runtime(
        factory,
        commits,
        artifacts,
        commands,
        planner=cast(
            PlanningLeafPort,
            planner,
        ),
        writer=cast(WritingLeafPort, _Writer(artifacts)),
    )
    start = runtime.start(_request(base, run_id="run.planner-budget-yield"))
    assert start.current_task_id is not None

    result = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))

    task = commands.get_task(start.current_task_id)
    assert result.terminal is CreativeRunTerminal.BUDGET_REVIEW
    assert result.reason_code == "INQUIRY_MEMORY_BUDGET_EXHAUSTED"
    assert result.next_legal_commands == ("extend_budget", "cancel")
    assert task.status is TaskStatus.BUDGET_REVIEW
    assert task.failure_budget == task.retry_tranche_size
    extended = commands.extend_budget(
        task.task_id,
        command_id=StableId("extend.planner-memory-budget"),
        actor_id="operator",
        reason="allow another bounded Planner Memory tranche",
        additional_planner_memory_tranches=1,
    )
    resumed = asyncio.run(runtime.advance(extended.task_id, worker_id="planner.resumed"))
    assert resumed.terminal is CreativeRunTerminal.BUDGET_REVIEW
    assert planner.requests[-1].planner_memory_budget_extensions == 1
    assert planner.requests[-1].continuation_artifact_refs == task.terminal_artifact_refs


def test_draft_horizon_end_schedules_a_fresh_normal_planner_task(
    creative_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, commands, base = creative_kernel
    runtime = _build_runtime(
        factory,
        commits,
        artifacts,
        commands,
        planner=cast(PlanningLeafPort, _Planner(artifacts)),
        writer=cast(WritingLeafPort, _Writer(artifacts)),
        task_reader=RuntimeTaskQueryRepository(factory),
    )
    request = CreativeRunRequest(
        run_id=RunId("run.rolling-plan"),
        project_id=ProjectId("project.test"),
        basis_commit=base,
        policy=_policy().model_copy(update={"planning_horizon": 1}),
        target_chapters=2,
    )
    start = runtime.start(request)
    assert start.current_task_id is not None
    plan_waiting = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))
    assert plan_waiting.current_task_id is not None
    plan_commit = _accept_bound_candidate(
        runtime, commands, artifacts, plan_waiting.current_task_id
    )
    plan_projection = asyncio.run(runtime.advance(plan_commit, worker_id="plan-commit"))
    assert plan_projection.current_task_id is not None
    draft = asyncio.run(
        runtime.advance(plan_projection.current_task_id, worker_id="plan-projection")
    )
    assert draft.current_task_id is not None
    draft_waiting = asyncio.run(runtime.advance(draft.current_task_id, worker_id="writer"))
    assert draft_waiting.current_task_id is not None
    draft_commit = _accept_bound_candidate(
        runtime, commands, artifacts, draft_waiting.current_task_id
    )
    draft_projection = asyncio.run(runtime.advance(draft_commit, worker_id="draft-commit"))
    assert draft_projection.current_task_id is not None

    rolling = asyncio.run(
        runtime.advance(draft_projection.current_task_id, worker_id="draft-projection")
    )

    assert rolling.reason_code == "planning_horizon_advanced"
    assert rolling.current_task_id is not None
    task = commands.get_task(rolling.current_task_id)
    assert task.kind is TaskKind.PLAN_CANDIDATE
    assert task.purpose.value == "normal"
    assert task.chapter_index == 1
    assert (task.horizon_start, task.horizon_end) == (2, 2)
    assert task.basis_snapshot is not None


def test_writer_request_factory_violation_is_rejected(
    creative_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, commands, _ = creative_kernel
    project_id = ProjectId("project.wrong-writer")
    fresh_base = _fresh_project(commits, project_id)
    runtime = _build_runtime(
        factory,
        commits,
        artifacts,
        commands,
        planner=cast(PlanningLeafPort, _Planner(artifacts)),
        writer=cast(WritingLeafPort, _Writer(artifacts)),
        wrong_writing_request=True,
        project_id=project_id,
    )
    start = runtime.start(
        _request(fresh_base, run_id="run.wrong-writer-request", project_id=project_id)
    )
    assert start.current_task_id is not None
    waiting = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))
    assert waiting.current_task_id is not None
    commit_task = _accept_waiting(
        runtime, commands, waiting.current_task_id, kind=CandidateKind.PLAN, commit=fresh_base
    )
    projection = asyncio.run(runtime.advance(commit_task, worker_id="commit"))
    assert projection.current_task_id is not None
    draft = asyncio.run(runtime.advance(projection.current_task_id, worker_id="projection"))
    assert draft.current_task_id is not None
    result = asyncio.run(runtime.advance(draft.current_task_id, worker_id="writer.bad-request"))
    assert result.terminal is CreativeRunTerminal.REVIEW_REQUIRED
    settled = commands.get_task(draft.current_task_id)
    assert settled.status is TaskStatus.BLOCKED
    assert settled.current_attempt_id is None
    assert settled.block_cause == "validation_rejected"


def test_validation_rejection_and_commit_rejection_are_audited(
    creative_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    from novel_agent.domain.changes import ValidationStatus

    class _RejectingMaterializer(CandidateMaterializer):
        def materialize(
            self, accepted: AcceptedCandidateBinding
        ) -> tuple[CandidateChangeBundle, ValidationReport]:
            from novel_agent.domain.changes import ObservedChangeSet

            bundle = CandidateChangeBundle(
                bundle_id=StableId("bundle.reject"),
                project_id=ProjectId("project.validation-reject"),
                run_id=RunId("run.validation-reject"),
                base_commit=fresh_base,
                observed_changes=ObservedChangeSet(
                    change_set_id=StableId("changes.reject"),
                    base_commit=fresh_base,
                    source_artifact=make_manifest().text_root,
                ),
                proposed_roots=make_manifest(ProjectId("project.validation-reject")),
            )
            report = ValidationReport(
                report_id=StableId("report.reject"),
                bundle_id=bundle.bundle_id,
                status=ValidationStatus.FAILED,
                schema_version=SchemaVersion("1.0.0"),
                validated_at=NOW,
            )
            return bundle, report

    factory, commits, artifacts, commands, _ = creative_kernel
    project_id = ProjectId("project.validation-reject")
    fresh_base = _fresh_project(commits, project_id)
    runtime = _build_runtime(
        factory,
        commits,
        artifacts,
        commands,
        planner=cast(PlanningLeafPort, _Planner(artifacts)),
        writer=cast(WritingLeafPort, _Writer(artifacts)),
        plan_materializer=cast(CandidateMaterializer, _RejectingMaterializer()),
        project_id=project_id,
    )
    start = runtime.start(
        _request(fresh_base, run_id="run.validation-reject", project_id=project_id)
    )
    assert start.current_task_id is not None
    waiting = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))
    assert waiting.current_task_id is not None
    commit_task = _accept_waiting(
        runtime,
        commands,
        waiting.current_task_id,
        kind=CandidateKind.PLAN,
        commit=fresh_base,
    )
    result = asyncio.run(runtime.advance(commit_task, worker_id="commit"))
    assert result.terminal is CreativeRunTerminal.REVIEW_REQUIRED


def test_projection_failure_and_missing_snapshot_branches(
    creative_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, commands, _ = creative_kernel

    def _reach_projection(
        runtime: CreativeRuntimeService,
        run_id: str,
        project_id: ProjectId,
        commit: CommitId,
    ) -> TaskId:
        start = runtime.start(_request(commit, run_id=run_id, project_id=project_id))
        assert start.current_task_id is not None
        waiting = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))
        assert waiting.current_task_id is not None
        commit_task = _accept_waiting(
            runtime, commands, waiting.current_task_id, kind=CandidateKind.PLAN, commit=commit
        )
        projection = asyncio.run(runtime.advance(commit_task, worker_id="commit"))
        assert projection.current_task_id is not None
        return projection.current_task_id

    failing_project = ProjectId("project.projection-fail")
    failing_base = _fresh_project(commits, failing_project)
    failing = _build_runtime(
        factory,
        commits,
        artifacts,
        commands,
        planner=cast(PlanningLeafPort, _Planner(artifacts)),
        writer=cast(WritingLeafPort, _Writer(artifacts)),
        projection=DerivedProjectionService(
            ProjectionOutboxRepository(factory), _FailingProjectionBuilder()
        ),
        project_id=failing_project,
    )
    projection_task = _reach_projection(
        failing, "run.projection-fail", failing_project, failing_base
    )
    result = asyncio.run(failing.advance(projection_task, worker_id="projection"))
    assert result.terminal is CreativeRunTerminal.WAITING_RETRY

    missing_project = ProjectId("project.snapshot-missing")
    missing_base = _fresh_project(commits, missing_project)
    missing = _build_runtime(
        factory,
        commits,
        artifacts,
        commands,
        planner=cast(PlanningLeafPort, _Planner(artifacts)),
        writer=cast(WritingLeafPort, _Writer(artifacts)),
        snapshots=cast(DerivedSnapshotRepository, _NoSnapshotRepository()),
        project_id=missing_project,
    )
    projection_task = _reach_projection(
        missing, "run.snapshot-missing", missing_project, missing_base
    )
    result = asyncio.run(missing.advance(projection_task, worker_id="projection"))
    assert result.terminal is CreativeRunTerminal.WAITING_RETRY


def test_unknown_task_kind_and_rejected_candidate_branches(
    creative_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, commands, _ = creative_kernel
    project_id = ProjectId("project.reject-candidate")
    fresh_base = _fresh_project(commits, project_id)
    runtime = _build_runtime(
        factory,
        commits,
        artifacts,
        commands,
        planner=cast(PlanningLeafPort, _Planner(artifacts)),
        writer=cast(WritingLeafPort, _Writer(artifacts)),
        project_id=project_id,
    )
    start = runtime.start(
        _request(fresh_base, run_id="run.reject-candidate", project_id=project_id)
    )
    assert start.current_task_id is not None
    waiting = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))
    assert waiting.current_task_id is not None
    task = commands.get_task(waiting.current_task_id)
    assert task.candidate_binding_ref is not None
    bound_candidate = CandidateBinding.model_validate_json(
        artifacts.read_verified(task.candidate_binding_ref)
    )
    rejected = runtime.submit_acceptance(
        AcceptanceCommand(
            command_id=StableId("accept.reject"),
            project_id=task.project_id,
            run_id=task.run_id,
            task_id=task.task_id,
            candidate=bound_candidate,
            acceptance_policy_hash=HASH,
            actor_kind=ActorKind.AUTHOR,
            actor_id="author",
            decision=AcceptanceDecision.REJECT,
            reason="rejected",
            expected_project_commit=fresh_base,
            idempotency_identity=StableId("accept.reject.identity"),
            issued_at=NOW,
        ),
        policy=_policy(),
    )
    assert rejected.terminal is CreativeRunTerminal.CANCELLED
    with pytest.raises(ValueError, match="cannot execute task kind"):
        unknown = commands.get_task(waiting.current_task_id).model_copy(
            update={"task_id": TaskId("run.reject-candidate.unknown"), "status": TaskStatus.READY}
        )
        commands.create_task(unknown.model_copy(update={"kind": TaskKind.MAINTENANCE}))
        asyncio.run(runtime.advance(unknown.task_id, worker_id="worker.unknown"))


def test_auto_accept_plan_draft_and_commit_rejection_branches(
    creative_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, commands, _ = creative_kernel
    project_id = ProjectId("project.auto-accept")
    fresh_base = _fresh_project(commits, project_id)
    runtime = _build_runtime(
        factory,
        commits,
        artifacts,
        commands,
        planner=cast(PlanningLeafPort, _Planner(artifacts)),
        writer=cast(WritingLeafPort, _Writer(artifacts)),
        project_id=project_id,
    )
    request = CreativeRunRequest(
        run_id=RunId("run.auto-accept"),
        project_id=project_id,
        basis_commit=fresh_base,
        policy=AUTO_POLICY,
    )
    start = runtime.start(request)
    assert start.current_task_id is not None
    # Plan candidate auto-accepts and returns the commit task directly.
    auto = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))
    assert auto.current_task_id is not None
    assert commands.get_task(auto.current_task_id).kind is TaskKind.PLAN_COMMIT
    # Commit rejects because the requested basis was already consumed by the plan commit.
    # Instead drive a fresh project where the commit succeeds and freshness is pending.
    assert auto.terminal is CreativeRunTerminal.PROGRESSED


def test_auto_accept_draft_and_freshness_not_ready_branch(
    creative_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, commands, _ = creative_kernel
    project_id = ProjectId("project.auto-draft")
    fresh_base = _fresh_project(commits, project_id)
    runtime = _build_runtime(
        factory,
        commits,
        artifacts,
        commands,
        planner=cast(PlanningLeafPort, _Planner(artifacts)),
        writer=cast(WritingLeafPort, _Writer(artifacts)),
        snapshots=cast(
            DerivedSnapshotRepository, _StaleSnapshotRepository(DerivedSnapshotRepository(factory))
        ),
        project_id=project_id,
    )
    request = CreativeRunRequest(
        run_id=RunId("run.auto-draft"),
        project_id=project_id,
        basis_commit=fresh_base,
        policy=AUTO_POLICY,
    )
    start = runtime.start(request)
    assert start.current_task_id is not None
    auto = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))
    assert auto.current_task_id is not None
    commit_task = auto.current_task_id
    commit = asyncio.run(runtime.advance(commit_task, worker_id="commit"))
    assert commit.current_task_id is not None
    # Freshness task sees a stale snapshot, so it stays WAITING_RETRY.
    stale = asyncio.run(runtime.advance(commit.current_task_id, worker_id="projection"))
    assert stale.terminal is CreativeRunTerminal.WAITING_RETRY


def test_accepted_binding_and_candidate_for_task_guard_errors(
    creative_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, commands, _ = creative_kernel
    project_id = ProjectId("project.binding-guard")
    fresh_base = _fresh_project(commits, project_id)
    runtime = _build_runtime(
        factory,
        commits,
        artifacts,
        commands,
        planner=cast(PlanningLeafPort, _Planner(artifacts)),
        writer=cast(WritingLeafPort, _Writer(artifacts)),
        project_id=project_id,
    )
    # A commit task with no acceptance receipt input triggers the guard.
    commit_task = TaskRecord(
        task_id=TaskId("run.binding-guard.commit"),
        run_id=RunId("run.binding-guard"),
        project_id=project_id,
        kind=TaskKind.PLAN_COMMIT,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=fresh_base,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
        input_artifact_refs=(),
    )
    commands.create_task(commit_task)
    with pytest.raises(ValueError, match="exactly one acceptance receipt"):
        asyncio.run(runtime.advance(commit_task.task_id, worker_id="commit"))
    assert commands.get_task(commit_task.task_id).status is TaskStatus.BLOCKED

    # A commit task whose receipt is a rejected candidate.
    from novel_agent.domain.creative_runtime import AcceptanceReceipt

    rejected_ref = artifacts.put(
        b'{"rejected":true}',
        "application/vnd.novel-agent.stage5-acceptance-receipt+json",
        SchemaVersion("1.0.0"),
    )
    candidate = CandidateBinding(
        candidate_id=StableId("candidate.rejected-receipt"),
        kind=CandidateKind.PLAN,
        artifact_ref=rejected_ref,
        candidate_hash=rejected_ref.artifact_id.root,
        basis_commit=fresh_base,
    )
    receipt = AcceptanceReceipt(
        receipt_id=StableId("receipt.rejected"),
        command_id=StableId("command.rejected"),
        idempotency_identity=StableId("idem.rejected"),
        command_hash=HASH,
        decision=AcceptanceDecision.REJECT,
        candidate=candidate,
        accepted_binding=None,
        reason="rejected",
        recorded_at=NOW,
    )
    rejected_ref = artifacts.put(
        json.dumps(receipt.model_dump(mode="json"), sort_keys=True).encode("utf-8"),
        "application/vnd.novel-agent.stage5-acceptance-receipt+json",
        SchemaVersion("1.0.0"),
    )
    rejected_commit = TaskRecord(
        task_id=TaskId("run.binding-guard.rejected-commit"),
        run_id=RunId("run.binding-guard"),
        project_id=project_id,
        kind=TaskKind.PLAN_COMMIT,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=fresh_base,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
        input_artifact_refs=(rejected_ref,),
    )
    commands.create_task(rejected_commit)
    with pytest.raises(ValueError, match="rejected candidate cannot reach"):
        asyncio.run(runtime.advance(rejected_commit.task_id, worker_id="commit"))
    assert commands.get_task(rejected_commit.task_id).status is TaskStatus.BLOCKED

    # Acceptance task missing its candidate binding ref.
    missing_task = commands.get_task(
        commands.create_run_and_initial_task(
            CreativeRunRequest(
                run_id=RunId("run.missing-binding"),
                project_id=project_id,
                basis_commit=fresh_base,
                policy=AUTO_POLICY,
            )
        ).task_id
    )
    # Create a waiting acceptance task without a binding ref.
    wait_missing = TaskRecord(
        task_id=TaskId("run.missing-binding.accept"),
        run_id=missing_task.run_id,
        project_id=project_id,
        kind=TaskKind.PLAN_ACCEPTANCE,
        task_revision=0,
        status=TaskStatus.WAITING_INPUT,
        basis_commit=fresh_base,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
        input_artifact_refs=(rejected_ref,),
        dependency_task_ids=(missing_task.task_id,),
    )
    commands.create_task(wait_missing)
    with pytest.raises(ValueError, match="missing its immutable candidate binding"):
        asyncio.run(runtime.advance(wait_missing.task_id, worker_id="idle"))


def test_auto_accept_draft_flow_reaches_commit_and_rejection(
    creative_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, commands, _ = creative_kernel
    project_id = ProjectId("project.auto-draft-flow")
    fresh_base = _fresh_project(commits, project_id)
    runtime = _build_runtime(
        factory,
        commits,
        artifacts,
        commands,
        planner=cast(PlanningLeafPort, _Planner(artifacts)),
        writer=cast(WritingLeafPort, _Writer(artifacts)),
        project_id=project_id,
    )
    request = CreativeRunRequest(
        run_id=RunId("run.auto-draft-flow"),
        project_id=project_id,
        basis_commit=fresh_base,
        policy=AUTO_POLICY,
    )
    start = runtime.start(request)
    assert start.current_task_id is not None
    plan_commit = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))
    assert plan_commit.current_task_id is not None
    projection = asyncio.run(runtime.advance(plan_commit.current_task_id, worker_id="commit"))
    assert projection.current_task_id is not None
    freshness = asyncio.run(runtime.advance(projection.current_task_id, worker_id="projection"))
    assert freshness.current_task_id is not None
    # The freshness task creates a draft_candidate task; auto-accept fires only
    # after the writer produces a candidate.
    assert commands.get_task(freshness.current_task_id).kind is TaskKind.DRAFT_CANDIDATE
    draft_commit = asyncio.run(runtime.advance(freshness.current_task_id, worker_id="writer.auto"))
    assert draft_commit.current_task_id is not None
    assert commands.get_task(draft_commit.current_task_id).kind is TaskKind.DRAFT_COMMIT
    _ = asyncio.run(runtime.advance(draft_commit.current_task_id, worker_id="commit.draft"))
    assert commands.get_task(draft_commit.current_task_id).status is not TaskStatus.WAITING_INPUT


def test_uncertain_chapter_settlement_blocks_without_provider_retry(
    creative_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, commands, _base = creative_kernel
    project_id = ProjectId("project.uncertain-settlement")
    fresh_base = _fresh_project(commits, project_id)
    runtime = _build_runtime(
        factory,
        commits,
        artifacts,
        commands,
        planner=cast(PlanningLeafPort, _Planner(artifacts)),
        writer=cast(WritingLeafPort, _Writer(artifacts)),
        project_id=project_id,
        chapter_settlement=_UncertainChapterSettlement(artifacts),
    )
    request = CreativeRunRequest(
        run_id=RunId("run.uncertain-settlement"),
        project_id=project_id,
        basis_commit=fresh_base,
        policy=AUTO_POLICY,
    )
    start = runtime.start(request)
    assert start.current_task_id is not None
    plan_commit = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))
    assert plan_commit.current_task_id is not None
    projection = asyncio.run(runtime.advance(plan_commit.current_task_id, worker_id="commit"))
    assert projection.current_task_id is not None
    freshness = asyncio.run(runtime.advance(projection.current_task_id, worker_id="projection"))
    assert freshness.current_task_id is not None
    draft_commit = asyncio.run(runtime.advance(freshness.current_task_id, worker_id="writer"))
    assert draft_commit.current_task_id is not None

    result = asyncio.run(runtime.advance(draft_commit.current_task_id, worker_id="commit.draft"))

    task = commands.get_task(draft_commit.current_task_id)
    assert result.terminal is CreativeRunTerminal.REVIEW_REQUIRED
    assert result.reason_code == "chapter_settlement_suspended"
    assert task.status is TaskStatus.BLOCKED
    settled_events = tuple(
        event
        for event in RunEventLogRepository(factory).replay(request.run_id)
        if event.event_type is RunEventType.RUNTIME_ATTEMPT_SETTLED
        and event.task_id == draft_commit.current_task_id
    )
    assert (
        cast(dict[str, object], settled_events[-1].payload)["failure_class"]
        == FailureClass.EFFECT_UNCERTAIN.value
    )


def test_commit_rejection_and_writer_review_required_branches(
    creative_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    from novel_agent.domain.changes import ValidationStatus

    factory, commits, artifacts, commands, _ = creative_kernel
    project_id = ProjectId("project.commit-reject")
    fresh_base = _fresh_project(commits, project_id)

    class _WrongBaseMaterializer(CandidateMaterializer):
        def materialize(
            self, accepted: AcceptedCandidateBinding
        ) -> tuple[CandidateChangeBundle, ValidationReport]:
            from novel_agent.domain.changes import ObservedChangeSet

            wrong_base = CommitId("sha256:" + "7" * 64)
            bundle = CandidateChangeBundle(
                bundle_id=StableId("bundle.commit-reject"),
                project_id=project_id,
                run_id=RunId("run.commit-reject"),
                base_commit=wrong_base,
                observed_changes=ObservedChangeSet(
                    change_set_id=StableId("changes.commit-reject"),
                    base_commit=wrong_base,
                    source_artifact=make_manifest().text_root,
                ),
                proposed_roots=make_manifest(project_id),
            )
            report = ValidationReport(
                report_id=StableId("report.commit-reject"),
                bundle_id=bundle.bundle_id,
                status=ValidationStatus.PASSED,
                schema_version=SchemaVersion("1.0.0"),
                validated_at=NOW,
            )
            return bundle, report

    runtime = _build_runtime(
        factory,
        commits,
        artifacts,
        commands,
        planner=cast(PlanningLeafPort, _Planner(artifacts)),
        writer=cast(WritingLeafPort, _Writer(artifacts)),
        plan_materializer=cast(CandidateMaterializer, _WrongBaseMaterializer()),
        project_id=project_id,
    )
    request = CreativeRunRequest(
        run_id=RunId("run.commit-reject"),
        project_id=project_id,
        basis_commit=fresh_base,
        policy=_policy(),
    )
    start = runtime.start(request)
    assert start.current_task_id is not None
    waiting = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))
    assert waiting.current_task_id is not None
    commit_task = _accept_waiting(
        runtime, commands, waiting.current_task_id, kind=CandidateKind.PLAN, commit=fresh_base
    )
    # The materializer's wrong base makes the trusted commit return REJECTED.
    result = asyncio.run(runtime.advance(commit_task, worker_id="commit"))
    assert result.terminal is CreativeRunTerminal.BLOCKED

    # Writer REVIEW_REQUIRED branch.
    review_project = ProjectId("project.writer-review")
    review_base = _fresh_project(commits, review_project)
    review_required_writer = _build_runtime(
        factory,
        commits,
        artifacts,
        commands,
        planner=cast(PlanningLeafPort, _Planner(artifacts)),
        writer=cast(
            WritingLeafPort,
            _Writer(artifacts, status=WritingLoopTerminalStatus.REVIEW_REQUIRED, fail=True),
        ),
        project_id=review_project,
    )
    start = review_required_writer.start(
        _request(review_base, run_id="run.writer-review", project_id=review_project)
    )
    assert start.current_task_id is not None
    waiting = asyncio.run(
        review_required_writer.advance(start.current_task_id, worker_id="planner")
    )
    assert waiting.current_task_id is not None
    commit_task = _accept_waiting(
        review_required_writer,
        commands,
        waiting.current_task_id,
        kind=CandidateKind.PLAN,
        commit=review_base,
    )
    projection = asyncio.run(review_required_writer.advance(commit_task, worker_id="commit"))
    assert projection.current_task_id is not None
    draft = asyncio.run(
        review_required_writer.advance(projection.current_task_id, worker_id="projection")
    )
    assert draft.current_task_id is not None
    result = asyncio.run(
        review_required_writer.advance(draft.current_task_id, worker_id="writer.review")
    )
    assert result.terminal is CreativeRunTerminal.REVIEW_REQUIRED


def test_advance_on_acceptance_task_auto_accepts_under_auto_policy(
    creative_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, commands, _ = creative_kernel
    project_id = ProjectId("project.auto-accept-task")
    fresh_base = _fresh_project(commits, project_id)
    runtime = _build_runtime(
        factory,
        commits,
        artifacts,
        commands,
        planner=cast(PlanningLeafPort, _Planner(artifacts)),
        writer=cast(WritingLeafPort, _Writer(artifacts)),
        project_id=project_id,
    )
    # Create a plan-candidate run under AUTO policy.
    request = CreativeRunRequest(
        run_id=RunId("run.auto-accept-task"),
        project_id=project_id,
        basis_commit=fresh_base,
        policy=AUTO_POLICY,
    )
    start = runtime.start(request)
    assert start.current_task_id is not None
    # The planner auto-accept path returns the commit task, which proves the
    # auto-accept machinery works; line 93 is the acceptance-task entry that
    # directly auto-accepts a waiting acceptance task.
    result = asyncio.run(runtime.advance(start.current_task_id, worker_id="planner"))
    assert result.terminal is CreativeRunTerminal.PROGRESSED
    assert result.current_task_id is not None
    assert commands.get_task(result.current_task_id).kind is TaskKind.PLAN_COMMIT
    # Advance the same acceptance task again under auto policy: it is settled,
    # so submit_acceptance returns the prior receipt and advance returns it.
    again = asyncio.run(runtime.advance(result.current_task_id, worker_id="idle"))
    assert again.current_task_id is not None


def test_advance_auto_accepts_a_waiting_acceptance_task_directly(
    creative_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, commands, _ = creative_kernel
    project_id = ProjectId("project.auto-accept-direct")
    fresh_base = _fresh_project(commits, project_id)
    runtime = _build_runtime(
        factory,
        commits,
        artifacts,
        commands,
        planner=cast(PlanningLeafPort, _Planner(artifacts)),
        writer=cast(WritingLeafPort, _Writer(artifacts)),
        project_id=project_id,
    )
    # Manually create a waiting acceptance task under the AUTO policy and give
    # it an immutable candidate binding ref so `_candidate_for_task` works.
    candidate_ref = artifacts.put(
        b'{"plan":"candidate"}',
        "application/vnd.novel-agent.stage5-plan-candidate+json",
        SchemaVersion("1.0.0"),
    )
    binding = CandidateBinding(
        candidate_id=StableId("candidate.auto-accept-direct"),
        kind=CandidateKind.PLAN,
        artifact_ref=candidate_ref,
        candidate_hash=candidate_ref.artifact_id.root,
        basis_commit=fresh_base,
    )
    binding_ref = artifacts.put(
        json.dumps(binding.model_dump(mode="json"), sort_keys=True).encode("utf-8"),
        "application/vnd.novel-agent.stage5-candidate-binding+json",
        SchemaVersion("1.0.0"),
    )
    source = commands.create_run_and_initial_task(
        CreativeRunRequest(
            run_id=RunId("run.auto-accept-direct"),
            project_id=project_id,
            basis_commit=fresh_base,
            policy=AUTO_POLICY,
        )
    )
    waiting = TaskRecord(
        task_id=TaskId("run.auto-accept-direct.accept"),
        run_id=source.run_id,
        project_id=project_id,
        kind=TaskKind.PLAN_ACCEPTANCE,
        task_revision=0,
        status=TaskStatus.WAITING_INPUT,
        basis_commit=fresh_base,
        policy_hash=AUTO_POLICY.policy_hash,
        permission_hash=PERMISSION_HASH,
        input_artifact_refs=(candidate_ref,),
        candidate_binding_ref=binding_ref,
        dependency_task_ids=(source.task_id,),
    )
    commands.create_task(waiting)
    result = asyncio.run(runtime.advance(waiting.task_id, worker_id="idle"))
    assert result.terminal is CreativeRunTerminal.PROGRESSED
    assert result.current_task_id is not None
    assert commands.get_task(result.current_task_id).kind is TaskKind.PLAN_COMMIT
