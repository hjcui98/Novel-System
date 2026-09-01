"""Branch coverage for RuntimeCommandService lease, control, and successor guards."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.models import RuntimeTaskAttemptRow
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.creative_runtime import (
    AutomationMode,
    CreativeRunPolicy,
    CreativeRunRequest,
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
from novel_agent.domain.runtime import (
    AttemptOutcome,
    EffectReceipt,
    EffectStatus,
    FailureClass,
    ResumabilityStatus,
    RunCheckpoint,
    TaskAttempt,
    TaskKind,
    TaskRecord,
    TaskStatus,
)
from novel_agent.services.commits import CommitService
from novel_agent.services.event_log import RunEventLogRepository
from novel_agent.services.runtime_commands import (
    RuntimeCommandConflictError,
    RuntimeCommandService,
    StaleAttemptFenceError,
)
from tests.factories import make_manifest

HASH = "sha256:" + "1" * 64
PERMISSION_HASH = "sha256:" + "2" * 64


@pytest.fixture
def kernel(
    tmp_path: Path,
) -> Iterator[tuple[sessionmaker[Session], RuntimeCommandService, CommitId]]:
    del tmp_path
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    commits = CommitService(factory)
    base = commits.initialize_project(make_manifest())
    commands = RuntimeCommandService(
        factory, RunEventLogRepository(factory), lambda _project_id: PERMISSION_HASH
    )
    yield factory, commands, base
    engine.dispose()


def _request(run_id: str, base: CommitId) -> CreativeRunRequest:
    return CreativeRunRequest(
        run_id=RunId(run_id),
        project_id=ProjectId("project.test"),
        basis_commit=base,
        policy=CreativeRunPolicy(
            automation_mode=AutomationMode.MANUAL,
            policy_hash=HASH,
            permission_hash=PERMISSION_HASH,
        ),
    )


def _effect(identity: str, *, attempt_no: int = 1) -> EffectReceipt:
    return EffectReceipt(
        effect_identity=StableId(identity),
        external_system="provider",
        request_identity=StableId(f"request.{identity}"),
        status=EffectStatus.REQUESTED,
        attempt_no=attempt_no,
    )


def _artifact_ref(digit: str = "a") -> ArtifactRef:
    return ArtifactRef(
        artifact_id=ArtifactId("sha256:" + digit * 64),
        media_type="application/json",
        byte_length=1,
        schema_version=SchemaVersion("1.0.0"),
    )


def _acceptance(predecessor: TaskRecord, **updates: object) -> TaskRecord:
    values: dict[str, object] = {
        "task_id": TaskId(f"{predecessor.task_id.root}.accept"),
        "run_id": predecessor.run_id,
        "project_id": predecessor.project_id,
        "kind": TaskKind.PLAN_ACCEPTANCE,
        "task_revision": 0,
        "status": TaskStatus.WAITING_INPUT,
        "basis_commit": predecessor.basis_commit,
        "policy_hash": HASH,
        "permission_hash": PERMISSION_HASH,
        "dependency_task_ids": (predecessor.task_id,),
    }
    values.update(updates)
    return TaskRecord.model_validate(values)


def test_attempt_lease_must_be_at_least_three_seconds(
    kernel: tuple[sessionmaker[Session], RuntimeCommandService, CommitId],
) -> None:
    factory, _, _ = kernel
    with pytest.raises(ValueError, match="at least three seconds"):
        RuntimeCommandService(
            factory,
            RunEventLogRepository(factory),
            lambda _project_id: PERMISSION_HASH,
            attempt_lease_seconds=2,
        )


def test_supersede_rejects_empty_reason_running_work_and_is_idempotent(
    kernel: tuple[sessionmaker[Session], RuntimeCommandService, CommitId],
) -> None:
    _, commands, base = kernel
    task = commands.create_run_and_initial_task(_request("run.supersede", base))
    with pytest.raises(ValueError, match="non-empty and bounded"):
        commands.supersede_task(task.task_id, reason="")
    cancelled = commands.supersede_task(task.task_id, reason="drop unused plan")
    assert cancelled.superseded is True
    assert cancelled.status is TaskStatus.CANCELLED
    assert commands.supersede_task(task.task_id, reason="repeat") == cancelled

    running = commands.create_run_and_initial_task(_request("run.supersede-running", base))
    commands.claim(running.task_id, worker_id="worker")
    with pytest.raises(RuntimeCommandConflictError, match="inactive work"):
        commands.supersede_task(running.task_id, reason="too late")


def test_heartbeat_rejects_a_settled_attempt(
    kernel: tuple[sessionmaker[Session], RuntimeCommandService, CommitId],
) -> None:
    factory, commands, base = kernel
    task = commands.create_run_and_initial_task(_request("run.heartbeat-settled", base))
    _, fence = commands.claim(task.task_id, worker_id="worker")
    commands.mark_started(fence)
    with factory() as session, session.begin():
        row = session.get(RuntimeTaskAttemptRow, fence.attempt_id.root)
        assert row is not None
        attempt = TaskAttempt.model_validate_json(json.dumps(row.attempt_json))
        ended = attempt.model_copy(
            update={"ended_at": datetime.now(UTC), "outcome": AttemptOutcome.SUCCEEDED}
        )
        row.attempt_json = json.loads(ended.model_dump_json())
    with pytest.raises(StaleAttemptFenceError, match="settled Attempt"):
        commands.heartbeat(fence)


def test_heartbeat_renews_a_live_attempt(
    kernel: tuple[sessionmaker[Session], RuntimeCommandService, CommitId],
) -> None:
    _, commands, base = kernel
    task = commands.create_run_and_initial_task(_request("run.heartbeat-live", base))
    _, fence = commands.claim(task.task_id, worker_id="worker")
    commands.mark_started(fence)
    renewed = commands.heartbeat(fence)
    assert renewed.ended_at is None
    assert renewed.lease_expires_at is not None
    assert renewed.heartbeat_at is not None


def test_suspect_and_reclaim_expired_attempt_fail_closed(
    kernel: tuple[sessionmaker[Session], RuntimeCommandService, CommitId],
) -> None:
    factory, commands, base = kernel
    ready = commands.create_run_and_initial_task(_request("run.suspect-ready", base))
    with pytest.raises(RuntimeCommandConflictError, match="running Attempt"):
        commands.suspect_expired_attempt(
            ready.task_id,
            command_id=StableId("command.suspect-ready"),
            actor_id="supervisor",
            reason="not running",
        )

    task = commands.create_run_and_initial_task(_request("run.suspect-live", base))
    _, fence = commands.claim(task.task_id, worker_id="worker")
    commands.mark_started(fence)
    with pytest.raises(RuntimeCommandConflictError, match="has not expired"):
        commands.suspect_expired_attempt(
            task.task_id,
            command_id=StableId("command.suspect-live"),
            actor_id="supervisor",
            reason="still leased",
        )

    missing = commands.create_run_and_initial_task(_request("run.suspect-missing", base))
    _, missing_fence = commands.claim(missing.task_id, worker_id="worker.missing")
    commands.mark_started(missing_fence)
    with factory() as session, session.begin():
        session.execute(
            delete(RuntimeTaskAttemptRow).where(
                RuntimeTaskAttemptRow.attempt_id == missing_fence.attempt_id.root
            )
        )
    with pytest.raises(RuntimeCommandConflictError, match="projection is missing"):
        commands.suspect_expired_attempt(
            missing.task_id,
            command_id=StableId("command.suspect-missing"),
            actor_id="supervisor",
            reason="row gone",
            now=datetime.now(UTC) + timedelta(minutes=10),
        )

    reclaim_ready = commands.create_run_and_initial_task(_request("run.reclaim-ready", base))
    with pytest.raises(RuntimeCommandConflictError, match="RECOVERY_PENDING"):
        commands.reclaim_expired_attempt(
            reclaim_ready.task_id,
            command_id=StableId("command.reclaim-ready"),
            actor_id="supervisor",
            reason="not suspected",
        )

    live = commands.create_run_and_initial_task(_request("run.reclaim-live", base))
    _, live_fence = commands.claim(live.task_id, worker_id="worker.live")
    commands.mark_started(live_fence)
    expired_at = datetime.now(UTC) + timedelta(minutes=10)
    suspected = commands.suspect_expired_attempt(
        live.task_id,
        command_id=StableId("command.suspect-once"),
        actor_id="supervisor",
        reason="lease expired",
        now=expired_at,
    )
    again = commands.suspect_expired_attempt(
        live.task_id,
        command_id=StableId("command.suspect-again"),
        actor_id="supervisor",
        reason="already pending",
        now=expired_at,
    )
    assert again.status is TaskStatus.RECOVERY_PENDING
    assert again.task_revision == suspected.task_revision
    with pytest.raises(RuntimeCommandConflictError, match="no longer expired"):
        commands.reclaim_expired_attempt(
            live.task_id,
            command_id=StableId("command.reclaim-early"),
            actor_id="supervisor",
            reason="clock went backwards",
            now=datetime.now(UTC),
        )

    gone = commands.create_run_and_initial_task(_request("run.reclaim-missing", base))
    _, gone_fence = commands.claim(gone.task_id, worker_id="worker.gone")
    commands.mark_started(gone_fence)
    commands.suspect_expired_attempt(
        gone.task_id,
        command_id=StableId("command.suspect-gone"),
        actor_id="supervisor",
        reason="lease expired",
        now=expired_at,
    )
    with factory() as session, session.begin():
        session.execute(
            delete(RuntimeTaskAttemptRow).where(
                RuntimeTaskAttemptRow.attempt_id == gone_fence.attempt_id.root
            )
        )
    with pytest.raises(RuntimeCommandConflictError, match="projection is missing"):
        commands.reclaim_expired_attempt(
            gone.task_id,
            command_id=StableId("command.reclaim-missing"),
            actor_id="supervisor",
            reason="row gone",
            now=expired_at,
        )


def test_effect_for_current_attempt_filters_owner_and_system(
    kernel: tuple[sessionmaker[Session], RuntimeCommandService, CommitId],
) -> None:
    _, commands, base = kernel
    task = commands.create_run_and_initial_task(_request("run.effect-lookup", base))
    assert commands.effect_for_current_attempt(task.task_id, external_system="provider") is None
    claimed, fence = commands.claim(task.task_id, worker_id="worker")
    commands.mark_started(fence)
    assert commands.effect_for_current_attempt(task.task_id, external_system="provider") is None
    receipt = commands.record_effect_requested(
        fence, _effect("effect.lookup", attempt_no=claimed.attempt_no)
    )
    assert commands.effect_for_current_attempt(task.task_id, external_system="other") is None
    assert commands.effect_for_current_attempt(task.task_id, external_system="provider") == receipt


def test_operator_reconcile_blocks_and_rejects_illegal_terminals(
    kernel: tuple[sessionmaker[Session], RuntimeCommandService, CommitId],
) -> None:
    _, commands, base = kernel
    task = commands.create_run_and_initial_task(_request("run.operator-block", base))
    with pytest.raises(ValueError, match="retry, block, or cancel"):
        commands.operator_reconcile_attempt(
            task.task_id,
            command_id=StableId("command.illegal-terminal"),
            actor_id="operator",
            reason="illegal",
            terminal_status=TaskStatus.SUCCEEDED,
        )
    commands.claim(task.task_id, worker_id="worker")
    blocked = commands.operator_reconcile_attempt(
        task.task_id,
        command_id=StableId("command.operator-block"),
        actor_id="operator",
        reason="operator blocked the attempt",
        terminal_status=TaskStatus.BLOCKED,
        failure_class=FailureClass.LEAF_REVIEW_REQUIRED,
        artifact_refs=(_artifact_ref("b"),),
    )
    assert blocked.status is TaskStatus.BLOCKED
    assert blocked.block_cause == "operator blocked the attempt"
    assert blocked.terminal_artifact_refs == (_artifact_ref("b"),)


def test_retry_with_exhausted_budget_enters_budget_review(
    kernel: tuple[sessionmaker[Session], RuntimeCommandService, CommitId],
) -> None:
    _, commands, base = kernel
    task = commands.create_task(
        TaskRecord(
            task_id=TaskId("task.retry-exhausted"),
            run_id=RunId("run.retry-exhausted"),
            project_id=ProjectId("project.test"),
            kind=TaskKind.PLAN_CANDIDATE,
            task_revision=0,
            status=TaskStatus.WAITING_RETRY,
            basis_commit=base,
            policy_hash=HASH,
            permission_hash=PERMISSION_HASH,
            failure_budget=0,
        )
    )
    reviewed = commands.control(
        task.task_id,
        command_id=StableId("control.retry-exhausted"),
        action="retry",
        actor_id="operator",
        reason="retry with no remaining budget",
    )
    assert reviewed.status is TaskStatus.BUDGET_REVIEW


def test_create_task_accepts_max_length_task_identity(
    kernel: tuple[sessionmaker[Session], RuntimeCommandService, CommitId],
) -> None:
    factory, commands, base = kernel
    task = TaskRecord(
        task_id=TaskId("t" * 128),
        run_id=RunId("run.max-task-id"),
        project_id=ProjectId("project.test"),
        kind=TaskKind.PLAN_CANDIDATE,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=base,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
    )

    assert commands.create_task(task) == task
    assert commands.create_task(task) == task
    event = RunEventLogRepository(factory).replay(task.run_id)[0]
    assert event.idempotency_identity.root == task.task_id.root


def test_supersede_max_length_task_uses_run_scoped_event_identity(
    kernel: tuple[sessionmaker[Session], RuntimeCommandService, CommitId],
) -> None:
    _, commands, base = kernel
    task = TaskRecord(
        task_id=TaskId("s" * 128),
        run_id=RunId("run.max-supersede"),
        project_id=ProjectId("project.test"),
        kind=TaskKind.PLAN_CANDIDATE,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=base,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
    )
    commands.create_task(task)

    superseded = commands.supersede_task(task.task_id, reason="operator stop")

    assert superseded.superseded
    events = RunEventLogRepository(kernel[0]).replay(task.run_id)
    assert len(events) == 2
    assert events[0].idempotency_identity != events[1].idempotency_identity
    assert all(len(event.idempotency_identity.root) <= 128 for event in events)


def test_runtime_commands_bound_effect_and_checkpoint_event_identities(
    kernel: tuple[sessionmaker[Session], RuntimeCommandService, CommitId],
) -> None:
    factory, commands, base = kernel
    task = commands.create_run_and_initial_task(_request("run.max-derived", base))
    _, fence = commands.claim(task.task_id, worker_id="worker")
    commands.mark_started(fence)

    effect = EffectReceipt(
        effect_identity=StableId("e" * 128),
        external_system="provider",
        request_identity=StableId("request.max-derived"),
        status=EffectStatus.REQUESTED,
        attempt_no=1,
    )
    commands.record_effect_requested(fence, effect)
    commands.record_effect_terminal(
        fence,
        effect.model_copy(update={"status": EffectStatus.COMPLETED}),
    )

    event_position = RunEventLogRepository(factory).replay(task.run_id)[-1].sequence_no
    checkpoint = RunCheckpoint(
        checkpoint_id=StableId("c" * 128),
        run_id=task.run_id,
        event_position=event_position,
        logical_stage="runtime-test",
        state_artifact_ref=_artifact_ref("c"),
        resumability_status=ResumabilityStatus.TERMINAL,
    )
    commands.save_checkpoint(fence, checkpoint)

    identities = RunEventLogRepository(factory).replay(task.run_id)
    assert len(identities) == 6
    assert all(len(event.idempotency_identity.root) <= 128 for event in identities)


def test_operator_reconcile_bounds_derived_identities_for_max_command_id(
    kernel: tuple[sessionmaker[Session], RuntimeCommandService, CommitId],
) -> None:
    factory, commands, base = kernel
    task = commands.create_run_and_initial_task(_request("run.max-command", base))
    commands.claim(task.task_id, worker_id="worker")

    commands.operator_reconcile_attempt(
        task.task_id,
        command_id=StableId("o" * 128),
        actor_id="operator",
        reason="operator stop",
        terminal_status=TaskStatus.BLOCKED,
        failure_class=FailureClass.VALIDATION_REJECTED,
    )

    events = RunEventLogRepository(factory).replay(task.run_id)
    assert all(len(event.idempotency_identity.root) <= 128 for event in events)


def test_runtime_trace_falls_back_to_existing_max_run_identity(
    kernel: tuple[sessionmaker[Session], RuntimeCommandService, CommitId],
) -> None:
    factory, commands, base = kernel
    task = TaskRecord(
        task_id=TaskId("task.max-run"),
        run_id=RunId("r" * 128),
        project_id=ProjectId("project.test"),
        kind=TaskKind.PLAN_CANDIDATE,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=base,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
    )

    commands.create_task(task)

    event = RunEventLogRepository(factory).replay(task.run_id)[0]
    assert event.trace_id == task.run_id.root
    assert len(event.trace_id) <= 128


def test_create_run_accepts_max_length_run_identity(
    kernel: tuple[sessionmaker[Session], RuntimeCommandService, CommitId],
) -> None:
    factory, commands, base = kernel
    run_id = "r" * 128

    task = commands.create_run_and_initial_task(_request(run_id, base))

    assert task.task_id.root == run_id
    assert commands.create_run_and_initial_task(_request(run_id, base)) == task
    event = RunEventLogRepository(factory).replay(task.run_id)[0]
    assert event.trace_id == run_id
    assert len(event.idempotency_identity.root) <= 128


def test_extend_budget_rejects_invalid_args_and_non_review_status(
    kernel: tuple[sessionmaker[Session], RuntimeCommandService, CommitId],
) -> None:
    _, commands, base = kernel
    task = commands.create_run_and_initial_task(_request("run.extend-invalid", base))
    with pytest.raises(ValueError, match="retry or Planner Memory"):
        commands.extend_budget(
            task.task_id,
            command_id=StableId("extend.zero"),
            actor_id="operator",
            reason="nothing added",
        )
    with pytest.raises(ValueError, match="retry or Planner Memory"):
        commands.extend_budget(
            task.task_id,
            command_id=StableId("extend.bool"),
            actor_id="operator",
            reason="bool is not a count",
            additional_attempts=True,
        )
    with pytest.raises(RuntimeCommandConflictError, match="inactive BUDGET_REVIEW"):
        commands.extend_budget(
            task.task_id,
            command_id=StableId("extend.ready"),
            actor_id="operator",
            reason="not waiting for budget",
            additional_attempts=1,
        )


def test_successor_insert_enforces_fixed_topology(
    kernel: tuple[sessionmaker[Session], RuntimeCommandService, CommitId],
) -> None:
    _, commands, base = kernel

    def _settle(run_id: str, successor: TaskRecord) -> None:
        task = commands.create_run_and_initial_task(_request(run_id, base))
        _, fence = commands.claim(task.task_id, worker_id="worker")
        commands.mark_started(fence)
        commands.settle_attempt(
            fence,
            outcome=AttemptOutcome.SUCCEEDED,
            terminal_status=TaskStatus.SUCCEEDED,
            successor_tasks=(successor,),
        )

    waiting = commands.create_run_and_initial_task(_request("run.succ-waiting", base))
    _, waiting_fence = commands.claim(waiting.task_id, worker_id="worker")
    commands.mark_started(waiting_fence)
    with pytest.raises(RuntimeCommandConflictError, match="succeeded task"):
        commands.settle_attempt(
            waiting_fence,
            outcome=AttemptOutcome.SUSPENDED,
            terminal_status=TaskStatus.WAITING_RETRY,
            successor_tasks=(_acceptance(waiting),),
        )

    duplicate = commands.create_run_and_initial_task(_request("run.succ-dup", base))
    _, dup_fence = commands.claim(duplicate.task_id, worker_id="worker")
    commands.mark_started(dup_fence)
    successor = _acceptance(duplicate)
    with pytest.raises(RuntimeCommandConflictError, match="duplicated"):
        commands.settle_attempt(
            dup_fence,
            outcome=AttemptOutcome.SUCCEEDED,
            terminal_status=TaskStatus.SUCCEEDED,
            successor_tasks=(successor, successor),
        )

    with pytest.raises(RuntimeCommandConflictError, match="fixed runtime topology"):
        _settle(
            "run.succ-kind",
            _acceptance(
                commands.create_run_and_initial_task(_request("run.succ-kind", base)),
                kind=TaskKind.DRAFT_ACCEPTANCE,
            ),
        )

    owner = commands.create_run_and_initial_task(_request("run.succ-owner", base))
    _, owner_fence = commands.claim(owner.task_id, worker_id="worker")
    commands.mark_started(owner_fence)
    with pytest.raises(RuntimeCommandConflictError, match="runtime owner"):
        commands.settle_attempt(
            owner_fence,
            outcome=AttemptOutcome.SUCCEEDED,
            terminal_status=TaskStatus.SUCCEEDED,
            successor_tasks=(_acceptance(owner, run_id=RunId("run.other")),),
        )

    basis = commands.create_run_and_initial_task(_request("run.succ-basis", base))
    _, basis_fence = commands.claim(basis.task_id, worker_id="worker")
    commands.mark_started(basis_fence)
    with pytest.raises(RuntimeCommandConflictError, match="settled basis"):
        commands.settle_attempt(
            basis_fence,
            outcome=AttemptOutcome.SUCCEEDED,
            terminal_status=TaskStatus.SUCCEEDED,
            successor_tasks=(_acceptance(basis, basis_commit=CommitId("sha256:" + "b" * 64)),),
        )

    dep = commands.create_run_and_initial_task(_request("run.succ-dep", base))
    _, dep_fence = commands.claim(dep.task_id, worker_id="worker")
    commands.mark_started(dep_fence)
    with pytest.raises(RuntimeCommandConflictError, match="does not depend"):
        commands.settle_attempt(
            dep_fence,
            outcome=AttemptOutcome.SUCCEEDED,
            terminal_status=TaskStatus.SUCCEEDED,
            successor_tasks=(_acceptance(dep, dependency_task_ids=()),),
        )

    rev = commands.create_run_and_initial_task(_request("run.succ-rev", base))
    _, rev_fence = commands.claim(rev.task_id, worker_id="worker")
    commands.mark_started(rev_fence)
    with pytest.raises(RuntimeCommandConflictError, match="unclaimed initial"):
        commands.settle_attempt(
            rev_fence,
            outcome=AttemptOutcome.SUCCEEDED,
            terminal_status=TaskStatus.SUCCEEDED,
            successor_tasks=(_acceptance(rev, task_revision=1),),
        )

    status = commands.create_run_and_initial_task(_request("run.succ-status", base))
    _, status_fence = commands.claim(status.task_id, worker_id="worker")
    commands.mark_started(status_fence)
    with pytest.raises(RuntimeCommandConflictError, match="invalid initial status"):
        commands.settle_attempt(
            status_fence,
            outcome=AttemptOutcome.SUCCEEDED,
            terminal_status=TaskStatus.SUCCEEDED,
            successor_tasks=(_acceptance(status, status=TaskStatus.SUCCEEDED),),
        )

    missing = commands.create_run_and_initial_task(_request("run.succ-missing", base))
    _, missing_fence = commands.claim(missing.task_id, worker_id="worker")
    commands.mark_started(missing_fence)
    with pytest.raises(RuntimeCommandConflictError, match="dependency does not exist"):
        commands.settle_attempt(
            missing_fence,
            outcome=AttemptOutcome.SUCCEEDED,
            terminal_status=TaskStatus.SUCCEEDED,
            successor_tasks=(
                _acceptance(
                    missing,
                    dependency_task_ids=(missing.task_id, TaskId("task.absent")),
                ),
            ),
        )

    collide = commands.create_run_and_initial_task(_request("run.succ-collide", base))
    prior = _acceptance(collide)
    commands.create_task(prior.model_copy(update={"priority": 7}))
    _, collide_fence = commands.claim(collide.task_id, worker_id="worker")
    commands.mark_started(collide_fence)
    with pytest.raises(RuntimeCommandConflictError, match="identity collision"):
        commands.settle_attempt(
            collide_fence,
            outcome=AttemptOutcome.SUCCEEDED,
            terminal_status=TaskStatus.SUCCEEDED,
            successor_tasks=(prior,),
        )


def test_successor_insert_is_idempotent_for_an_identical_task(
    kernel: tuple[sessionmaker[Session], RuntimeCommandService, CommitId],
) -> None:
    _, commands, base = kernel
    task = commands.create_run_and_initial_task(_request("run.succ-idempotent", base))
    prior = _acceptance(task)
    commands.create_task(prior)
    _, fence = commands.claim(task.task_id, worker_id="worker")
    commands.mark_started(fence)
    settled = commands.settle_attempt(
        fence,
        outcome=AttemptOutcome.SUCCEEDED,
        terminal_status=TaskStatus.SUCCEEDED,
        successor_tasks=(prior,),
    )
    assert settled.status is TaskStatus.SUCCEEDED
    assert commands.get_task(prior.task_id) == prior


def test_unknown_failure_settlement_fails_closed_to_recovery_pending(
    kernel: tuple[sessionmaker[Session], RuntimeCommandService, CommitId],
) -> None:
    factory, commands, base = kernel
    task = commands.create_run_and_initial_task(_request("run.unknown-failure", base))
    attempt, fence = commands.claim(task.task_id, worker_id="worker")
    commands.mark_started(fence)

    settled = commands.settle_attempt(
        fence,
        outcome=AttemptOutcome.FAILED,
        terminal_status=TaskStatus.WAITING_RETRY,
        failure_class="unseen-runtime-error",
    )

    assert settled.status is TaskStatus.RECOVERY_PENDING
    with factory() as session:
        row = session.get(RuntimeTaskAttemptRow, attempt.attempt_id.root)
        assert row is not None
        assert row.attempt_json["failure_class"] == FailureClass.UNKNOWN.value
