"""Atomic Planner-gap maintenance command coverage."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.models import ProjectRow, RuntimeTaskProjectionRow
from novel_agent.adapters.postgres.runtime import RuntimeTaskQueryRepository
from novel_agent.domain.artifacts import ArtifactRef, RootKind
from novel_agent.domain.creative_runtime import (
    AutomationMode,
    CreativeRunPolicy,
    CreativeRunRequest,
    CreativeRunTerminal,
    PlanningLoopRequest,
    PlanningLoopResult,
    PlanningTerminalStatus,
)
from novel_agent.domain.generation import WritingLoopRequest
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import FreshnessDecision, FreshnessStatus
from novel_agent.domain.memory_write import (
    InformationBoundary,
    MemoryGapClassification,
    MemoryRepairFinding,
    MemoryRepairOwner,
    MemoryWriteWorkflowPhase,
    MemoryWriteWorkflowResult,
    MemoryWriteWorkflowStatus,
    NarrativePosition,
    RepairScope,
)
from novel_agent.domain.runtime import TaskKind, TaskPurpose, TaskRecord, TaskStatus
from novel_agent.domain.stage2 import AccessScope, ContractRef
from novel_agent.ports.creative_runtime import (
    CandidateMaterializer,
    PlanningLeafPort,
    WritingLeafPort,
)
from novel_agent.runtime.creative_dispatcher import CreativeDispatcher
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.creative_runtime import CreativeRuntimeService
from novel_agent.services.event_log import RunEventLogRepository
from novel_agent.services.projection import DerivedSnapshotRepository
from novel_agent.services.runtime_commands import RuntimeCommandConflictError, RuntimeCommandService
from novel_agent.services.runtime_maintenance import RuntimeSupervisor
from tests.factories import make_manifest

HASH = "sha256:" + "1" * 64
PERMISSION_HASH = "sha256:" + "2" * 64
VERSION = SchemaVersion("1.0.0")
PROJECT = ProjectId("project.test")


class _MaintenancePort:
    def __init__(
        self,
        result: MemoryWriteWorkflowResult,
        *,
        factory: sessionmaker[Session] | None = None,
        commit_after_claim: CommitId | None = None,
    ) -> None:
        self.result = result
        self._factory = factory
        self._commit_after_claim = commit_after_claim
        self.calls = 0

    async def run(
        self, _task: TaskRecord, _finding: MemoryRepairFinding
    ) -> MemoryWriteWorkflowResult:
        self.calls += 1
        if self._factory is not None and self._commit_after_claim is not None:
            with self._factory() as session, session.begin():
                project = session.get(ProjectRow, PROJECT.root)
                assert project is not None
                project.current_commit_id = self._commit_after_claim.root
        return self.result


class _RaisingMaintenancePort:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def run(
        self, _task: TaskRecord, _finding: MemoryRepairFinding
    ) -> MemoryWriteWorkflowResult:
        raise self.error


def _runtime(
    factory: sessionmaker[Session],
    artifacts: ArtifactRepository,
    commands: RuntimeCommandService,
    maintenance: _MaintenancePort,
    *,
    planner: PlanningLeafPort | None = None,
) -> CreativeRuntimeService:
    commits = CommitService(factory)
    policy = CreativeRunPolicy(
        automation_mode=AutomationMode.MANUAL,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
    )
    return CreativeRuntimeService(
        commands,
        cast(Any, object()),
        commits,
        artifacts,
        planner or cast(PlanningLeafPort, object()),
        cast(WritingLeafPort, object()),
        lambda _task: cast(WritingLoopRequest, object()),
        cast(CandidateMaterializer, object()),
        cast(CandidateMaterializer, object()),
        cast(Any, object()),
        cast(DerivedSnapshotRepository, object()),
        lambda _policy_hash: policy,
        memory_maintenance=maintenance,
    )


class _PlannerGap:
    def __init__(self, artifacts: ArtifactRepository) -> None:
        self._artifacts = artifacts
        self.requests: list[PlanningLoopRequest] = []
        self.finding: MemoryRepairFinding | None = None

    async def run(self, request: PlanningLoopRequest) -> PlanningLoopResult:
        self.requests.append(request)
        assert request.attempt_id is not None
        assert request.input_artifact_refs
        checkpoint = self._artifacts.put(
            b"{}",
            "application/vnd.novel-agent.planning-loop-checkpoint+json",
            VERSION,
        )
        boundary = InformationBoundary(
            boundary_id=StableId("boundary.u8b.planner-gap"),
            base_commit=request.basis_commit,
            maximum_visible_position=NarrativePosition(chapter_index=request.chapter_index),
            evaluator_sources_forbidden=True,
            policy_ref=ContractRef(
                contract_id=StableId("policy.u8b.planner-gap"),
                version=VERSION,
                content_hash=ArtifactId("sha256:" + "2" * 64),
            ),
        )
        finding = MemoryRepairFinding(
            finding_id=StableId("finding.u8b.planner-gap"),
            incident_id=StableId("incident.u8b.planner-gap"),
            planner_run_id=request.run_id,
            planner_task_id=request.task_id,
            planner_attempt_id=request.attempt_id,
            planner_request_id=StableId("request.u8b.planner-gap"),
            planner_intent_ref=request.input_artifact_refs[0],
            planner_checkpoint_ref=checkpoint,
            project_id=request.project_id,
            base_commit=request.basis_commit,
            basis_snapshot_id=request.basis_snapshot,
            information_boundary=boundary,
            cutoff=NarrativePosition(chapter_index=request.chapter_index),
            access_scope=AccessScope.WRITER_SAFE,
            need_id=StableId("need.u8b.planner-gap"),
            need_query="missing canonical relation",
            semantic_question="which canonical relation is missing?",
            classification=MemoryGapClassification.CANON_EXTRACTION_GAP,
            repair_owner=MemoryRepairOwner.ORDINARY_CURATOR,
            target_root_kind=RootKind.WORLD,
            repair_scope=RepairScope(field_paths=("world.relations",)),
            no_progress_key=StableId("progress.u8b.planner-gap"),
        )
        finding_ref = self._artifacts.put(
            canonical_json_bytes(finding.model_dump(mode="json")),
            "application/vnd.novel-agent.memory-repair-finding+json",
            VERSION,
        )
        self.finding = finding
        return PlanningLoopResult(
            result_id=StableId("planner-result.u8b.planner-gap"),
            run_id=request.run_id,
            task_id=request.task_id,
            status=PlanningTerminalStatus.REVIEW_REQUIRED,
            artifact_refs=(finding_ref,),
            failure_code="PLANNER_MEMORY_FACETS_UNRESOLVED",
            failure_detail="evidence-bound canonical extraction gap",
        )


@pytest.fixture
def kernel(
    tmp_path: Path,
) -> Iterator[
    tuple[
        sessionmaker[Session],
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
        factory,
        RunEventLogRepository(factory),
        lambda _project_id: PERMISSION_HASH,
        artifacts=artifacts,
    )
    yield factory, artifacts, commands, base
    engine.dispose()


def _request(run_id: str, base: CommitId) -> CreativeRunRequest:
    return CreativeRunRequest(
        run_id=RunId(run_id),
        project_id=PROJECT,
        basis_commit=base,
        policy=CreativeRunPolicy(
            automation_mode=AutomationMode.MANUAL,
            policy_hash=HASH,
            permission_hash=PERMISSION_HASH,
        ),
    )


def _ref(digit: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=ArtifactId("sha256:" + digit * 64),
        media_type="application/json",
        byte_length=1,
        schema_version=VERSION,
    )


def _finding(
    planner: TaskRecord,
    attempt_id: StableId,
    *,
    finding_id: str = "finding.u8b.command",
    classification: MemoryGapClassification = MemoryGapClassification.CANON_EXTRACTION_GAP,
    owner: MemoryRepairOwner = MemoryRepairOwner.GRAPH_CURATOR,
) -> MemoryRepairFinding:
    return MemoryRepairFinding(
        finding_id=StableId(finding_id),
        incident_id=StableId("incident.u8b.command"),
        planner_run_id=planner.run_id,
        planner_task_id=planner.task_id,
        planner_attempt_id=attempt_id,
        planner_request_id=StableId("request.u8b.command"),
        planner_intent_ref=_ref("3"),
        planner_checkpoint_ref=_ref("4"),
        project_id=planner.project_id,
        base_commit=planner.basis_commit,
        information_boundary=InformationBoundary(
            boundary_id=StableId("boundary.u8b.command"),
            base_commit=planner.basis_commit,
            maximum_visible_position=NarrativePosition(chapter_index=4),
            evaluator_sources_forbidden=True,
            policy_ref=ContractRef(
                contract_id=StableId("policy.u8b.command"),
                version=VERSION,
                content_hash=ArtifactId("sha256:" + "5" * 64),
            ),
        ),
        cutoff=NarrativePosition(chapter_index=4),
        access_scope=AccessScope.WRITER_SAFE,
        need_id=StableId("need.u8b.command"),
        need_query="which relation is missing?",
        semantic_question="which visible source supports the relation?",
        classification=classification,
        repair_owner=owner,
        target_root_kind=RootKind.WORLD,
        repair_scope=RepairScope(field_paths=("relations",)),
        no_progress_key=StableId("progress.u8b.command"),
    )


def _maintenance(
    planner: TaskRecord, finding_ref: ArtifactRef, finding: MemoryRepairFinding
) -> TaskRecord:
    return TaskRecord(
        task_id=RuntimeCommandService._maintenance_task_id(finding),
        run_id=planner.run_id,
        project_id=planner.project_id,
        kind=TaskKind.MAINTENANCE,
        purpose=TaskPurpose.DERIVED_MAINTENANCE,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=planner.basis_commit,
        basis_snapshot=planner.basis_snapshot,
        policy_hash=planner.policy_hash,
        permission_hash=planner.permission_hash,
        input_artifact_refs=(finding_ref,),
        failure_budget=planner.failure_budget,
        retry_tranche_size=planner.retry_tranche_size,
        chapter_index=planner.chapter_index,
        target_chapters=planner.target_chapters,
        horizon_start=planner.horizon_start,
        horizon_end=planner.horizon_end,
        protected_chapter_index=planner.protected_chapter_index,
    )


def test_maintenance_identity_is_bound_to_finding_and_owner_not_long_run() -> None:
    planner_run = RunId("run." + "r" * 120)
    planner = TaskRecord(
        task_id=TaskId("task.u8b.identity"),
        run_id=planner_run,
        project_id=PROJECT,
        kind=TaskKind.PLAN_CANDIDATE,
        task_revision=0,
        status=TaskStatus.BLOCKED,
        basis_commit=CommitId("sha256:" + "a" * 64),
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
    )
    finding = _finding(planner, StableId("attempt.u8b.identity"))
    other_finding = finding.model_copy(update={"finding_id": StableId("finding.u8b.other")})
    other_owner = finding.model_copy(update={"repair_owner": MemoryRepairOwner.OPERATOR})

    first = RuntimeCommandService._maintenance_task_id(finding)
    assert first.root == "maintenance.finding.u8b.command.graph_curator"
    assert len(first.root) <= 128
    assert RuntimeCommandService._maintenance_task_id(other_finding) != first
    assert RuntimeCommandService._maintenance_task_id(other_owner) != first


def test_maintenance_identity_falls_back_when_finding_id_is_max_length() -> None:
    planner = TaskRecord(
        task_id=TaskId("task.u8b.identity.long-finding"),
        run_id=RunId("run.u8b.identity.long-finding"),
        project_id=PROJECT,
        kind=TaskKind.PLAN_CANDIDATE,
        task_revision=0,
        status=TaskStatus.BLOCKED,
        basis_commit=CommitId("sha256:" + "a" * 64),
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
    )
    finding = _finding(planner, StableId("attempt.u8b.long-finding")).model_copy(
        update={"finding_id": StableId("f" * 128)}
    )

    task_id = RuntimeCommandService._maintenance_task_id(finding)

    assert task_id.root == "maintenance.incident.u8b.command.attempt.u8b.long-finding.graph_curator"
    assert len(task_id.root) <= 128


def test_gap_settlement_blocks_planner_and_creates_unblocked_maintenance(
    kernel: tuple[sessionmaker[Session], ArtifactRepository, RuntimeCommandService, CommitId],
) -> None:
    _, artifacts, commands, base = kernel
    planner = commands.create_run_and_initial_task(_request("run.u8b.gap", base))
    attempt, fence = commands.claim(planner.task_id, worker_id="planner")
    commands.mark_started(fence)
    finding = _finding(planner, attempt.attempt_id)
    finding_ref = artifacts.put(
        canonical_json_bytes(finding.model_dump(mode="json")),
        "application/vnd.novel-agent.memory-repair-finding+json",
        VERSION,
    )
    maintenance = _maintenance(planner, finding_ref, finding)

    created = commands.settle_gap_and_create_maintenance(
        fence,
        finding_ref=finding_ref,
        maintenance_task=maintenance,
    )

    assert created == maintenance
    assert commands.get_task(planner.task_id).status is TaskStatus.BLOCKED
    assert commands.get_task(planner.task_id).current_attempt_id is None
    assert commands.get_task(maintenance.task_id).status is TaskStatus.READY
    assert commands.get_task(maintenance.task_id).dependency_task_ids == ()
    assert (
        commands.settle_gap_and_create_maintenance(
            fence,
            finding_ref=finding_ref,
            maintenance_task=maintenance,
        )
        == maintenance
    )


def test_gap_command_rejects_non_canon_classification_without_mutation(
    kernel: tuple[sessionmaker[Session], ArtifactRepository, RuntimeCommandService, CommitId],
) -> None:
    _, artifacts, commands, base = kernel
    planner = commands.create_run_and_initial_task(_request("run.u8b.bad-gap", base))
    attempt, fence = commands.claim(planner.task_id, worker_id="planner")
    commands.mark_started(fence)
    finding = _finding(
        planner,
        attempt.attempt_id,
        classification=MemoryGapClassification.SOURCE_EVIDENCE_ABSENT,
        owner=MemoryRepairOwner.OPERATOR,
    )
    finding_ref = artifacts.put(
        canonical_json_bytes(finding.model_dump(mode="json")),
        "application/vnd.novel-agent.memory-repair-finding+json",
        VERSION,
    )
    with pytest.raises(RuntimeCommandConflictError, match="only CANON_EXTRACTION_GAP"):
        commands.settle_gap_and_create_maintenance(
            fence,
            finding_ref=finding_ref,
            maintenance_task=_maintenance(planner, finding_ref, finding),
        )
    assert commands.get_task(planner.task_id).status is TaskStatus.RUNNING


def test_runtime_routes_planner_gap_to_independent_maintenance_task(
    kernel: tuple[sessionmaker[Session], ArtifactRepository, RuntimeCommandService, CommitId],
) -> None:
    factory, artifacts, commands, base = kernel
    author_ref = artifacts.put(b"author intent", "text/plain", VERSION)
    request = _request("run.u8b.runtime-gap-entry", base).model_copy(
        update={"input_artifact_refs": (author_ref,)}
    )
    planner = _PlannerGap(artifacts)
    maintenance_port = _MaintenancePort(
        MemoryWriteWorkflowResult(
            request_id=StableId("request.u8b.runtime-gap-entry"),
            status=MemoryWriteWorkflowStatus.NOOP,
            workflow_phase=MemoryWriteWorkflowPhase.COMPLETE,
            canonical_commit_accepted=False,
            base_commit=base,
        )
    )
    runtime = _runtime(
        factory,
        artifacts,
        commands,
        maintenance_port,
        planner=planner,
    )
    planner_task = commands.create_run_and_initial_task(request)

    result = asyncio.run(runtime.advance(planner_task.task_id, worker_id="planner"))

    assert result.terminal is CreativeRunTerminal.PROGRESSED
    assert result.reason_code == "planner_memory_gap_maintenance_created"
    blocked = commands.get_task(planner_task.task_id)
    assert blocked.status is TaskStatus.BLOCKED
    assert blocked.block_cause == MemoryGapClassification.CANON_EXTRACTION_GAP.value
    assert planner.finding is not None
    maintenance = commands.get_task(RuntimeCommandService._maintenance_task_id(planner.finding))
    assert maintenance.kind is TaskKind.MAINTENANCE
    assert maintenance.purpose is TaskPurpose.DERIVED_MAINTENANCE
    assert maintenance.status is TaskStatus.READY
    assert maintenance.dependency_task_ids == ()
    assert maintenance_port.calls == 0
    assert len(planner.requests) == 1


def test_committed_maintenance_supersedes_old_planner_and_creates_new_basis_retry(
    kernel: tuple[sessionmaker[Session], ArtifactRepository, RuntimeCommandService, CommitId],
) -> None:
    factory, artifacts, commands, base = kernel
    planner = commands.create_run_and_initial_task(_request("run.u8b.commit", base))
    planner_attempt, planner_fence = commands.claim(planner.task_id, worker_id="planner")
    commands.mark_started(planner_fence)
    finding = _finding(planner, planner_attempt.attempt_id)
    finding_ref = artifacts.put(
        canonical_json_bytes(finding.model_dump(mode="json")),
        "application/vnd.novel-agent.memory-repair-finding+json",
        VERSION,
    )
    maintenance = _maintenance(planner, finding_ref, finding)
    commands.settle_gap_and_create_maintenance(
        planner_fence,
        finding_ref=finding_ref,
        maintenance_task=maintenance,
    )
    _, maintenance_fence = commands.claim(maintenance.task_id, worker_id="curator")
    commands.mark_started(maintenance_fence)

    new_commit = CommitId("sha256:" + "d" * 64)
    snapshot = StableId("snapshot.u8b.new")
    with factory() as session, session.begin():
        project = session.get(ProjectRow, PROJECT.root)
        assert project is not None
        project.current_commit_id = new_commit.root
    workflow_result = MemoryWriteWorkflowResult(
        request_id=StableId("request.u8b.maintenance"),
        status=MemoryWriteWorkflowStatus.COMMITTED,
        workflow_phase=MemoryWriteWorkflowPhase.COMPLETE,
        canonical_commit_accepted=True,
        base_commit=base,
        resulting_commit=new_commit,
        accepted_candidate_id=StableId("candidate.u8b.accepted"),
        validation_receipt=_ref("6"),
        guardian_receipt=_ref("7"),
        commit_receipt=_ref("8"),
        projection_receipt_ref=_ref("9"),
        freshness_receipt_ref=_ref("a"),
        projection_snapshot_id=snapshot,
        freshness=FreshnessDecision(
            status=FreshnessStatus.READY,
            canonical_commit=new_commit,
            r1_basis_commit=new_commit,
            required_snapshot_id=snapshot,
            reason="fake committed maintenance",
        ),
    )
    result_ref = artifacts.put(
        canonical_json_bytes(workflow_result.model_dump(mode="json")),
        "application/vnd.novel-agent.memory-write-result+json",
        VERSION,
    )
    old_planner = commands.get_task(planner.task_id)
    retry = old_planner.model_copy(
        update={
            "task_id": RuntimeCommandService._planner_retry_task_id(old_planner, workflow_result),
            "task_revision": 0,
            "status": TaskStatus.READY,
            "basis_commit": new_commit,
            "basis_snapshot": snapshot,
            "dependency_task_ids": (maintenance.task_id,),
            "terminal_artifact_refs": (),
            "block_cause": None,
            "superseded": False,
        }
    )

    result = commands.settle_maintenance_and_retry_planner(
        maintenance_fence,
        workflow_result_ref=result_ref,
        retry_task=retry,
    )

    assert result == retry
    assert commands.get_task(maintenance.task_id).status is TaskStatus.SUCCEEDED
    superseded = commands.get_task(planner.task_id)
    assert superseded.status is TaskStatus.CANCELLED
    assert superseded.superseded is True
    assert superseded.basis_commit == base
    assert commands.get_task(retry.task_id).basis_commit == new_commit
    assert (
        commands.settle_maintenance_and_retry_planner(
            maintenance_fence,
            workflow_result_ref=result_ref,
            retry_task=retry,
        )
        == retry
    )


def _create_runtime_maintenance(
    factory: sessionmaker[Session],
    artifacts: ArtifactRepository,
    commands: RuntimeCommandService,
    base: CommitId,
    run_id: str,
    *,
    finding_id: str = "finding.u8b.command",
) -> tuple[TaskRecord, TaskRecord]:
    planner = commands.create_run_and_initial_task(_request(run_id, base))
    attempt, fence = commands.claim(planner.task_id, worker_id="planner")
    commands.mark_started(fence)
    finding = _finding(planner, attempt.attempt_id, finding_id=finding_id)
    finding_ref = artifacts.put(
        canonical_json_bytes(finding.model_dump(mode="json")),
        "application/vnd.novel-agent.memory-repair-finding+json",
        VERSION,
    )
    maintenance = _maintenance(planner, finding_ref, finding)
    commands.settle_gap_and_create_maintenance(
        fence,
        finding_ref=finding_ref,
        maintenance_task=maintenance,
    )
    return planner, maintenance


def test_runtime_maintenance_noop_blocks_without_planner_retry(
    kernel: tuple[sessionmaker[Session], ArtifactRepository, RuntimeCommandService, CommitId],
) -> None:
    factory, artifacts, commands, base = kernel
    planner, maintenance = _create_runtime_maintenance(
        factory, artifacts, commands, base, "run.u8b.runtime-noop"
    )
    port = _MaintenancePort(
        MemoryWriteWorkflowResult(
            request_id=StableId("request.u8b.runtime-noop"),
            status=MemoryWriteWorkflowStatus.NOOP,
            workflow_phase=MemoryWriteWorkflowPhase.COMPLETE,
            canonical_commit_accepted=False,
            base_commit=base,
        )
    )
    runtime = _runtime(factory, artifacts, commands, port)

    result = asyncio.run(runtime.advance(maintenance.task_id, worker_id="maintenance"))

    assert result.terminal is CreativeRunTerminal.REVIEW_REQUIRED
    assert result.reason_code == "memory_maintenance_noop"
    assert port.calls == 1
    assert commands.get_task(maintenance.task_id).status is TaskStatus.BLOCKED
    assert commands.get_task(planner.task_id).status is TaskStatus.BLOCKED


def test_runtime_maintenance_defers_when_project_writer_lane_is_busy(
    kernel: tuple[sessionmaker[Session], ArtifactRepository, RuntimeCommandService, CommitId],
) -> None:
    factory, artifacts, commands, base = kernel
    _, held = _create_runtime_maintenance(
        factory, artifacts, commands, base, "run.u8b.runtime-lane-holder"
    )
    _, held_fence = commands.claim(held.task_id, worker_id="lane-holder")
    commands.mark_started(held_fence)
    held_fence = commands.claim_writer_lane(held_fence)

    _, waiting = _create_runtime_maintenance(
        factory,
        artifacts,
        commands,
        base,
        "run.u8b.runtime-lane-waiter",
        finding_id="finding.u8b.runtime-lane-waiter",
    )
    port = _MaintenancePort(
        MemoryWriteWorkflowResult(
            request_id=StableId("request.u8b.runtime-lane-waiter"),
            status=MemoryWriteWorkflowStatus.NOOP,
            workflow_phase=MemoryWriteWorkflowPhase.COMPLETE,
            canonical_commit_accepted=False,
            base_commit=base,
        )
    )
    runtime = _runtime(factory, artifacts, commands, port)

    result = asyncio.run(runtime.advance(waiting.task_id, worker_id="lane-waiter"))

    assert result.terminal is CreativeRunTerminal.WAITING_RETRY
    assert result.reason_code == "writer_lane_busy"
    assert commands.get_task(waiting.task_id).status is TaskStatus.WAITING_RETRY
    assert port.calls == 0
    commands.verify_writer_lane(held_fence)


def test_runtime_maintenance_fatal_carries_terminal_artifact_into_settlement(
    kernel: tuple[sessionmaker[Session], ArtifactRepository, RuntimeCommandService, CommitId],
) -> None:
    factory, artifacts, commands, base = kernel
    _, maintenance = _create_runtime_maintenance(
        factory, artifacts, commands, base, "run.u8b.runtime-terminal-ref"
    )
    terminal = MemoryWriteWorkflowResult(
        request_id=StableId("request.u8b.runtime-terminal-ref"),
        status=MemoryWriteWorkflowStatus.FATAL,
        workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
        canonical_commit_accepted=False,
        base_commit=base,
        terminal_codes=("CHECKPOINT_PERSIST_FAILED", "RuntimeError"),
    )
    terminal_ref = artifacts.put(
        canonical_json_bytes(terminal.model_dump(mode="json")),
        "application/vnd.novel-agent.terminal-result+json",
        VERSION,
    )
    port = _MaintenancePort(terminal.model_copy(update={"terminal_result_ref": terminal_ref}))
    runtime = _runtime(factory, artifacts, commands, port)

    result = asyncio.run(runtime.advance(maintenance.task_id, worker_id="maintenance"))

    settled = commands.get_task(maintenance.task_id)
    assert result.terminal is CreativeRunTerminal.BLOCKED
    assert settled.status is TaskStatus.BLOCKED
    assert terminal_ref in settled.terminal_artifact_refs
    workflow_result_refs = tuple(
        ref
        for ref in settled.terminal_artifact_refs
        if ref.media_type == "application/vnd.novel-agent.memory-write-workflow-result+json"
    )
    assert len(workflow_result_refs) == 1
    persisted = MemoryWriteWorkflowResult.model_validate_json(
        artifacts.read_verified(workflow_result_refs[0]), strict=True
    )
    assert persisted.terminal_result_ref == terminal_ref


def test_runtime_maintenance_adapter_validation_error_settles_blocked(
    kernel: tuple[sessionmaker[Session], ArtifactRepository, RuntimeCommandService, CommitId],
) -> None:
    factory, artifacts, commands, base = kernel
    _, maintenance = _create_runtime_maintenance(
        factory, artifacts, commands, base, "run.u8b.runtime-adapter-rejected"
    )
    runtime = _runtime(
        factory,
        artifacts,
        commands,
        cast(Any, _RaisingMaintenancePort(ValueError("manifest mismatch"))),
    )

    result = asyncio.run(runtime.advance(maintenance.task_id, worker_id="maintenance"))

    settled = commands.get_task(maintenance.task_id)
    assert result.terminal is CreativeRunTerminal.REVIEW_REQUIRED
    assert result.reason_code == "memory_maintenance_adapter_rejected"
    assert settled.status is TaskStatus.BLOCKED
    assert settled.current_attempt_id is None


def test_runtime_maintenance_adapter_resource_error_settles_recovery_pending(
    kernel: tuple[sessionmaker[Session], ArtifactRepository, RuntimeCommandService, CommitId],
) -> None:
    factory, artifacts, commands, base = kernel
    _, maintenance = _create_runtime_maintenance(
        factory, artifacts, commands, base, "run.u8b.runtime-adapter-resource"
    )
    runtime = _runtime(
        factory,
        artifacts,
        commands,
        cast(Any, _RaisingMaintenancePort(OSError("object store unavailable"))),
    )

    result = asyncio.run(runtime.advance(maintenance.task_id, worker_id="maintenance"))

    settled = commands.get_task(maintenance.task_id)
    assert result.terminal is CreativeRunTerminal.RECOVERY_PENDING
    assert result.reason_code == "memory_maintenance_external_resource_unavailable"
    assert settled.status is TaskStatus.RECOVERY_PENDING
    assert settled.current_attempt_id is None


def test_runtime_maintenance_result_persistence_failure_fences_attempt(
    kernel: tuple[sessionmaker[Session], ArtifactRepository, RuntimeCommandService, CommitId],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, artifacts, commands, base = kernel
    _, maintenance = _create_runtime_maintenance(
        factory, artifacts, commands, base, "run.u8b.runtime-result-persist-resource"
    )
    port = _MaintenancePort(
        MemoryWriteWorkflowResult(
            request_id=StableId("request.u8b.runtime-result-persist-resource"),
            status=MemoryWriteWorkflowStatus.NOOP,
            workflow_phase=MemoryWriteWorkflowPhase.COMPLETE,
            canonical_commit_accepted=False,
            base_commit=base,
        )
    )

    def fail_put(*_args: object, **_kwargs: object) -> ArtifactRef:
        raise OSError("workflow result object store unavailable")

    monkeypatch.setattr(artifacts, "put", fail_put)
    runtime = _runtime(factory, artifacts, commands, port)

    result = asyncio.run(runtime.advance(maintenance.task_id, worker_id="maintenance"))

    settled = commands.get_task(maintenance.task_id)
    assert result.terminal is CreativeRunTerminal.RECOVERY_PENDING
    assert result.reason_code == "memory_maintenance_result_persist_unavailable"
    assert settled.status is TaskStatus.RECOVERY_PENDING
    assert settled.current_attempt_id is None
    assert port.calls == 1


def test_runtime_maintenance_commit_creates_new_basis_planner_retry(
    kernel: tuple[sessionmaker[Session], ArtifactRepository, RuntimeCommandService, CommitId],
) -> None:
    factory, artifacts, commands, base = kernel
    planner, maintenance = _create_runtime_maintenance(
        factory, artifacts, commands, base, "run.u8b.runtime-commit"
    )
    new_commit = CommitId("sha256:" + "e" * 64)
    snapshot = StableId("snapshot.u8b.runtime-commit")
    port = _MaintenancePort(
        MemoryWriteWorkflowResult(
            request_id=StableId("request.u8b.runtime-commit"),
            status=MemoryWriteWorkflowStatus.COMMITTED,
            workflow_phase=MemoryWriteWorkflowPhase.COMPLETE,
            canonical_commit_accepted=True,
            base_commit=base,
            resulting_commit=new_commit,
            accepted_candidate_id=StableId("candidate.u8b.runtime-commit"),
            validation_receipt=_ref("6"),
            commit_receipt=_ref("7"),
            projection_receipt_ref=_ref("8"),
            freshness_receipt_ref=_ref("9"),
            projection_snapshot_id=snapshot,
            freshness=FreshnessDecision(
                status=FreshnessStatus.READY,
                canonical_commit=new_commit,
                r1_basis_commit=new_commit,
                required_snapshot_id=snapshot,
                reason="fake committed maintenance",
            ),
        ),
        factory=factory,
        commit_after_claim=new_commit,
    )
    runtime = _runtime(factory, artifacts, commands, port)

    result = asyncio.run(runtime.advance(maintenance.task_id, worker_id="maintenance"))

    retry_id = RuntimeCommandService._planner_retry_task_id(planner, port.result)
    assert result.terminal is CreativeRunTerminal.PROGRESSED
    assert result.current_task_id == retry_id
    assert port.calls == 1
    assert commands.get_task(maintenance.task_id).status is TaskStatus.SUCCEEDED
    assert commands.get_task(planner.task_id).status is TaskStatus.CANCELLED
    assert commands.get_task(planner.task_id).superseded is True
    retry = commands.get_task(retry_id)
    assert retry.status is TaskStatus.READY
    assert retry.basis_commit == new_commit
    assert retry.basis_snapshot == snapshot
    assert retry.dependency_task_ids == (maintenance.task_id,)


def test_runtime_maintenance_rejects_missing_finding_without_leaving_running_task(
    kernel: tuple[sessionmaker[Session], ArtifactRepository, RuntimeCommandService, CommitId],
) -> None:
    factory, artifacts, commands, base = kernel
    task = TaskRecord(
        task_id=TaskId("run.u8b.runtime-invalid.maintenance"),
        run_id=RunId("run.u8b.runtime-invalid"),
        project_id=PROJECT,
        kind=TaskKind.MAINTENANCE,
        purpose=TaskPurpose.DERIVED_MAINTENANCE,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=base,
        basis_snapshot=StableId("snapshot.u8b.runtime-invalid"),
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
        input_artifact_refs=(_ref("a"),),
    )
    commands.create_task(task)
    port = _MaintenancePort(
        MemoryWriteWorkflowResult(
            request_id=StableId("request.u8b.runtime-invalid"),
            status=MemoryWriteWorkflowStatus.NOOP,
            workflow_phase=MemoryWriteWorkflowPhase.COMPLETE,
            canonical_commit_accepted=False,
            base_commit=base,
        )
    )
    runtime = _runtime(factory, artifacts, commands, port)

    result = asyncio.run(runtime.advance(task.task_id, worker_id="maintenance"))

    assert result.terminal is CreativeRunTerminal.REVIEW_REQUIRED
    assert result.reason_code == "maintenance_finding_rejected"
    assert commands.get_task(task.task_id).status is TaskStatus.BLOCKED
    assert port.calls == 0


def test_dispatcher_admits_maintenance_and_supervisor_remains_read_only(
    kernel: tuple[sessionmaker[Session], ArtifactRepository, RuntimeCommandService, CommitId],
) -> None:
    factory, artifacts, commands, base = kernel
    planner, maintenance = _create_runtime_maintenance(
        factory, artifacts, commands, base, "run.u8b.runtime-dispatch"
    )
    port = _MaintenancePort(
        MemoryWriteWorkflowResult(
            request_id=StableId("request.u8b.runtime-dispatch"),
            status=MemoryWriteWorkflowStatus.NOOP,
            workflow_phase=MemoryWriteWorkflowPhase.COMPLETE,
            canonical_commit_accepted=False,
            base_commit=base,
        )
    )
    runtime = _runtime(factory, artifacts, commands, port)
    dispatcher = CreativeDispatcher(
        RuntimeTaskQueryRepository(factory),
        runtime,
        worker_id="maintenance-dispatcher",
        project_id=PROJECT,
        run_id=maintenance.run_id,
    )

    result = asyncio.run(dispatcher.poll_one())

    assert result is not None
    assert result.current_task_id == maintenance.task_id
    assert commands.get_task(maintenance.task_id).status is TaskStatus.BLOCKED
    assert commands.get_task(planner.task_id).status is TaskStatus.BLOCKED
    before_commit = CommitService(factory).current_commit(PROJECT)
    findings = RuntimeSupervisor(factory).inspect()
    after_commit = CommitService(factory).current_commit(PROJECT)
    assert findings == ()
    assert after_commit == before_commit
    assert port.calls == 1


def test_runtime_maintenance_human_required_can_resume_from_bound_checkpoint(
    kernel: tuple[sessionmaker[Session], ArtifactRepository, RuntimeCommandService, CommitId],
) -> None:
    factory, artifacts, commands, base = kernel
    _, maintenance = _create_runtime_maintenance(
        factory, artifacts, commands, base, "run.u8b.runtime-human"
    )
    checkpoint_ref = _ref("b")
    port = _MaintenancePort(
        MemoryWriteWorkflowResult(
            request_id=StableId("request.u8b.runtime-human"),
            status=MemoryWriteWorkflowStatus.HUMAN_REQUIRED,
            workflow_phase=MemoryWriteWorkflowPhase.PRECOMMIT,
            canonical_commit_accepted=False,
            base_commit=base,
            checkpoint_ref=checkpoint_ref,
        )
    )
    runtime = _runtime(factory, artifacts, commands, port)

    result = asyncio.run(runtime.advance(maintenance.task_id, worker_id="maintenance"))

    waiting = commands.get_task(maintenance.task_id)
    assert result.terminal is CreativeRunTerminal.REVIEW_REQUIRED
    assert waiting.status is TaskStatus.WAITING_INPUT
    assert result.next_legal_commands == ("resume", "cancel")
    resumed = commands.resume(
        maintenance.task_id,
        command_id=StableId("resume.u8b.runtime-human"),
        actor_id="operator",
        reason="human decision recorded in the Memory Write owner",
        observed_revision=waiting.task_revision,
    )
    assert resumed.status is TaskStatus.READY
    assert resumed.current_attempt_id is None


def test_supervisor_only_reports_stuck_maintenance_without_mutation(
    kernel: tuple[sessionmaker[Session], ArtifactRepository, RuntimeCommandService, CommitId],
) -> None:
    factory, artifacts, commands, base = kernel
    _, maintenance = _create_runtime_maintenance(
        factory, artifacts, commands, base, "run.u8b.runtime-supervisor"
    )
    old = datetime.now(UTC) - timedelta(days=1)
    with factory() as session, session.begin():
        row = session.get(RuntimeTaskProjectionRow, maintenance.task_id.root)
        assert row is not None
        row.status = TaskStatus.WAITING_RETRY.value
        row.updated_at = old
    before_commit = CommitService(factory).current_commit(PROJECT)

    findings = RuntimeSupervisor(factory, stuck_after=timedelta(minutes=1)).inspect()

    after = commands.get_task(maintenance.task_id)
    assert len(findings) == 1
    assert findings[0].task_id == maintenance.task_id
    assert findings[0].proposed_command == "pause"
    with factory() as session:
        row = session.get(RuntimeTaskProjectionRow, maintenance.task_id.root)
        assert row is not None
        assert row.status == TaskStatus.WAITING_RETRY.value
    assert after.status is TaskStatus.READY
    assert after.current_attempt_id is None
    assert CommitService(factory).current_commit(PROJECT) == before_commit
