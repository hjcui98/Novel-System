from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.models import (
    RuntimeTaskAttemptRow,
)
from novel_agent.adapters.postgres.runtime import RuntimeTaskQueryRepository
from novel_agent.adapters.runtime.isolated import StrictDeterministicCandidateMaterializer
from novel_agent.domain.changes import CommitRequest, CommitStatus
from novel_agent.domain.creative_runtime import (
    AcceptanceCommand,
    AcceptanceDecision,
    ActorKind,
    AutomationMode,
    CandidateBinding,
    CandidateKind,
    CreativeRunPolicy,
    CreativeRunRequest,
)
from novel_agent.domain.ids import (
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import DerivedBuildStatus, DerivedSnapshotLite
from novel_agent.domain.runtime import (
    AttemptOutcome,
    EffectReceipt,
    EffectStatus,
    FailureClass,
    ResumabilityStatus,
    RunCheckpoint,
    TaskKind,
    TaskRecord,
    TaskStatus,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository
from novel_agent.services.projection import (
    DerivedProjectionService,
    DerivedSnapshotRepository,
    ProjectionOutboxRepository,
)
from novel_agent.services.runtime_acceptance import RuntimeAcceptanceService
from novel_agent.services.runtime_commands import (
    RuntimeCommandConflictError,
    RuntimeCommandService,
    StaleAttemptFenceError,
)
from novel_agent.services.runtime_projection import (
    assert_task_projection_matches,
    project_runtime_events,
)
from tests.factories import make_commit_request, make_manifest

POLICY_HASH = "sha256:" + "1" * 64
PERMISSION_HASH = "sha256:" + "2" * 64
NOW = datetime(2026, 8, 10, tzinfo=UTC)


@pytest.fixture
def kernel(
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


def _policy(*, auto: bool = False) -> CreativeRunPolicy:
    return CreativeRunPolicy(
        automation_mode=AutomationMode.AUTO if auto else AutomationMode.MANUAL,
        policy_hash=POLICY_HASH,
        permission_hash=PERMISSION_HASH,
        auto_accept_plan=auto,
        auto_accept_draft=auto,
    )


def _request(run_id: str, base: CommitId) -> CreativeRunRequest:
    return CreativeRunRequest(
        run_id=RunId(run_id),
        project_id=ProjectId("project.test"),
        basis_commit=base,
        policy=_policy(),
        target_chapters=3,
    )


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def test_atomic_task_attempt_effect_checkpoint_and_full_replay(
    kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, _, artifacts, events, commands, base = kernel
    request = _request("run.atomic", base)
    task = commands.create_run_and_initial_task(request)
    assert commands.create_run_and_initial_task(request) == task
    attempt, fence = commands.claim(task.task_id, worker_id="worker.one")
    assert attempt.attempt_no == 1
    with pytest.raises(RuntimeCommandConflictError, match="status_not_claimable"):
        commands.claim(task.task_id, worker_id="worker.two")
    started = commands.mark_started(fence)
    assert started.started_at is not None
    assert commands.mark_started(fence) == started

    effect = EffectReceipt(
        effect_identity=StableId("effect.provider"),
        external_system="provider",
        request_identity=StableId("request.provider"),
        status=EffectStatus.REQUESTED,
        attempt_no=1,
    )
    assert commands.record_effect_requested(fence, effect) == effect
    assert commands.record_effect_requested(fence, effect) == effect
    with pytest.raises(RuntimeCommandConflictError, match="unresolved effect"):
        commands.settle_attempt(
            fence,
            outcome=AttemptOutcome.SUCCEEDED,
            terminal_status=TaskStatus.SUCCEEDED,
        )
    terminal_effect = effect.model_copy(
        update={"status": EffectStatus.COMPLETED, "completed_at": NOW}
    )
    commands.record_effect_terminal(fence, terminal_effect)

    state_ref = artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0"))
    position = events.replay(task.run_id)[-1].sequence_no
    checkpoint = RunCheckpoint(
        checkpoint_id=StableId("checkpoint.safe"),
        run_id=task.run_id,
        event_position=position,
        logical_stage="planner-candidate",
        state_artifact_ref=state_ref,
        completed_effect_ids=(effect.effect_identity,),
        resumability_status=ResumabilityStatus.RESUMABLE,
    )
    assert commands.save_checkpoint(fence, checkpoint) == checkpoint
    assert RunCheckpointRepository(factory).latest_resumable(task.run_id) == checkpoint
    settled = commands.settle_attempt(
        fence,
        outcome=AttemptOutcome.SUCCEEDED,
        terminal_status=TaskStatus.SUCCEEDED,
    )
    with pytest.raises(StaleAttemptFenceError):
        commands.mark_started(fence)
    replayed = events.replay(task.run_id)
    assert_task_projection_matches(replayed, (settled,))
    with pytest.raises(ValueError, match="contiguous"):
        project_runtime_events((replayed[0].model_copy(update={"sequence_no": 2}),))
    duplicate = replayed[0].model_copy(
        update={"sequence_no": len(replayed) + 1, "event_id": StableId("event.duplicate")}
    )
    with pytest.raises(ValueError, match="duplicate task"):
        project_runtime_events((*replayed, duplicate))
    unknown = replayed[1].model_copy(
        update={
            "sequence_no": len(replayed) + 1,
            "event_id": StableId("event.unknown-task"),
            "task_id": TaskId("task.unknown"),
        }
    )
    assert project_runtime_events((*replayed, unknown)).tasks[task.task_id.root] == settled
    with pytest.raises(RuntimeError, match="differs"):
        assert_task_projection_matches(replayed, ())


def test_acceptance_is_durable_idempotent_and_policy_pinned(
    kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, events, commands, base = kernel
    first = commands.create_run_and_initial_task(_request("run.accept", base))
    _, fence = commands.claim(first.task_id, worker_id="planner")
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
    candidate = CandidateBinding(
        candidate_id=StableId("candidate.accepted-plan"),
        kind=CandidateKind.PLAN,
        artifact_ref=candidate_ref,
        candidate_hash=candidate_ref.artifact_id.root,
        basis_commit=base,
    )
    binding_ref = artifacts.put(
        canonical_json_bytes(candidate.model_dump(mode="json")),
        "application/vnd.novel-agent.stage5-candidate-binding+json",
        SchemaVersion("1.0.0"),
    )
    waiting = TaskRecord(
        task_id=TaskId("run.accept.plan.accept"),
        run_id=first.run_id,
        project_id=first.project_id,
        kind=TaskKind.PLAN_ACCEPTANCE,
        task_revision=0,
        status=TaskStatus.WAITING_INPUT,
        basis_commit=base,
        policy_hash=POLICY_HASH,
        permission_hash=PERMISSION_HASH,
        input_artifact_refs=(candidate_ref,),
        candidate_binding_ref=binding_ref,
        dependency_task_ids=(first.task_id,),
    )
    commands.create_task(waiting)
    command = AcceptanceCommand(
        command_id=StableId("accept.command"),
        project_id=first.project_id,
        run_id=first.run_id,
        task_id=waiting.task_id,
        candidate=candidate,
        acceptance_policy_hash=POLICY_HASH,
        actor_kind=ActorKind.AUTHOR,
        actor_id="author",
        decision=AcceptanceDecision.ACCEPT,
        reason="approved",
        expected_project_commit=base,
        idempotency_identity=StableId("accept.identity"),
        issued_at=NOW,
    )
    service = RuntimeAcceptanceService(commands, commits, artifacts)
    with pytest.raises(RuntimeCommandConflictError, match="another acceptance task"):
        service.submit(command.model_copy(update={"run_id": RunId("run.other")}), policy=_policy())
    with pytest.raises(RuntimeCommandConflictError, match="project mismatch"):
        service.submit(
            command.model_copy(update={"project_id": ProjectId("project.other")}), policy=_policy()
        )
    with pytest.raises(RuntimeCommandConflictError, match="policy hash mismatch"):
        service.submit(
            command.model_copy(update={"acceptance_policy_hash": "sha256:" + "9" * 64}),
            policy=_policy(),
        )
    receipt = service.submit(command, policy=_policy())
    assert receipt.accepted_binding is not None
    assert service.submit(command, policy=_policy()) == receipt
    assert_task_projection_matches(
        events.replay(first.run_id), RuntimeTaskQueryRepository(factory).list_run(first.run_id)
    )
    with pytest.raises(RuntimeCommandConflictError, match="another payload"):
        service.submit(command.model_copy(update={"reason": "changed"}), policy=_policy())

    second = waiting.model_copy(
        update={
            "task_id": TaskId("run.accept.plan.accept.auto"),
            "status": TaskStatus.WAITING_INPUT,
            "terminal_artifact_refs": (),
        }
    )
    commands.create_task(second)
    with pytest.raises(RuntimeCommandConflictError, match="not profile-pinned"):
        service.submit(
            command.model_copy(
                update={
                    "command_id": StableId("accept.auto"),
                    "task_id": second.task_id,
                    "actor_kind": ActorKind.POLICY,
                }
            ),
            policy=_policy(),
        )
    wrong_status = second.model_copy(
        update={"task_id": TaskId("run.accept.plan.accept.ready"), "status": TaskStatus.READY}
    )
    commands.create_task(wrong_status)
    with pytest.raises(RuntimeCommandConflictError, match="not waiting"):
        service.submit(
            command.model_copy(
                update={
                    "command_id": StableId("accept.ready"),
                    "idempotency_identity": StableId("accept.ready.identity"),
                    "task_id": wrong_status.task_id,
                }
            ),
            policy=_policy(),
        )


class _ProjectionBuilder:
    def build(self, project_id: ProjectId, source_commit: CommitId) -> DerivedSnapshotLite:
        assert project_id == ProjectId("project.test")
        return DerivedSnapshotLite(
            snapshot_id=StableId("snapshot." + source_commit.root.removeprefix("sha256:")),
            source_commit=source_commit,
            anchor_build_id=StableId("anchor.stage5"),
            anchor_index_version="anchor-v1",
            grounded_index_version="grounded-v1",
            embedding_profile="offline-deterministic-v1",
            fusion_profile="rrf-v1",
            build_status=DerivedBuildStatus.EXACT,
            published_at=NOW,
        )


def test_single_writer_fences_commit_and_projection_is_layer_local(
    kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, events, commands, base = kernel
    candidate_ref = artifacts.put(
        b'{"plan":"candidate"}',
        "application/vnd.novel-agent.stage5-plan-candidate+json",
        SchemaVersion("1.0.0"),
    )
    candidate = CandidateBinding(
        candidate_id=StableId("candidate.plan-commit"),
        kind=CandidateKind.PLAN,
        artifact_ref=candidate_ref,
        candidate_hash=candidate_ref.artifact_id.root,
        basis_commit=base,
    )
    accepted = AcceptanceCommand(
        command_id=StableId("accept.for-commit"),
        project_id=ProjectId("project.test"),
        run_id=RunId("run.commit"),
        task_id=TaskId("task.accept.source"),
        candidate=candidate,
        acceptance_policy_hash=POLICY_HASH,
        actor_kind=ActorKind.AUTHOR,
        actor_id="author",
        decision=AcceptanceDecision.ACCEPT,
        reason="approved",
        expected_project_commit=base,
        idempotency_identity=StableId("accept.commit.identity"),
        issued_at=NOW,
    )
    # Build the binding through the same immutable command shape without giving
    # the materializer any acceptance or Commit authority.
    from novel_agent.domain.creative_runtime import AcceptedCandidateBinding

    binding = AcceptedCandidateBinding(
        acceptance_id=StableId("acceptance.for-commit"),
        command_id=accepted.command_id,
        project_id=accepted.project_id,
        run_id=accepted.run_id,
        task_id=accepted.task_id,
        candidate=candidate,
        actor_kind=ActorKind.AUTHOR,
        actor_id="author",
        accepted_at=NOW,
        expected_project_commit=base,
    )
    task = TaskRecord(
        task_id=TaskId("task.plan-commit"),
        run_id=binding.run_id,
        project_id=binding.project_id,
        kind=TaskKind.PLAN_COMMIT,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=base,
        policy_hash=POLICY_HASH,
        permission_hash=PERMISSION_HASH,
    )
    commands.create_task(task)
    _, fence = commands.claim(task.task_id, worker_id="commit-worker")
    commands.mark_started(fence)
    fence = commands.claim_writer_lane(fence)
    bundle, validation = StrictDeterministicCandidateMaterializer(
        commits, candidate_kind=CandidateKind.PLAN
    ).materialize(binding)
    request = CommitRequest(
        request_id=StableId("request.plan-commit"),
        project_id=task.project_id,
        base_commit=base,
        idempotency_key=StableId("commit.plan-commit"),
        bundle=bundle,
        validation_report=validation,
    )
    result = commands.commit_accepted_candidate(fence, request, commits)
    assert result.status is CommitStatus.ACCEPTED and result.commit_id is not None
    assert_task_projection_matches(
        events.replay(task.run_id), RuntimeTaskQueryRepository(factory).list_run(task.run_id)
    )
    with pytest.raises(StaleAttemptFenceError):
        commands.verify_writer_lane(fence)

    projection = DerivedProjectionService(ProjectionOutboxRepository(factory), _ProjectionBuilder())
    assert projection.process_all() == 2
    assert DerivedSnapshotRepository(factory).get_for_commit(result.commit_id) is not None


def test_control_unblock_cancel_and_operator_reconcile_are_audited(
    kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, _, artifacts, events, commands, base = kernel
    blocked = commands.create_run_and_initial_task(_request("run.control", base))
    _, fence = commands.claim(blocked.task_id, worker_id="worker")
    commands.mark_started(fence)
    blocked = commands.settle_attempt(
        fence,
        outcome=AttemptOutcome.FAILED,
        terminal_status=TaskStatus.BLOCKED,
        failure_class=FailureClass.BASIS_CHANGED,
    )
    evidence = artifacts.put(b"changed", "text/plain", SchemaVersion("1.0.0"))
    with pytest.raises(RuntimeCommandConflictError, match="stale"):
        commands.unblock(
            blocked.task_id,
            command_id=StableId("unblock.bad"),
            actor_id="operator",
            block_cause_fingerprint="sha256:" + "0" * 64,
            changed_evidence_refs=(evidence,),
        )
    ready = commands.unblock(
        blocked.task_id,
        command_id=StableId("unblock.good"),
        actor_id="operator",
        block_cause_fingerprint=_digest("basis_changed"),
        changed_evidence_refs=(evidence,),
    )
    assert ready.status is TaskStatus.READY and ready.block_cause is None
    _, new_fence = commands.claim(ready.task_id, worker_id="worker.new")
    cancelled = commands.control(
        ready.task_id,
        command_id=StableId("cancel.request"),
        action="cancel",
        actor_id="operator",
        reason="stop",
    )
    assert cancelled.status is TaskStatus.RECOVERY_PENDING and cancelled.cancel_requested
    with pytest.raises(RuntimeCommandConflictError, match="only settle CANCELLED"):
        commands.settle_attempt(
            new_fence,
            outcome=AttemptOutcome.SUCCEEDED,
            terminal_status=TaskStatus.SUCCEEDED,
        )
    reconciled = commands.operator_reconcile_attempt(
        ready.task_id,
        command_id=StableId("cancel.acknowledged"),
        actor_id="operator",
        reason="worker stopped",
        terminal_status=TaskStatus.CANCELLED,
    )
    assert reconciled.status is TaskStatus.CANCELLED
    assert_task_projection_matches(
        events.replay(blocked.run_id), RuntimeTaskQueryRepository(factory).list_run(blocked.run_id)
    )

    paused = commands.create_run_and_initial_task(_request("run.pause", base))
    paused = commands.control(
        paused.task_id,
        command_id=StableId("pause.command"),
        action="pause",
        actor_id="operator",
        reason="pause",
    )
    assert paused.status is TaskStatus.PENDING and paused.paused
    resumed = commands.resume(
        paused.task_id,
        command_id=StableId("resume.command"),
        actor_id="operator",
        reason="resume",
    )
    assert resumed.status is TaskStatus.READY and not resumed.paused
    assert_task_projection_matches(
        events.replay(paused.run_id), RuntimeTaskQueryRepository(factory).list_run(paused.run_id)
    )

    retrying = commands.create_run_and_initial_task(_request("run.retry", base))
    _, retry_fence = commands.claim(retrying.task_id, worker_id="worker.retry")
    retrying = commands.settle_attempt(
        retry_fence,
        outcome=AttemptOutcome.SUSPENDED,
        terminal_status=TaskStatus.WAITING_RETRY,
        failure_class=FailureClass.PROVIDER_TRANSIENT,
    )
    retrying = commands.control(
        retrying.task_id,
        command_id=StableId("retry.command"),
        action="retry",
        actor_id="operator",
        reason="retry",
    )
    assert retrying.status is TaskStatus.READY
    assert_task_projection_matches(
        events.replay(retrying.run_id),
        RuntimeTaskQueryRepository(factory).list_run(retrying.run_id),
    )


def test_retry_releases_settled_commit_writer_lane(
    kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, _, _, events, commands, base = kernel
    task = TaskRecord(
        task_id=TaskId("task.retry-commit-lane"),
        run_id=RunId("run.retry-commit-lane"),
        project_id=ProjectId("project.test"),
        kind=TaskKind.DRAFT_COMMIT,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=base,
        policy_hash=POLICY_HASH,
        permission_hash=PERMISSION_HASH,
    )
    commands.create_task(task)
    first_attempt, first_fence = commands.claim(task.task_id, worker_id="commit-worker.one")
    commands.mark_started(first_fence)
    first_fence = commands.claim_writer_lane(first_fence)
    waiting = commands.settle_attempt(
        first_fence,
        outcome=AttemptOutcome.SUSPENDED,
        terminal_status=TaskStatus.WAITING_RETRY,
        failure_class=FailureClass.PROVIDER_TRANSIENT,
    )
    assert waiting.writer_generation == first_fence.writer_generation

    ready = commands.control(
        task.task_id,
        command_id=StableId("retry.commit-lane"),
        action="retry",
        actor_id="operator",
        reason="retry settled provider failure",
    )
    assert ready.status is TaskStatus.READY
    assert ready.writer_generation == 0

    second_attempt, second_fence = commands.claim(task.task_id, worker_id="commit-worker.two")
    assert second_attempt.attempt_no == first_attempt.attempt_no + 1
    commands.mark_started(second_fence)
    second_fence = commands.claim_writer_lane(second_fence)
    assert second_fence.writer_generation == first_fence.writer_generation + 1
    commands.verify_writer_lane(second_fence)
    assert_task_projection_matches(
        events.replay(task.run_id), RuntimeTaskQueryRepository(factory).list_run(task.run_id)
    )


def test_claim_writer_lane_after_blocked_commit_without_retry(
    kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, _, artifacts, events, commands, base = kernel
    task = TaskRecord(
        task_id=TaskId("task.unblock-commit-lane"),
        run_id=RunId("run.unblock-commit-lane"),
        project_id=ProjectId("project.test"),
        kind=TaskKind.DRAFT_COMMIT,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=base,
        policy_hash=POLICY_HASH,
        permission_hash=PERMISSION_HASH,
    )
    commands.create_task(task)
    first_attempt, first_fence = commands.claim(task.task_id, worker_id="commit-worker.block")
    commands.mark_started(first_fence)
    first_fence = commands.claim_writer_lane(first_fence)
    blocked = commands.settle_attempt(
        first_fence,
        outcome=AttemptOutcome.FAILED,
        terminal_status=TaskStatus.BLOCKED,
        failure_class=FailureClass.LEAF_REVIEW_REQUIRED,
    )
    assert blocked.writer_generation == first_fence.writer_generation
    evidence = artifacts.put(b"changed", "text/plain", SchemaVersion("1.0.0"))
    ready = commands.unblock(
        blocked.task_id,
        command_id=StableId("unblock.commit-lane"),
        actor_id="operator",
        block_cause_fingerprint=_digest("leaf_review_required"),
        changed_evidence_refs=(evidence,),
    )
    assert ready.status is TaskStatus.READY
    assert ready.writer_generation == 0

    second_attempt, second_fence = commands.claim(ready.task_id, worker_id="commit-worker.retry")
    assert second_attempt.attempt_no == first_attempt.attempt_no + 1
    commands.mark_started(second_fence)
    second_fence = commands.claim_writer_lane(second_fence)
    assert second_fence.writer_generation == first_fence.writer_generation + 1
    commands.verify_writer_lane(second_fence)
    assert_task_projection_matches(
        events.replay(task.run_id), RuntimeTaskQueryRepository(factory).list_run(task.run_id)
    )


def test_writer_lane_rejects_a_second_active_project_writer(
    kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    _, _, _, _, commands, base = kernel

    def _write_task(task_id: str, run_id: str, kind: TaskKind) -> TaskRecord:
        return TaskRecord(
            task_id=TaskId(task_id),
            run_id=RunId(run_id),
            project_id=ProjectId("project.test"),
            kind=kind,
            task_revision=0,
            status=TaskStatus.READY,
            basis_commit=base,
            policy_hash=POLICY_HASH,
            permission_hash=PERMISSION_HASH,
        )

    first = _write_task("task.writer-lane.first", "run.writer-lane.first", TaskKind.PLAN_COMMIT)
    second = _write_task("task.writer-lane.second", "run.writer-lane.second", TaskKind.DRAFT_COMMIT)
    commands.create_task(first)
    commands.create_task(second)

    _, first_fence = commands.claim(first.task_id, worker_id="writer.one")
    commands.mark_started(first_fence)
    first_fence = commands.claim_writer_lane(first_fence)
    assert commands.claim_writer_lane(first_fence) == first_fence

    _, second_fence = commands.claim(second.task_id, worker_id="writer.two")
    commands.mark_started(second_fence)
    with pytest.raises(RuntimeCommandConflictError, match="already held"):
        commands.claim_writer_lane(second_fence)

    assert commands.get_task(first.task_id).status is TaskStatus.RUNNING
    assert commands.get_task(second.task_id).status is TaskStatus.RUNNING
    commands.verify_writer_lane(first_fence)


def test_runtime_commands_fail_closed_on_invalid_identity_and_ownership(
    kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RunEventLogRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, _, artifacts, events, commands, base = kernel
    request = _request("run.edges", base)
    task = commands.create_run_and_initial_task(request)
    with pytest.raises(RuntimeCommandConflictError, match="another request"):
        commands.create_run_and_initial_task(
            request.model_copy(
                update={
                    "policy": request.policy.model_copy(
                        update={"policy_hash": "sha256:" + "8" * 64}
                    )
                }
            )
        )
    with pytest.raises(RuntimeCommandConflictError, match="current project commit"):
        commands.create_run_and_initial_task(
            _request("run.bad-basis", CommitId("sha256:" + "8" * 64))
        )
    with pytest.raises(ValueError, match="worker id"):
        commands.claim(task.task_id, worker_id="")

    collision = task.model_copy(update={"status": TaskStatus.PENDING})
    with pytest.raises(RuntimeCommandConflictError, match="identity collision"):
        commands.create_task(collision)
    missing_dependency = task.model_copy(
        update={
            "task_id": TaskId("task.missing-dependency"),
            "dependency_task_ids": (TaskId("task.absent"),),
        }
    )
    with pytest.raises(RuntimeCommandConflictError, match="dependency"):
        commands.create_task(missing_dependency)

    foreign = task.model_copy(
        update={
            "task_id": TaskId("task.foreign-project"),
            "run_id": RunId("run.foreign-project"),
            "project_id": ProjectId("project.absent"),
        }
    )
    commands.create_task(foreign)
    with pytest.raises(RuntimeCommandConflictError, match="no current commit"):
        commands.claim(foreign.task_id, worker_id="worker")

    attempt, fence = commands.claim(task.task_id, worker_id="worker.edges")
    assert attempt.attempt_no == 1
    with pytest.raises(ValueError, match="terminal or explicit"):
        commands.settle_attempt(
            fence, outcome=AttemptOutcome.SUCCEEDED, terminal_status=TaskStatus.RUNNING
        )
    requested = EffectReceipt(
        effect_identity=StableId("effect.edges"),
        external_system="provider",
        request_identity=StableId("request.edges"),
        status=EffectStatus.REQUESTED,
        attempt_no=1,
    )
    with pytest.raises(ValueError, match="REQUESTED"):
        commands.record_effect_requested(
            fence, requested.model_copy(update={"status": EffectStatus.COMPLETED})
        )
    commands.record_effect_requested(fence, requested)
    with pytest.raises(RuntimeCommandConflictError, match="identity collision"):
        commands.record_effect_requested(
            fence, requested.model_copy(update={"external_system": "other"})
        )
    with pytest.raises(ValueError, match="terminal effect"):
        commands.record_effect_terminal(fence, requested)
    with pytest.raises(RuntimeCommandConflictError, match="not requested"):
        commands.record_effect_terminal(
            fence,
            requested.model_copy(
                update={
                    "effect_identity": StableId("effect.unknown"),
                    "status": EffectStatus.COMPLETED,
                }
            ),
        )
    with pytest.raises(ValueError, match="authoritative terminal"):
        commands.reconcile_effect(
            task.task_id, requested, command_id=StableId("reconcile.requested")
        )
    terminal = requested.model_copy(update={"status": EffectStatus.COMPLETED})
    with pytest.raises(RuntimeCommandConflictError, match="unknown effect"):
        commands.reconcile_effect(
            foreign.task_id, terminal, command_id=StableId("reconcile.foreign")
        )
    with pytest.raises(RuntimeCommandConflictError, match="request identity changed"):
        commands.reconcile_effect(
            task.task_id,
            terminal.model_copy(update={"request_identity": StableId("request.changed")}),
            command_id=StableId("reconcile.changed"),
        )
    with pytest.raises(RuntimeCommandConflictError, match="effect frontier"):
        commands.operator_reconcile_attempt(
            task.task_id,
            command_id=StableId("reconcile.active"),
            actor_id="operator",
            reason="active",
        )
    commands.reconcile_effect(task.task_id, terminal, command_id=StableId("reconcile.completed"))
    with pytest.raises(ValueError, match="only retry, block, or cancel"):
        commands.operator_reconcile_attempt(
            task.task_id,
            command_id=StableId("reconcile.invalid-status"),
            actor_id="operator",
            reason="invalid",
            terminal_status=TaskStatus.SUCCEEDED,
        )

    state = artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0"))
    position = events.replay(task.run_id)[-1].sequence_no
    wrong_run = RunCheckpoint(
        checkpoint_id=StableId("checkpoint.wrong-run"),
        run_id=RunId("run.other"),
        event_position=position,
        logical_stage="edge",
        state_artifact_ref=state,
        resumability_status=ResumabilityStatus.BLOCKED,
        reason="test",
    )
    with pytest.raises(RuntimeCommandConflictError, match="another run"):
        commands.save_checkpoint(fence, wrong_run)
    too_high = wrong_run.model_copy(
        update={
            "checkpoint_id": StableId("checkpoint.too-high"),
            "run_id": task.run_id,
            "event_position": position + 100,
        }
    )
    with pytest.raises(RuntimeCommandConflictError, match="high watermark"):
        commands.save_checkpoint(fence, too_high)
    checkpoint = too_high.model_copy(
        update={
            "checkpoint_id": StableId("checkpoint.edge"),
            "event_position": position,
        }
    )
    commands.save_checkpoint(fence, checkpoint)
    with pytest.raises(RuntimeCommandConflictError, match="identity collision"):
        commands.save_checkpoint(fence, checkpoint.model_copy(update={"logical_stage": "changed"}))

    with pytest.raises(ValueError, match="unsupported"):
        commands.control(
            task.task_id,
            command_id=StableId("control.invalid"),
            action="invalid",
            actor_id="operator",
            reason="invalid",
        )
    with pytest.raises(RuntimeCommandConflictError, match="replace an active"):
        commands.control(
            task.task_id,
            command_id=StableId("control.active-retry"),
            action="retry",
            actor_id="operator",
            reason="invalid",
        )
    with pytest.raises(RuntimeCommandConflictError, match="paused task"):
        commands.resume(
            task.task_id,
            command_id=StableId("resume.active"),
            actor_id="operator",
            reason="invalid",
        )
    with pytest.raises(ValueError, match="changed prerequisite"):
        commands.unblock(
            task.task_id,
            command_id=StableId("unblock.empty"),
            actor_id="operator",
            block_cause_fingerprint=POLICY_HASH,
            changed_evidence_refs=(),
        )
    with pytest.raises(RuntimeCommandConflictError, match="recorded block cause"):
        commands.unblock(
            task.task_id,
            command_id=StableId("unblock.running"),
            actor_id="operator",
            block_cause_fingerprint=POLICY_HASH,
            changed_evidence_refs=(state,),
        )
    with pytest.raises(RuntimeCommandConflictError, match="commit tasks"):
        commands.claim_writer_lane(fence)
    with pytest.raises(StaleAttemptFenceError, match="writer lane"):
        commands.verify_writer_lane(fence)
    with pytest.raises(RuntimeCommandConflictError, match="commit task"):
        commands.commit_accepted_candidate(
            fence,
            make_commit_request(task.basis_commit, project_id=task.project_id),
            CommitService(factory),
        )

    commands.operator_reconcile_attempt(
        task.task_id,
        command_id=StableId("reconcile.edge"),
        actor_id="operator",
        reason="done",
    )
    with pytest.raises(RuntimeCommandConflictError, match="no attempt"):
        commands.operator_reconcile_attempt(
            task.task_id,
            command_id=StableId("reconcile.none"),
            actor_id="operator",
            reason="none",
        )
    with pytest.raises(LookupError):
        commands.get_task(TaskId("task.does-not-exist"))

    missing_row = commands.create_run_and_initial_task(_request("run.missing-attempt-row", base))
    _, missing_fence = commands.claim(missing_row.task_id, worker_id="worker.missing")
    with factory() as session, session.begin():
        session.execute(
            delete(RuntimeTaskAttemptRow).where(
                RuntimeTaskAttemptRow.attempt_id == missing_fence.attempt_id.root
            )
        )
    with pytest.raises(RuntimeCommandConflictError, match="projection is missing"):
        commands.operator_reconcile_attempt(
            missing_row.task_id,
            command_id=StableId("reconcile.missing-row"),
            actor_id="operator",
            reason="missing",
        )
