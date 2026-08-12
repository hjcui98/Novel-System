"""Deterministic edge coverage for Stage 5 runtime command, recovery, and maintenance boundaries."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.models import (
    ProjectWriterClaimRow,
    RuntimeTaskAttemptRow,
    RuntimeTaskProjectionRow,
)
from novel_agent.adapters.runtime.isolated import StrictDeterministicCandidateMaterializer
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.changes import CommitRequest, CommitStatus
from novel_agent.domain.creative_runtime import (
    AcceptanceCommand,
    AcceptanceDecision,
    AcceptanceReceipt,
    AcceptedCandidateBinding,
    ActorKind,
    AutomationMode,
    CandidateBinding,
    CandidateKind,
    CreativeRunPolicy,
    CreativeRunRequest,
)
from novel_agent.domain.ids import CommitId, ProjectId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.runtime import (
    AttemptFence,
    AttemptOutcome,
    EffectReceipt,
    EffectStatus,
    ResumabilityStatus,
    RunCheckpoint,
    TaskKind,
    TaskRecord,
    TaskStatus,
)
from novel_agent.ports.creative_runtime import EffectStatusResolver
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository
from novel_agent.services.runtime_acceptance import RuntimeAcceptanceService
from novel_agent.services.runtime_commands import (
    RuntimeCommandConflictError,
    RuntimeCommandService,
    StaleAttemptFenceError,
)
from novel_agent.services.runtime_maintenance import (
    MaintenanceCommand,
    MaintenanceDisposition,
    MaintenanceKind,
    RuntimeMaintenanceService,
    RuntimeSupervisor,
)
from novel_agent.services.runtime_projection import project_runtime_events
from novel_agent.services.runtime_recovery import RuntimeRecoveryService
from tests.factories import make_commit_request, make_manifest

HASH = "sha256:" + "1" * 64
PERMISSION_HASH = "sha256:" + "2" * 64
NOW = datetime(2026, 8, 10, tzinfo=UTC)


@pytest.fixture
def edge_kernel(
    tmp_path: Path,
) -> Iterator[
    tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
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
    events = RunEventLogRepository(factory)
    commands = RuntimeCommandService(factory, events, lambda _project_id: PERMISSION_HASH)
    yield factory, commits, artifacts, events, commands, base
    engine.dispose()


def _policy() -> CreativeRunPolicy:
    return CreativeRunPolicy(
        automation_mode=AutomationMode.MANUAL,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
    )


def _request(run_id: str, base: CommitId) -> CreativeRunRequest:
    return CreativeRunRequest(
        run_id=RunId(run_id),
        project_id=ProjectId("project.test"),
        basis_commit=base,
        policy=_policy(),
    )


def _effect(identity: str, *, attempt_no: int = 1) -> EffectReceipt:
    return EffectReceipt(
        effect_identity=StableId(identity),
        external_system="provider",
        request_identity=StableId(f"request.{identity}"),
        status=EffectStatus.REQUESTED,
        attempt_no=attempt_no,
    )


def _binding_ref(
    artifacts: ArtifactRepository, candidate: CandidateBinding
) -> ArtifactRef:
    return artifacts.put(
        canonical_json_bytes(candidate.model_dump(mode="json")),
        "application/vnd.novel-agent.stage5-candidate-binding+json",
        SchemaVersion("1.0.0"),
    )


def test_create_task_is_idempotent_and_identity_collision_is_conflict(
    edge_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    _, _, _, _, commands, base = edge_kernel
    task = commands.create_run_and_initial_task(_request("run.idempotent", base))
    assert commands.create_task(task) == task
    with pytest.raises(RuntimeCommandConflictError, match="identity collision"):
        commands.create_task(task.model_copy(update={"priority": 99}))
    missing_dep = task.model_copy(
        update={
            "task_id": TaskId("run.idempotent.missing-dep"),
            "dependency_task_ids": (TaskId("task.absent"),),
        }
    )
    with pytest.raises(RuntimeCommandConflictError, match="dependency does not exist"):
        commands.create_task(missing_dep)


def test_mark_recovery_pending_is_idempotent_and_rejects_settled(
    edge_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    _, _, _, events, commands, base = edge_kernel
    task = commands.create_run_and_initial_task(_request("run.recovery-pending", base))
    _, fence = commands.claim(task.task_id, worker_id="worker")
    commands.mark_started(fence)
    commands.record_effect_requested(fence, _effect("effect.recovery-pending"))
    pending = commands.mark_recovery_pending(
        task.task_id,
        command_id=StableId("recovery-pending.one"),
        actor_id="reconciler",
        reason="effect unresolved",
    )
    assert pending.status is TaskStatus.RECOVERY_PENDING
    again = commands.mark_recovery_pending(
        task.task_id,
        command_id=StableId("recovery-pending.two"),
        actor_id="reconciler",
        reason="still unresolved",
    )
    assert again.status is TaskStatus.RECOVERY_PENDING
    commands.record_effect_terminal(
        fence,
        _effect("effect.recovery-pending").model_copy(update={"status": EffectStatus.COMPLETED}),
    )
    commands.operator_reconcile_attempt(
        task.task_id,
        command_id=StableId("reconcile.recovery-pending"),
        actor_id="operator",
        reason="worker confirmed dead",
    )
    task = commands.get_task(task.task_id)
    assert task.status is TaskStatus.WAITING_RETRY
    commands.control(
        task.task_id,
        command_id=StableId("control.retry.recovery"),
        action="retry",
        actor_id="operator",
        reason="retry from settled checkpoint",
    )
    _, retry_fence = commands.claim(task.task_id, worker_id="worker.retry")
    commands.mark_started(retry_fence)
    commands.settle_attempt(
        retry_fence,
        outcome=AttemptOutcome.SUCCEEDED,
        terminal_status=TaskStatus.SUCCEEDED,
    )
    with pytest.raises(RuntimeCommandConflictError, match="settled task"):
        commands.mark_recovery_pending(
            task.task_id,
            command_id=StableId("recovery-pending.settled"),
            actor_id="reconciler",
            reason="late",
        )
    rebuilt = project_runtime_events(events.replay(task.run_id))
    assert rebuilt.tasks[task.task_id.root].status is TaskStatus.SUCCEEDED


def test_save_checkpoint_rejects_unresolved_frontier_and_identity_collision(
    edge_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    _, _, artifacts, events, commands, base = edge_kernel
    task = commands.create_run_and_initial_task(_request("run.checkpoint-edge", base))
    _, fence = commands.claim(task.task_id, worker_id="worker")
    commands.mark_started(fence)
    state = artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0"))
    position = events.replay(task.run_id)[-1].sequence_no
    commands.record_effect_requested(fence, _effect("effect.checkpoint"))
    unresolved = RunCheckpoint(
        checkpoint_id=StableId("checkpoint.unresolved"),
        run_id=task.run_id,
        event_position=position,
        logical_stage="edge",
        state_artifact_ref=state,
        resumability_status=ResumabilityStatus.RESUMABLE,
    )
    with pytest.raises(RuntimeCommandConflictError, match="settled effect frontier"):
        commands.save_checkpoint(fence, unresolved)
    commands.record_effect_terminal(
        fence, _effect("effect.checkpoint").model_copy(update={"status": EffectStatus.COMPLETED})
    )
    frontier = RunCheckpoint(
        checkpoint_id=StableId("checkpoint.frontier"),
        run_id=task.run_id,
        event_position=events.replay(task.run_id)[-1].sequence_no,
        logical_stage="edge",
        state_artifact_ref=state,
        completed_effect_ids=(StableId("effect.checkpoint"),),
        resumability_status=ResumabilityStatus.RESUMABLE,
    )
    incomplete = frontier.model_copy(
        update={
            "checkpoint_id": StableId("checkpoint.incomplete"),
            "completed_effect_ids": (),
        }
    )
    with pytest.raises(RuntimeCommandConflictError, match="effect frontier is incomplete"):
        commands.save_checkpoint(fence, incomplete)
    commands.save_checkpoint(fence, frontier)
    assert commands.save_checkpoint(fence, frontier) == frontier
    collision = frontier.model_copy(
        update={"checkpoint_id": StableId("checkpoint.frontier"), "logical_stage": "changed"}
    )
    with pytest.raises(RuntimeCommandConflictError, match="identity collision"):
        commands.save_checkpoint(fence, collision)


def test_control_retry_requires_waiting_retry_and_observed_revision(
    edge_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    _, _, _, _, commands, base = edge_kernel
    task = commands.create_run_and_initial_task(_request("run.control-retry", base))
    with pytest.raises(RuntimeCommandConflictError, match="retry requires WAITING_RETRY"):
        commands.control(
            task.task_id,
            command_id=StableId("control.retry.early"),
            action="retry",
            actor_id="operator",
            reason="too early",
        )
    with pytest.raises(RuntimeCommandConflictError, match="stale"):
        commands.control(
            task.task_id,
            command_id=StableId("control.retry.stale"),
            action="pause",
            actor_id="operator",
            reason="stale view",
            observed_revision=99,
        )
    paused = commands.control(
        task.task_id,
        command_id=StableId("control.pause.early"),
        action="pause",
        actor_id="operator",
        reason="pause before claim",
    )
    assert paused.status is TaskStatus.PENDING
    resumed = commands.resume(
        paused.task_id,
        command_id=StableId("control.resume"),
        actor_id="operator",
        reason="resume paused",
    )
    assert resumed.status is TaskStatus.READY


def test_commit_accepted_candidate_rejects_stale_lane_and_basis(
    edge_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, _, _, commands, base = edge_kernel
    task = TaskRecord(
        task_id=TaskId("task.commit-edge"),
        run_id=RunId("run.commit-edge"),
        project_id=ProjectId("project.test"),
        kind=TaskKind.PLAN_COMMIT,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=base,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
    )
    commands.create_task(task)
    _, fence = commands.claim(task.task_id, worker_id="commit-worker")
    commands.mark_started(fence)
    fence = commands.claim_writer_lane(fence)
    request = make_commit_request(base, project_id=task.project_id)
    with factory() as session, session.begin():
        row = session.get(ProjectWriterClaimRow, task.project_id.root)
        assert row is not None
        row.attempt_id = "attempt.stale"
    with pytest.raises(StaleAttemptFenceError, match="writer generation"):
        commands.commit_accepted_candidate(fence, request, commits)
    with factory() as session, session.begin():
        row = session.get(ProjectWriterClaimRow, task.project_id.root)
        assert row is not None
        row.attempt_id = fence.attempt_id.root
    wrong_basis = request.model_copy(update={"base_commit": CommitId("sha256:" + "7" * 64)})
    with pytest.raises(RuntimeCommandConflictError, match="task basis"):
        commands.commit_accepted_candidate(fence, wrong_basis, commits)


def test_require_fence_detects_missing_and_mismatched_attempts(
    edge_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, _, _, _, commands, base = edge_kernel
    task = commands.create_run_and_initial_task(_request("run.fence-edge", base))
    _, fence = commands.claim(task.task_id, worker_id="worker")
    commands.mark_started(fence)
    with factory() as session, session.begin():
        session.execute(
            delete(RuntimeTaskAttemptRow).where(
                RuntimeTaskAttemptRow.attempt_id == fence.attempt_id.root
            )
        )
    with pytest.raises(StaleAttemptFenceError, match="does not exist"):
        commands.mark_started(fence)
    other = commands.create_run_and_initial_task(_request("run.fence-forged", base))
    _, fresh_fence = commands.claim(other.task_id, worker_id="worker.2")
    forged = fresh_fence.model_copy(
        update={"claim_token": StableId("claim.forged"), "task_revision": 99}
    )
    with pytest.raises(StaleAttemptFenceError, match="does not match"):
        commands.mark_started(forged)


class _Resolution:
    def __init__(self, receipt: EffectReceipt) -> None:
        self.receipt = receipt


class _Resolver:
    def __init__(self, status: EffectStatus) -> None:
        self.status = status

    def resolve(self, receipt: EffectReceipt) -> _Resolution:
        return _Resolution(receipt.model_copy(update={"status": self.status, "completed_at": NOW}))


def test_recovery_selects_safe_checkpoint_fails_closed_on_all_guard_branches(
    edge_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, events, commands, base = edge_kernel
    task = commands.create_run_and_initial_task(_request("run.recovery-edges", base))
    _, fence = commands.claim(task.task_id, worker_id="worker.dead")
    commands.mark_started(fence)
    state = artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0"))
    position = events.replay(task.run_id)[-1].sequence_no
    safe = RunCheckpoint(
        checkpoint_id=StableId("checkpoint.recovery-edges.safe"),
        run_id=task.run_id,
        event_position=position,
        logical_stage="safe",
        state_artifact_ref=state,
        resumability_status=ResumabilityStatus.RESUMABLE,
    )
    commands.save_checkpoint(fence, safe)
    commands.record_effect_requested(fence, _effect("effect.recovery-edges"))
    recovery = RuntimeRecoveryService(
        factory,
        commands,
        RunCheckpointRepository(factory),
        artifacts,
        commits,
        cast(EffectStatusResolver, _Resolver(EffectStatus.UNCERTAIN)),
    )
    with pytest.raises(RuntimeCommandConflictError, match="remains unresolved"):
        recovery.reconcile_uncertain_effects(task.task_id)
    commands.record_effect_terminal(
        fence,
        _effect("effect.recovery-edges").model_copy(update={"status": EffectStatus.COMPLETED}),
    )

    # No resumable checkpoint: settle the task so its latest checkpoint is terminal.
    commands.settle_attempt(
        fence,
        outcome=AttemptOutcome.SUCCEEDED,
        terminal_status=TaskStatus.SUCCEEDED,
    )
    empty_task = commands.create_run_and_initial_task(_request("run.recovery-empty", base))
    empty_recovery = RuntimeRecoveryService(
        factory,
        commands,
        RunCheckpointRepository(factory),
        artifacts,
        commits,
        cast(EffectStatusResolver, _Resolver(EffectStatus.COMPLETED)),
    )
    with pytest.raises(RuntimeCommandConflictError, match="no settled resumable"):
        empty_recovery.select_safe_checkpoint(empty_task.task_id)


def test_recovery_rejects_drifted_task_identity_and_stale_basis(
    edge_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, events, commands, base = edge_kernel
    task = commands.create_run_and_initial_task(_request("run.recovery-drift", base))
    _, fence = commands.claim(task.task_id, worker_id="worker.dead")
    commands.mark_started(fence)
    state = artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0"))
    position = events.replay(task.run_id)[-1].sequence_no
    safe = RunCheckpoint(
        checkpoint_id=StableId("checkpoint.recovery-drift.safe"),
        run_id=task.run_id,
        event_position=position,
        logical_stage="safe",
        state_artifact_ref=state,
        resumability_status=ResumabilityStatus.RESUMABLE,
    )
    commands.save_checkpoint(fence, safe)
    recovery = RuntimeRecoveryService(
        factory,
        commands,
        RunCheckpointRepository(factory),
        artifacts,
        commits,
        cast(EffectStatusResolver, _Resolver(EffectStatus.COMPLETED)),
    )
    assert recovery.select_safe_checkpoint(task.task_id) == safe

    # Basis is no longer current after a commit advances the project.
    basis_moved = commands.create_run_and_initial_task(
        CreativeRunRequest(
            run_id=RunId("run.recovery-basis"),
            project_id=ProjectId("project.test"),
            basis_commit=base,
            policy=_policy(),
        )
    )
    moved_recovery = RuntimeRecoveryService(
        factory,
        commands,
        RunCheckpointRepository(factory),
        artifacts,
        commits,
        cast(EffectStatusResolver, _Resolver(EffectStatus.COMPLETED)),
    )
    with pytest.raises(RuntimeCommandConflictError, match="no settled resumable"):
        moved_recovery.select_safe_checkpoint(basis_moved.task_id)


def test_recovery_resume_rejects_stale_checkpoint_and_old_attempt(
    edge_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, events, commands, base = edge_kernel
    task = commands.create_run_and_initial_task(_request("run.recovery-resume", base))
    _, fence = commands.claim(task.task_id, worker_id="worker.dead")
    commands.mark_started(fence)
    state = artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0"))
    position = events.replay(task.run_id)[-1].sequence_no
    safe = RunCheckpoint(
        checkpoint_id=StableId("checkpoint.recovery-resume.safe"),
        run_id=task.run_id,
        event_position=position,
        logical_stage="safe",
        state_artifact_ref=state,
        resumability_status=ResumabilityStatus.RESUMABLE,
    )
    commands.save_checkpoint(fence, safe)
    recovery = RuntimeRecoveryService(
        factory,
        commands,
        RunCheckpointRepository(factory),
        artifacts,
        commits,
        cast(EffectStatusResolver, _Resolver(EffectStatus.COMPLETED)),
    )
    with pytest.raises(RuntimeCommandConflictError, match="old attempt"):
        recovery.resume(task.task_id, worker_id="worker.fresh", actor_id="operator")
    commands.operator_reconcile_attempt(
        task.task_id,
        command_id=StableId("operator.reconcile.resume"),
        actor_id="operator",
        reason="worker dead",
    )
    checkpoint, attempt, resumed_fence = recovery.resume(
        task.task_id, worker_id="worker.fresh", actor_id="operator"
    )
    assert checkpoint == safe and attempt.attempt_no == 2
    assert resumed_fence.attempt_id == attempt.attempt_id


def test_recovery_resume_uses_paused_and_retry_and_ready_paths(
    edge_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, events, commands, base = edge_kernel

    def _make_run(run_id: str, *, paused: bool) -> tuple[TaskRecord, RuntimeRecoveryService]:
        task = commands.create_run_and_initial_task(_request(run_id, base))
        _, fence = commands.claim(task.task_id, worker_id="worker.dead")
        commands.mark_started(fence)
        state = artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0"))
        position = events.replay(task.run_id)[-1].sequence_no
        safe = RunCheckpoint(
            checkpoint_id=StableId(f"checkpoint.{run_id}.safe"),
            run_id=task.run_id,
            event_position=position,
            logical_stage="safe",
            state_artifact_ref=state,
            resumability_status=ResumabilityStatus.RESUMABLE,
        )
        commands.save_checkpoint(fence, safe)
        commands.operator_reconcile_attempt(
            task.task_id,
            command_id=StableId(f"operator.reconcile.{run_id}"),
            actor_id="operator",
            reason="worker dead",
            terminal_status=TaskStatus.CANCELLED if paused else TaskStatus.WAITING_RETRY,
        )
        return task, RuntimeRecoveryService(
            factory,
            commands,
            RunCheckpointRepository(factory),
            artifacts,
            commits,
            cast(EffectStatusResolver, _Resolver(EffectStatus.COMPLETED)),
        )

    paused_task, paused_recovery = _make_run("run.recovery-paused", paused=True)
    _, paused_attempt, _ = paused_recovery.resume(
        paused_task.task_id, worker_id="worker.paused", actor_id="operator"
    )
    assert paused_attempt.attempt_no == 2
    retry_task, retry_recovery = _make_run("run.recovery-retry", paused=False)
    _, retry_attempt, _ = retry_recovery.resume(
        retry_task.task_id, worker_id="worker.retry", actor_id="operator"
    )
    assert retry_attempt.attempt_no == 2
    # READY path: reconcile to WAITING_RETRY, then explicit control retry.
    ready_task, ready_recovery = _make_run("run.recovery-ready", paused=False)
    commands.control(
        ready_task.task_id,
        command_id=StableId("control.retry.ready"),
        action="retry",
        actor_id="operator",
        reason="retry from settled checkpoint",
    )
    assert commands.get_task(ready_task.task_id).status is TaskStatus.READY
    _, ready_attempt, _ = ready_recovery.resume(
        ready_task.task_id, worker_id="worker.ready", actor_id="operator"
    )
    assert ready_attempt.attempt_no == 2


def test_recovery_rejects_missing_task_in_rebuild_and_drifted_identity(
    edge_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, events, commands, base = edge_kernel
    task = commands.create_run_and_initial_task(_request("run.recovery-rebuild", base))
    _, fence = commands.claim(task.task_id, worker_id="worker.dead")
    commands.mark_started(fence)
    state = artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0"))
    # Checkpoint at a position that predates the task-created event is not
    # possible because save_checkpoint enforces the high watermark; instead use
    # a checkpoint whose rebuild does not contain this task by pointing at a
    # different run's stream? No: replay uses the task's own run. Instead force
    # the drift by rewriting the durable task projection row.
    position = events.replay(task.run_id)[-1].sequence_no
    safe = RunCheckpoint(
        checkpoint_id=StableId("checkpoint.recovery-rebuild.safe"),
        run_id=task.run_id,
        event_position=position,
        logical_stage="safe",
        state_artifact_ref=state,
        resumability_status=ResumabilityStatus.RESUMABLE,
    )
    commands.save_checkpoint(fence, safe)
    recovery = RuntimeRecoveryService(
        factory,
        commands,
        RunCheckpointRepository(factory),
        artifacts,
        commits,
        cast(EffectStatusResolver, _Resolver(EffectStatus.COMPLETED)),
    )
    assert recovery.select_safe_checkpoint(task.task_id) == safe
    # Advance the project commit behind the task basis, triggering the
    # "basis is no longer current" guard.
    advance = make_commit_request(base, project_id=task.project_id, root_offset=9)
    assert commits.commit(advance).status is CommitStatus.ACCEPTED
    with pytest.raises(RuntimeCommandConflictError, match="basis is no longer current"):
        recovery.select_safe_checkpoint(task.task_id)


def test_recovery_resume_rejects_ineligible_task_status(
    edge_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, events, commands, base = edge_kernel
    task = commands.create_run_and_initial_task(_request("run.recovery-ineligible", base))
    _, fence = commands.claim(task.task_id, worker_id="worker.dead")
    commands.mark_started(fence)
    state = artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0"))
    position = events.replay(task.run_id)[-1].sequence_no
    safe = RunCheckpoint(
        checkpoint_id=StableId("checkpoint.recovery-ineligible.safe"),
        run_id=task.run_id,
        event_position=position,
        logical_stage="safe",
        state_artifact_ref=state,
        resumability_status=ResumabilityStatus.RESUMABLE,
    )
    commands.save_checkpoint(fence, safe)
    # Settle into a non-resumable, non-READY terminal state.
    commands.settle_attempt(
        fence,
        outcome=AttemptOutcome.FAILED,
        terminal_status=TaskStatus.FAILED,
    )
    recovery = RuntimeRecoveryService(
        factory,
        commands,
        RunCheckpointRepository(factory),
        artifacts,
        commits,
        cast(EffectStatusResolver, _Resolver(EffectStatus.COMPLETED)),
    )
    with pytest.raises(RuntimeCommandConflictError, match="not eligible"):
        recovery.resume(task.task_id, worker_id="worker.fresh", actor_id="operator")


def test_projection_replays_pause_and_cancel_control_actions(
    edge_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    _, _, _, events, commands, base = edge_kernel
    task = commands.create_run_and_initial_task(_request("run.projection-control", base))
    _, fence = commands.claim(task.task_id, worker_id="worker")
    commands.mark_started(fence)
    # Pause while an attempt is active -> RECOVERY_PENDING-style cancel path.
    commands.control(
        task.task_id,
        command_id=StableId("control.pause.active"),
        action="pause",
        actor_id="operator",
        reason="pause with active attempt",
    )
    commands.control(
        task.task_id,
        command_id=StableId("control.cancel.active"),
        action="cancel",
        actor_id="operator",
        reason="cancel with active attempt",
    )
    commands.mark_recovery_pending(
        task.task_id,
        command_id=StableId("control.recovery-pending"),
        actor_id="reconciler",
        reason="effect unresolved",
    )
    rebuilt = project_runtime_events(events.replay(task.run_id))
    assert rebuilt.tasks[task.task_id.root].status is TaskStatus.RECOVERY_PENDING
    assert rebuilt.tasks[task.task_id.root].cancel_requested is True
    assert rebuilt.tasks[task.task_id.root].paused is True


def test_maintenance_counts_project_filter_and_uncertain_effects(
    edge_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, _, _, _, commands, base = edge_kernel
    task = commands.create_run_and_initial_task(_request("run.maintenance", base))
    _, fence = commands.claim(task.task_id, worker_id="worker")
    commands.mark_started(fence)
    commands.record_effect_requested(fence, _effect("effect.maintenance"))
    maintenance = RuntimeMaintenanceService(factory)
    uncertain = maintenance.precheck(
        MaintenanceCommand(
            command_id=StableId("maintenance.uncertain"),
            kind=MaintenanceKind.RECONCILE_UNCERTAIN_EFFECTS,
            requested_at=NOW,
        )
    )
    assert uncertain.item_count == 1
    other = maintenance.precheck(
        MaintenanceCommand(
            command_id=StableId("maintenance.other-project"),
            kind=MaintenanceKind.RECONCILE_PROJECTION_FRESHNESS,
            project_id=ProjectId("project.other"),
            requested_at=NOW,
        )
    )
    assert other.disposition is MaintenanceDisposition.NO_WORK
    global_count = maintenance.precheck(
        MaintenanceCommand(
            command_id=StableId("maintenance.global"),
            kind=MaintenanceKind.RECONCILE_PROJECTION_FRESHNESS,
            requested_at=NOW,
        )
    )
    assert global_count.item_count >= 1


def test_supervisor_finds_budget_exhausted_and_effect_unresolved(
    edge_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, _, _, _, commands, base = edge_kernel
    task = commands.create_run_and_initial_task(_request("run.supervisor-effect", base))
    _, fence = commands.claim(task.task_id, worker_id="worker")
    commands.mark_started(fence)
    commands.record_effect_requested(fence, _effect("effect.supervisor"))
    # Fresh effect task fires the effect finding.
    findings = RuntimeSupervisor(factory, stuck_after=timedelta(hours=1)).inspect()
    assert any(finding.code == "runtime_effect_unresolved" for finding in findings)
    # Make the same task stuck so a second pass dedupes the effect finding.
    with factory() as session, session.begin():
        row = session.get(RuntimeTaskProjectionRow, task.task_id.root)
        assert row is not None
        row.updated_at = NOW - timedelta(hours=2)
    deduped = RuntimeSupervisor(factory, stuck_after=timedelta(hours=1)).inspect()
    assert any(finding.code == "runtime_task_stuck" for finding in deduped)
    assert len([f for f in deduped if f.code == "runtime_effect_unresolved"]) == 0

    budget_task = commands.create_run_and_initial_task(_request("run.supervisor-budget", base))
    with factory() as session, session.begin():
        row = session.get(RuntimeTaskProjectionRow, budget_task.task_id.root)
        assert row is not None
        row.task_json = {
            **row.task_json,
            "failure_budget": 0,
        }
    budget_findings = RuntimeSupervisor(factory, stuck_after=timedelta(hours=1)).inspect()
    assert any(finding.code == "runtime_failure_budget_exhausted" for finding in budget_findings)


def test_acceptance_rejects_stale_commit_and_unpinned_auto_and_bad_lineage(
    edge_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    _, commits, artifacts, _, commands, base = edge_kernel
    task = commands.create_run_and_initial_task(_request("run.acceptance-edges", base))
    _, fence = commands.claim(task.task_id, worker_id="planner")
    commands.mark_started(fence)
    candidate_ref = artifacts.put(
        b'{"plan":"candidate"}',
        "application/vnd.novel-agent.stage5-plan-candidate+json",
        SchemaVersion("1.0.0"),
    )
    commands.settle_attempt(
        fence,
        outcome=AttemptOutcome.SUCCEEDED,
        terminal_status=TaskStatus.SUCCEEDED,
        artifact_refs=(candidate_ref,),
    )
    waiting = TaskRecord(
        task_id=TaskId("run.acceptance-edges.plan.accept"),
        run_id=task.run_id,
        project_id=task.project_id,
        kind=TaskKind.PLAN_ACCEPTANCE,
        task_revision=0,
        status=TaskStatus.WAITING_INPUT,
        basis_commit=base,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
        input_artifact_refs=(candidate_ref,),
        dependency_task_ids=(task.task_id,),
    )
    candidate = CandidateBinding(
        candidate_id=StableId("candidate.acceptance-edges"),
        kind=CandidateKind.PLAN,
        artifact_ref=candidate_ref,
        candidate_hash=candidate_ref.artifact_id.root,
        basis_commit=base,
    )
    waiting = waiting.model_copy(
        update={"candidate_binding_ref": _binding_ref(artifacts, candidate)}
    )
    commands.create_task(waiting)
    command = AcceptanceCommand(
        command_id=StableId("accept.edges.command"),
        project_id=task.project_id,
        run_id=task.run_id,
        task_id=waiting.task_id,
        candidate=candidate,
        acceptance_policy_hash=HASH,
        actor_kind=ActorKind.POLICY,
        actor_id="pinned-runtime-policy",
        decision=AcceptanceDecision.ACCEPT,
        reason="approved",
        expected_project_commit=base,
        idempotency_identity=StableId("accept.edges.identity"),
        issued_at=NOW,
    )
    manual = CreativeRunPolicy(
        automation_mode=AutomationMode.MANUAL,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
    )
    service = RuntimeAcceptanceService(commands, commits, artifacts)
    with pytest.raises(RuntimeCommandConflictError, match="not profile-pinned"):
        service.submit(command, policy=manual)
    auto = manual.model_copy(
        update={
            "automation_mode": AutomationMode.AUTO,
            "auto_accept_plan": True,
            "auto_accept_draft": True,
        }
    )
    with pytest.raises(RuntimeCommandConflictError, match="immutable candidate binding"):
        service.submit(
            command.model_copy(
                update={
                    "candidate": candidate.model_copy(
                        update={"candidate_id": StableId("candidate.acceptance-swapped")}
                    )
                }
            ),
            policy=auto,
        )
    receipt = service.submit(command, policy=auto)
    assert receipt.accepted_binding is not None
    # Rejected-candidate path covers the ACCEPT false branch on a fresh task.
    waiting_reject = waiting.model_copy(
        update={
            "task_id": TaskId("run.acceptance-edges.plan.accept.reject"),
        }
    )
    commands.create_task(waiting_reject)
    reject_command = command.model_copy(
        update={
            "command_id": StableId("accept.edges.reject"),
            "task_id": waiting_reject.task_id,
            "decision": AcceptanceDecision.REJECT,
            "idempotency_identity": StableId("accept.edges.reject.identity"),
        }
    )
    rejected = service.submit(reject_command, policy=auto)
    assert rejected.accepted_binding is None


def test_acceptance_rejects_stale_expected_commit_and_invalid_settled_lineage(
    edge_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    _, commits, artifacts, _, commands, base = edge_kernel
    task = commands.create_run_and_initial_task(_request("run.acceptance-stale", base))
    _, fence = commands.claim(task.task_id, worker_id="planner")
    commands.mark_started(fence)
    candidate_ref = artifacts.put(
        b'{"plan":"candidate"}',
        "application/vnd.novel-agent.stage5-plan-candidate+json",
        SchemaVersion("1.0.0"),
    )
    commands.settle_attempt(
        fence,
        outcome=AttemptOutcome.SUCCEEDED,
        terminal_status=TaskStatus.SUCCEEDED,
        artifact_refs=(candidate_ref,),
    )
    # Advance the project so the expected commit is stale.
    second = make_commit_request(base, project_id=task.project_id, root_offset=9)
    result = commits.commit(second)
    from novel_agent.domain.changes import CommitStatus

    assert result.status is CommitStatus.ACCEPTED
    waiting = TaskRecord(
        task_id=TaskId("run.acceptance-stale.plan.accept"),
        run_id=task.run_id,
        project_id=task.project_id,
        kind=TaskKind.PLAN_ACCEPTANCE,
        task_revision=0,
        status=TaskStatus.WAITING_INPUT,
        basis_commit=base,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
        input_artifact_refs=(candidate_ref,),
        dependency_task_ids=(task.task_id,),
    )
    stale_candidate = CandidateBinding(
        candidate_id=StableId("candidate.acceptance-stale"),
        kind=CandidateKind.PLAN,
        artifact_ref=candidate_ref,
        candidate_hash=candidate_ref.artifact_id.root,
        basis_commit=base,
    )
    waiting = waiting.model_copy(
        update={"candidate_binding_ref": _binding_ref(artifacts, stale_candidate)}
    )
    commands.create_task(waiting)
    stale_command = AcceptanceCommand(
        command_id=StableId("accept.stale.command"),
        project_id=task.project_id,
        run_id=task.run_id,
        task_id=waiting.task_id,
        candidate=stale_candidate,
        acceptance_policy_hash=HASH,
        actor_kind=ActorKind.AUTHOR,
        actor_id="author",
        decision=AcceptanceDecision.ACCEPT,
        reason="approved",
        expected_project_commit=base,
        idempotency_identity=StableId("accept.stale.identity"),
        issued_at=NOW,
    )
    service = RuntimeAcceptanceService(commands, commits, artifacts)
    with pytest.raises(RuntimeCommandConflictError, match="expected commit is stale"):
        service.submit(stale_command, policy=_policy())
    # Invalid settled lineage: terminal refs count is not exactly one.
    current_commit = result.commit_id
    assert current_commit is not None
    lineage_candidate = stale_candidate.model_copy(
        update={"basis_commit": current_commit, "candidate_hash": candidate_ref.artifact_id.root}
    )
    lineage_command = stale_command.model_copy(
        update={
            "task_id": TaskId("run.acceptance-stale.bad-lineage"),
            "candidate": lineage_candidate,
            "expected_project_commit": current_commit,
        }
    )
    bad_waiting = TaskRecord(
        task_id=TaskId("run.acceptance-stale.bad-lineage"),
        run_id=task.run_id,
        project_id=task.project_id,
        kind=TaskKind.PLAN_ACCEPTANCE,
        task_revision=0,
        status=TaskStatus.SUCCEEDED,
        basis_commit=current_commit,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
        input_artifact_refs=(candidate_ref,),
        terminal_artifact_refs=(),
        candidate_binding_ref=_binding_ref(artifacts, lineage_candidate),
        dependency_task_ids=(task.task_id,),
    )
    commands.create_task(bad_waiting)
    with pytest.raises(RuntimeCommandConflictError, match="invalid receipt lineage"):
        service.submit(lineage_command, policy=_policy())


def test_commit_accepted_candidate_handles_conflict_and_validation_rejection(
    edge_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, _, commands, base = edge_kernel
    from novel_agent.domain.changes import ValidationStatus

    def _commit_task(run_id: str, commit_id: CommitId) -> TaskRecord:
        return TaskRecord(
            task_id=TaskId(f"task.{run_id}"),
            run_id=RunId(run_id),
            project_id=ProjectId("project.test"),
            kind=TaskKind.PLAN_COMMIT,
            task_revision=0,
            status=TaskStatus.READY,
            basis_commit=commit_id,
            policy_hash=HASH,
            permission_hash=PERMISSION_HASH,
        )

    def _binding() -> AcceptedCandidateBinding:
        candidate_ref = artifacts.put(
            b'{"plan":"candidate"}',
            "application/vnd.novel-agent.stage5-plan-candidate+json",
            SchemaVersion("1.0.0"),
        )
        return AcceptedCandidateBinding(
            acceptance_id=StableId("acceptance.commit-branch"),
            command_id=StableId("command.commit-branch"),
            project_id=ProjectId("project.test"),
            run_id=RunId("run.commit-branch"),
            task_id=TaskId("task.commit-branch"),
            candidate=CandidateBinding(
                candidate_id=StableId("candidate.commit-branch"),
                kind=CandidateKind.PLAN,
                artifact_ref=candidate_ref,
                candidate_hash=candidate_ref.artifact_id.root,
                basis_commit=base,
            ),
            actor_kind=ActorKind.AUTHOR,
            actor_id="author",
            accepted_at=NOW,
            expected_project_commit=base,
        )

    def _commit_request(
        run_id: str,
        binding: AcceptedCandidateBinding,
        *,
        conflict: bool = False,
        rejected: bool = False,
    ) -> CommitRequest:
        bundle, report = StrictDeterministicCandidateMaterializer(
            commits, candidate_kind=CandidateKind.PLAN
        ).materialize(binding)
        if rejected:
            report = report.model_copy(update={"status": ValidationStatus.FAILED})
        return CommitRequest(
            request_id=StableId(f"request.{run_id}"),
            project_id=binding.project_id,
            base_commit=binding.expected_project_commit,
            idempotency_key=StableId(f"commit.{run_id}"),
            bundle=bundle,
            validation_report=report,
        )

    def _claim(run_id: str) -> tuple[TaskRecord, AttemptFence]:
        task = _commit_task(run_id, base)
        commands.create_task(task)
        _, fence = commands.claim(task.task_id, worker_id="commit-worker")
        commands.mark_started(fence)
        fence = commands.claim_writer_lane(fence)
        return task, fence

    # VALIDATION_REJECTED branch (953-957).
    task, fence = _claim("run.commit-rejected")
    rejected_result = commands.commit_accepted_candidate(
        fence, _commit_request("run.commit-rejected", _binding(), rejected=True), commits
    )
    assert rejected_result.status is CommitStatus.REJECTED
    assert commands.get_task(task.task_id).status is TaskStatus.BLOCKED

    # CONFLICTED branch: claim first, then advance the project behind the fence.
    task2, fence2 = _claim("run.commit-conflict")
    with factory() as session, session.begin():
        row = session.get(ProjectWriterClaimRow, task2.project_id.root)
        assert row is not None
        row.attempt_id = fence2.attempt_id.root
    advance = make_commit_request(base, project_id=ProjectId("project.test"), root_offset=9)
    assert commits.commit(advance).status is CommitStatus.ACCEPTED
    conflict_result = commands.commit_accepted_candidate(
        fence2, _commit_request("run.commit-conflict", _binding()), commits
    )
    assert conflict_result.status is CommitStatus.CONFLICTED
    assert commands.get_task(task2.task_id).status is TaskStatus.BLOCKED


def test_effect_recording_rejects_attempt_and_identity_mismatch(
    edge_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    _, _, _, _, commands, base = edge_kernel
    task = commands.create_run_and_initial_task(_request("run.effect-identity", base))
    _, fence = commands.claim(task.task_id, worker_id="worker")
    commands.mark_started(fence)
    with pytest.raises(RuntimeCommandConflictError, match="attempt number"):
        commands.record_effect_requested(fence, _effect("effect.mismatch-attempt", attempt_no=99))
    requested = _effect("effect.identity")
    commands.record_effect_requested(fence, requested)
    with pytest.raises(RuntimeCommandConflictError, match="differs from its request"):
        commands.record_effect_terminal(
            fence,
            requested.model_copy(
                update={"status": EffectStatus.COMPLETED, "request_identity": StableId("other")}
            ),
        )


def test_verify_writer_lane_passes_for_current_owner(
    edge_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    _, _, _, _, commands, base = edge_kernel
    task = TaskRecord(
        task_id=TaskId("task.verify-lane"),
        run_id=RunId("run.verify-lane"),
        project_id=ProjectId("project.test"),
        kind=TaskKind.PLAN_COMMIT,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=base,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
    )
    commands.create_task(task)
    _, fence = commands.claim(task.task_id, worker_id="commit-worker")
    commands.mark_started(fence)
    fence = commands.claim_writer_lane(fence)
    commands.verify_writer_lane(fence)


def test_complete_waiting_task_rejects_wrong_state_and_unbound_candidate(
    edge_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    _, _, artifacts, _, commands, base = edge_kernel
    task = commands.create_run_and_initial_task(_request("run.complete-waiting", base))
    _, fence = commands.claim(task.task_id, worker_id="planner")
    commands.mark_started(fence)
    candidate_ref = artifacts.put(
        b'{"plan":"candidate"}',
        "application/vnd.novel-agent.stage5-plan-candidate+json",
        SchemaVersion("1.0.0"),
    )
    commands.settle_attempt(
        fence,
        outcome=AttemptOutcome.SUCCEEDED,
        terminal_status=TaskStatus.SUCCEEDED,
        artifact_refs=(candidate_ref,),
    )
    waiting = TaskRecord(
        task_id=TaskId("run.complete-waiting.accept"),
        run_id=task.run_id,
        project_id=task.project_id,
        kind=TaskKind.PLAN_ACCEPTANCE,
        task_revision=0,
        status=TaskStatus.WAITING_INPUT,
        basis_commit=base,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
        input_artifact_refs=(candidate_ref,),
        dependency_task_ids=(task.task_id,),
    )
    commands.create_task(waiting)
    other_ref = artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0"))
    receipt = AcceptanceReceipt(
        receipt_id=StableId("receipt.complete-waiting"),
        command_id=StableId("command.complete-waiting"),
        idempotency_identity=StableId("idem.complete-waiting"),
        command_hash=HASH,
        decision=AcceptanceDecision.REJECT,
        candidate=CandidateBinding(
            candidate_id=StableId("candidate.unbound"),
            kind=CandidateKind.PLAN,
            artifact_ref=other_ref,
            candidate_hash=other_ref.artifact_id.root,
            basis_commit=base,
        ),
        accepted_binding=None,
        reason="rejected",
        recorded_at=NOW,
    )
    with pytest.raises(RuntimeCommandConflictError, match="not bound"):
        commands.complete_waiting_task(waiting.task_id, receipt=receipt, receipt_ref=candidate_ref)
    # Wrong task kind/status.
    non_acceptance = task.model_copy(
        update={
            "task_id": TaskId("run.complete-waiting.other"),
            "status": TaskStatus.READY,
        }
    )
    commands.create_task(non_acceptance)
    with pytest.raises(RuntimeCommandConflictError, match="waiting acceptance"):
        commands.complete_waiting_task(
            non_acceptance.task_id, receipt=receipt, receipt_ref=candidate_ref
        )
