"""Atomic Stage 5 Task/Attempt/Effect command owner."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import JsonValue
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.models import (
    ProjectRow,
    ProjectWriterClaimRow,
    RunCheckpointRow,
    RunStreamRow,
    RuntimeEffectProjectionRow,
    RuntimeTaskAttemptRow,
    RuntimeTaskProjectionRow,
)
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.changes import CommitRequest, CommitResult, CommitStatus
from novel_agent.domain.creative_runtime import AcceptanceReceipt, CreativeRunRequest
from novel_agent.domain.ids import CommitId, RunId, StableId, TaskId
from novel_agent.domain.runtime import (
    STAGE5_EVENT_SCHEMA_VERSION,
    AcceptanceRecordedPayload,
    AttemptFence,
    AttemptOutcome,
    CheckpointCreatedPayload,
    ControlIntentPayload,
    EffectReceipt,
    EffectRequestedPayload,
    EffectStatus,
    EffectTerminalPayload,
    FailureClass,
    ResumabilityStatus,
    RunCheckpoint,
    RunEvent,
    RunEventType,
    TaskAttempt,
    TaskAttemptSettledPayload,
    TaskAttemptStartedPayload,
    TaskBlockedPayload,
    TaskClaimedPayload,
    TaskCreatedPayload,
    TaskKind,
    TaskRecord,
    TaskStatus,
    WriterClaimedPayload,
    evaluate_task_eligibility,
    failure_policy,
)
from novel_agent.services.commits import CommitService
from novel_agent.services.event_log import RunEventLogRepository


class RuntimeCommandError(RuntimeError):
    pass


class StaleAttemptFenceError(RuntimeCommandError):
    pass


class RuntimeCommandConflictError(RuntimeCommandError):
    pass


def _digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


class RuntimeCommandService:
    """The sole writer of runtime task, attempt, effect, and writer-lane projections."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        events: RunEventLogRepository,
        permission_hash_resolver: Callable[[str], str],
    ) -> None:
        self._session_factory = session_factory
        self._events = events
        self._permission_hash_resolver = permission_hash_resolver

    def create_run_and_initial_task(self, request: CreativeRunRequest) -> TaskRecord:
        task = TaskRecord(
            task_id=TaskId(f"{request.run_id.root}.plan"),
            run_id=request.run_id,
            project_id=request.project_id,
            kind=TaskKind.PLAN_CANDIDATE,
            task_revision=0,
            status=TaskStatus.READY,
            basis_commit=request.basis_commit,
            basis_snapshot=request.basis_snapshot,
            policy_hash=request.policy.policy_hash,
            permission_hash=request.policy.permission_hash,
            input_artifact_refs=request.input_artifact_refs,
            failure_budget=request.policy.max_task_attempts,
            target_chapters=request.target_chapters,
        )
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            if session.get(RuntimeTaskProjectionRow, task.task_id.root) is not None:
                restored = self._load_task(session, task.task_id, lock=False)
                if restored != task:
                    raise RuntimeCommandConflictError(
                        "run identity was reused with another request"
                    )
                return restored
            project = session.get(ProjectRow, request.project_id.root)
            if project is None or project.current_commit_id != request.basis_commit.root:
                raise RuntimeCommandConflictError("run basis is not the current project commit")
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_TASK_CREATED,
                TaskCreatedPayload(task=task).model_dump(mode="json"),
                StableId(f"{task.task_id.root}.created"),
            )
            self._insert_task(session, task, now)
        return task

    def get_task(self, task_id: TaskId) -> TaskRecord:
        with self._session_factory() as session:
            return self._load_task(session, task_id, lock=False)

    def create_task(self, task: TaskRecord) -> TaskRecord:
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            existing = session.get(RuntimeTaskProjectionRow, task.task_id.root)
            if existing is not None:
                restored = TaskRecord.model_validate_json(json.dumps(existing.task_json))
                if restored != task:
                    raise RuntimeCommandConflictError("task identity collision")
                return restored
            for dependency in task.dependency_task_ids:
                if session.get(RuntimeTaskProjectionRow, dependency.root) is None:
                    raise RuntimeCommandConflictError("task dependency does not exist")
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_TASK_CREATED,
                TaskCreatedPayload(task=task).model_dump(mode="json"),
                StableId(f"{task.task_id.root}.created"),
            )
            self._insert_task(session, task, now)
            return task

    def complete_waiting_task(
        self,
        task_id: TaskId,
        *,
        receipt: AcceptanceReceipt,
        receipt_ref: ArtifactRef,
    ) -> TaskRecord:
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            task = self._load_task(session, task_id, lock=True)
            if task.status is not TaskStatus.WAITING_INPUT or task.kind not in {
                TaskKind.PLAN_ACCEPTANCE,
                TaskKind.DRAFT_ACCEPTANCE,
            }:
                raise RuntimeCommandConflictError("acceptance requires a waiting acceptance task")
            if receipt.candidate.artifact_ref not in task.input_artifact_refs:
                raise RuntimeCommandConflictError("acceptance candidate is not bound to this task")
            terminal = (
                TaskStatus.SUCCEEDED
                if receipt.accepted_binding is not None
                else TaskStatus.CANCELLED
            )
            updated = task.model_copy(
                update={
                    "task_revision": task.task_revision + 1,
                    "status": terminal,
                    "terminal_artifact_refs": (receipt_ref,),
                }
            )
            self._update_task(session, updated, now)
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_ACCEPTANCE_RECORDED,
                AcceptanceRecordedPayload(
                    command_id=receipt.command_id,
                    receipt_id=receipt.receipt_id,
                    candidate_id=receipt.candidate.candidate_id,
                    candidate_hash=receipt.candidate.candidate_hash,
                    decision=receipt.decision.value,
                    actor_kind=(
                        receipt.accepted_binding.actor_kind.value
                        if receipt.accepted_binding is not None
                        else "rejector"
                    ),
                ).model_dump(mode="json"),
                receipt.idempotency_identity,
                artifact_refs=(receipt_ref,),
            )
            return updated

    def claim(
        self,
        task_id: TaskId,
        *,
        worker_id: str,
        observed_revision: int | None = None,
    ) -> tuple[TaskAttempt, AttemptFence]:
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker id must be non-empty and bounded")
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            task = self._load_task(session, task_id, lock=True)
            self._require_observed_revision(task, observed_revision)
            project = session.get(ProjectRow, task.project_id.root)
            if project is None or project.current_commit_id is None:
                raise RuntimeCommandConflictError("project has no current commit")
            dependencies = tuple(
                self._load_task(session, item, lock=False).status
                for item in task.dependency_task_ids
            )
            eligibility = evaluate_task_eligibility(
                task,
                now=now,
                current_commit=CommitId(project.current_commit_id),
                dependency_statuses=dependencies,
                permission_hash=self._permission_hash_resolver(task.project_id.root),
                writer_generation=task.writer_generation,
            )
            if not eligibility.eligible:
                raise RuntimeCommandConflictError(eligibility.reason_code)
            attempt_no = (
                int(
                    session.scalar(
                        select(func.count(RuntimeTaskAttemptRow.attempt_id)).where(
                            RuntimeTaskAttemptRow.task_id == task_id.root
                        )
                    )
                    or 0
                )
                + 1
            )
            token = StableId(f"claim.{secrets.token_hex(32)}")
            attempt_id = StableId(f"attempt.{secrets.token_hex(24)}")
            revision = task.task_revision + 1
            attempt = TaskAttempt(
                attempt_id=attempt_id,
                task_id=task.task_id,
                attempt_no=attempt_no,
                worker_id=worker_id,
                claim_token_digest=_digest(token.root),
                fence_generation=attempt_no,
                claimed_at=now,
            )
            claimed = task.model_copy(
                update={
                    "task_revision": revision,
                    "status": TaskStatus.RUNNING,
                    "current_attempt_id": attempt_id,
                }
            )
            self._update_task(session, claimed, now)
            self._insert_attempt(session, attempt)
            fence = AttemptFence(
                project_id=task.project_id,
                task_id=task.task_id,
                attempt_id=attempt_id,
                claim_token=token,
                task_revision=revision,
                writer_generation=task.writer_generation,
            )
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_TASK_CLAIMED,
                TaskClaimedPayload(
                    attempt=attempt,
                    fence_digest=_digest(fence.model_dump_json()),
                ).model_dump(mode="json"),
                StableId(f"{attempt_id.root}.claimed"),
            )
            return attempt, fence

    def mark_started(self, fence: AttemptFence) -> TaskAttempt:
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            task, attempt = self._require_fence(session, fence)
            if attempt.started_at is not None:
                return attempt
            started = attempt.model_copy(update={"started_at": now})
            self._update_attempt(session, started)
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_ATTEMPT_STARTED,
                TaskAttemptStartedPayload(
                    attempt_id=attempt.attempt_id,
                    worker_id=attempt.worker_id,
                    started_at=now,
                ).model_dump(mode="json"),
                StableId(f"{attempt.attempt_id.root}.started"),
            )
            return started

    def settle_attempt(
        self,
        fence: AttemptFence,
        *,
        outcome: AttemptOutcome,
        terminal_status: TaskStatus,
        artifact_refs: tuple[ArtifactRef, ...] = (),
        failure_class: FailureClass | None = None,
    ) -> TaskRecord:
        if terminal_status not in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.BLOCKED,
            TaskStatus.WAITING_RETRY,
        }:
            raise ValueError("attempt settlement requires a terminal or explicit waiting status")
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            task, attempt = self._require_fence(session, fence)
            if task.cancel_requested and terminal_status is not TaskStatus.CANCELLED:
                raise RuntimeCommandConflictError(
                    "cancel-requested attempt may only settle CANCELLED"
                )
            unsettled = session.scalar(
                select(RuntimeEffectProjectionRow.effect_identity).where(
                    RuntimeEffectProjectionRow.attempt_id == attempt.attempt_id.root,
                    RuntimeEffectProjectionRow.status.in_(
                        (EffectStatus.REQUESTED.value, EffectStatus.UNCERTAIN.value)
                    ),
                )
            )
            if unsettled is not None:
                raise RuntimeCommandConflictError("attempt has an unresolved effect frontier")
            settled_attempt = attempt.model_copy(
                update={"ended_at": now, "outcome": outcome, "failure_class": failure_class}
            )
            consumes_budget = (
                failure_class is not None and failure_policy(failure_class).consumes_task_budget
            )
            remaining = task.failure_budget - (1 if consumes_budget else 0)
            settled_task = task.model_copy(
                update={
                    "task_revision": task.task_revision + 1,
                    "status": terminal_status,
                    "current_attempt_id": None,
                    "terminal_artifact_refs": artifact_refs,
                    "failure_budget": max(0, remaining),
                    "block_cause": (
                        failure_class.value
                        if terminal_status is TaskStatus.BLOCKED and failure_class is not None
                        else task.block_cause
                    ),
                }
            )
            self._update_attempt(session, settled_attempt)
            self._update_task(session, settled_task, now)
            if terminal_status is TaskStatus.BLOCKED and settled_task.block_cause is not None:
                self._append(
                    session,
                    task.run_id,
                    task.task_id,
                    RunEventType.RUNTIME_TASK_BLOCKED,
                    TaskBlockedPayload(
                        failure_class=failure_class or FailureClass.BASIS_CHANGED,
                        cause_fingerprint=_digest(settled_task.block_cause),
                        sanitized_message=settled_task.block_cause[:512],
                    ).model_dump(mode="json"),
                    StableId(f"{attempt.attempt_id.root}.blocked"),
                )
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_ATTEMPT_SETTLED,
                TaskAttemptSettledPayload(
                    attempt_id=attempt.attempt_id,
                    outcome=outcome,
                    task_status=terminal_status,
                    failure_class=failure_class,
                    block_cause=(
                        failure_class.value
                        if terminal_status is TaskStatus.BLOCKED and failure_class is not None
                        else None
                    ),
                    terminal_artifact_refs=artifact_refs,
                    ended_at=now,
                ).model_dump(mode="json"),
                StableId(f"{attempt.attempt_id.root}.settled"),
                artifact_refs=artifact_refs,
            )
            return settled_task

    def record_effect_requested(self, fence: AttemptFence, receipt: EffectReceipt) -> EffectReceipt:
        if receipt.status is not EffectStatus.REQUESTED:
            raise ValueError("requested effect command requires REQUESTED receipt")
        with self._session_factory() as session, session.begin():
            task, attempt = self._require_fence(session, fence)
            if receipt.attempt_no != attempt.attempt_no:
                raise RuntimeCommandConflictError("effect attempt number does not match its owner")
            existing = session.get(RuntimeEffectProjectionRow, receipt.effect_identity.root)
            if existing is not None:
                restored = EffectReceipt.model_validate_json(json.dumps(existing.effect_json))
                if restored != receipt:
                    raise RuntimeCommandConflictError("effect identity collision")
                return restored
            session.add(
                RuntimeEffectProjectionRow(
                    effect_identity=receipt.effect_identity.root,
                    request_identity=receipt.request_identity.root,
                    run_id=task.run_id.root,
                    task_id=task.task_id.root,
                    attempt_id=attempt.attempt_id.root,
                    status=receipt.status.value,
                    provider_request_id=receipt.provider_request_id,
                    result_ref_json=None,
                    effect_json=receipt.model_dump(mode="json"),
                )
            )
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_EFFECT_REQUESTED,
                EffectRequestedPayload(
                    effect=receipt, task_id=task.task_id, attempt_id=attempt.attempt_id
                ).model_dump(mode="json"),
                StableId(f"{receipt.effect_identity.root}.requested"),
            )
            return receipt

    def record_effect_terminal(self, fence: AttemptFence, receipt: EffectReceipt) -> EffectReceipt:
        if receipt.status is EffectStatus.REQUESTED:
            raise ValueError("terminal effect command requires a terminal receipt")
        with self._session_factory() as session, session.begin():
            task, attempt = self._require_fence(session, fence)
            row = session.get(RuntimeEffectProjectionRow, receipt.effect_identity.root)
            if row is None or row.attempt_id != attempt.attempt_id.root:
                raise RuntimeCommandConflictError("effect was not requested by this attempt")
            prior = EffectReceipt.model_validate_json(json.dumps(row.effect_json))
            if (
                prior.request_identity != receipt.request_identity
                or prior.external_system != receipt.external_system
                or prior.attempt_no != receipt.attempt_no
            ):
                raise RuntimeCommandConflictError(
                    "terminal effect identity differs from its request"
                )
            row.status = receipt.status.value
            row.provider_request_id = receipt.provider_request_id
            row.result_ref_json = (
                None
                if receipt.result_artifact_ref is None
                else receipt.result_artifact_ref.model_dump(mode="json")
            )
            row.effect_json = receipt.model_dump(mode="json")
            refs = () if receipt.result_artifact_ref is None else (receipt.result_artifact_ref,)
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_EFFECT_TERMINAL,
                EffectTerminalPayload(
                    effect=receipt, task_id=task.task_id, attempt_id=attempt.attempt_id
                ).model_dump(mode="json"),
                StableId(f"{receipt.effect_identity.root}.{receipt.status.value}"),
                artifact_refs=refs,
            )
            return receipt

    def reconcile_effect(
        self,
        task_id: TaskId,
        receipt: EffectReceipt,
        *,
        command_id: StableId,
        observed_revision: int | None = None,
    ) -> EffectReceipt:
        """Trusted resolver/operator path for a dead attempt whose token no longer exists."""

        if receipt.status is EffectStatus.REQUESTED:
            raise ValueError("reconciliation requires an authoritative terminal status")
        with self._session_factory() as session, session.begin():
            task = self._load_task(session, task_id, lock=True)
            self._require_observed_revision(task, observed_revision)
            row = session.get(RuntimeEffectProjectionRow, receipt.effect_identity.root)
            if row is None or row.task_id != task_id.root:
                raise RuntimeCommandConflictError("unknown effect for task")
            prior = EffectReceipt.model_validate_json(json.dumps(row.effect_json))
            if (
                prior.request_identity != receipt.request_identity
                or prior.external_system != receipt.external_system
                or prior.attempt_no != receipt.attempt_no
            ):
                raise RuntimeCommandConflictError("effect request identity changed")
            row.status = receipt.status.value
            row.provider_request_id = receipt.provider_request_id
            row.result_ref_json = (
                None
                if receipt.result_artifact_ref is None
                else receipt.result_artifact_ref.model_dump(mode="json")
            )
            row.effect_json = receipt.model_dump(mode="json")
            refs = () if receipt.result_artifact_ref is None else (receipt.result_artifact_ref,)
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_EFFECT_TERMINAL,
                EffectTerminalPayload(
                    effect=receipt,
                    task_id=task.task_id,
                    attempt_id=StableId(row.attempt_id),
                ).model_dump(mode="json"),
                command_id,
                artifact_refs=refs,
            )
            return receipt

    def operator_reconcile_attempt(
        self,
        task_id: TaskId,
        *,
        command_id: StableId,
        actor_id: str,
        reason: str,
        terminal_status: TaskStatus = TaskStatus.WAITING_RETRY,
        observed_revision: int | None = None,
    ) -> TaskRecord:
        if terminal_status not in {TaskStatus.WAITING_RETRY, TaskStatus.CANCELLED}:
            raise ValueError("operator reconciliation may only retry or cancel")
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            task = self._load_task(session, task_id, lock=True)
            self._require_observed_revision(task, observed_revision)
            if task.current_attempt_id is None:
                raise RuntimeCommandConflictError("task has no attempt to reconcile")
            unresolved = session.scalar(
                select(RuntimeEffectProjectionRow.effect_identity).where(
                    RuntimeEffectProjectionRow.attempt_id == task.current_attempt_id.root,
                    RuntimeEffectProjectionRow.status.in_(
                        (EffectStatus.REQUESTED.value, EffectStatus.UNCERTAIN.value)
                    ),
                )
            )
            if unresolved is not None:
                raise RuntimeCommandConflictError("attempt effect frontier is unresolved")
            row = session.get(RuntimeTaskAttemptRow, task.current_attempt_id.root)
            if row is None:
                raise RuntimeCommandConflictError("current attempt projection is missing")
            attempt = TaskAttempt.model_validate_json(json.dumps(row.attempt_json))
            outcome = (
                AttemptOutcome.CANCELLED
                if terminal_status is TaskStatus.CANCELLED
                else AttemptOutcome.SUSPENDED
            )
            failure = (
                FailureClass.CANCELLED
                if terminal_status is TaskStatus.CANCELLED
                else FailureClass.WORKER_STARTUP
            )
            settled_attempt = attempt.model_copy(
                update={"ended_at": now, "outcome": outcome, "failure_class": failure}
            )
            settled_task = task.model_copy(
                update={
                    "task_revision": task.task_revision + 1,
                    "status": terminal_status,
                    "current_attempt_id": None,
                    "paused": terminal_status is TaskStatus.CANCELLED,
                }
            )
            self._update_attempt(session, settled_attempt)
            self._update_task(session, settled_task, now)
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_CONTROL_RECORDED,
                ControlIntentPayload(
                    command_id=command_id,
                    action="operator_reconcile",
                    actor_id=actor_id,
                    reason=reason,
                ).model_dump(mode="json"),
                command_id,
            )
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_ATTEMPT_SETTLED,
                TaskAttemptSettledPayload(
                    attempt_id=attempt.attempt_id,
                    outcome=outcome,
                    task_status=terminal_status,
                    failure_class=failure,
                    ended_at=now,
                ).model_dump(mode="json"),
                StableId(f"{command_id.root}.attempt-settled"),
            )
            return settled_task

    def mark_recovery_pending(
        self,
        task_id: TaskId,
        *,
        command_id: StableId,
        actor_id: str,
        reason: str,
        observed_revision: int | None = None,
    ) -> TaskRecord:
        """Fence new claims while liveness or an external effect remains unresolved."""

        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            task = self._load_task(session, task_id, lock=True)
            self._require_observed_revision(task, observed_revision)
            if task.status is TaskStatus.RECOVERY_PENDING:
                return task
            if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                raise RuntimeCommandConflictError("settled task cannot enter recovery pending")
            updated = task.model_copy(
                update={
                    "task_revision": task.task_revision + 1,
                    "status": TaskStatus.RECOVERY_PENDING,
                }
            )
            self._update_task(session, updated, now)
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_CONTROL_RECORDED,
                ControlIntentPayload(
                    command_id=command_id,
                    action="recovery_pending",
                    actor_id=actor_id,
                    reason=reason,
                ).model_dump(mode="json"),
                command_id,
            )
            return updated

    def save_checkpoint(self, fence: AttemptFence, checkpoint: RunCheckpoint) -> RunCheckpoint:
        with self._session_factory() as session, session.begin():
            task, attempt = self._require_fence(session, fence)
            if checkpoint.run_id != task.run_id:
                raise RuntimeCommandConflictError("checkpoint belongs to another run")
            unresolved = session.scalar(
                select(RuntimeEffectProjectionRow.effect_identity).where(
                    RuntimeEffectProjectionRow.attempt_id == attempt.attempt_id.root,
                    RuntimeEffectProjectionRow.status.in_(
                        (EffectStatus.REQUESTED.value, EffectStatus.UNCERTAIN.value)
                    ),
                )
            )
            if (
                checkpoint.resumability_status is ResumabilityStatus.RESUMABLE
                and unresolved is not None
            ):
                raise RuntimeCommandConflictError(
                    "resumable checkpoint requires a settled effect frontier"
                )
            if checkpoint.resumability_status is ResumabilityStatus.RESUMABLE:
                effect_ids = set(
                    session.scalars(
                        select(RuntimeEffectProjectionRow.effect_identity).where(
                            RuntimeEffectProjectionRow.attempt_id == attempt.attempt_id.root
                        )
                    )
                )
                completed_ids = {item.root for item in checkpoint.completed_effect_ids}
                if effect_ids != completed_ids:
                    raise RuntimeCommandConflictError(
                        "checkpoint completed effect frontier is incomplete"
                    )
            existing = session.get(RunCheckpointRow, checkpoint.checkpoint_id.root)
            if existing is not None:
                restored = RunCheckpoint.model_validate_json(json.dumps(existing.checkpoint_json))
                if restored != checkpoint:
                    raise RuntimeCommandConflictError("checkpoint identity collision")
                return restored
            stream = session.get(RunStreamRow, task.run_id.root)
            if stream is None or checkpoint.event_position > stream.last_sequence_no:
                raise RuntimeCommandConflictError("checkpoint exceeds event high watermark")
            session.add(
                RunCheckpointRow(
                    checkpoint_id=checkpoint.checkpoint_id.root,
                    run_id=checkpoint.run_id.root,
                    event_position=checkpoint.event_position,
                    checkpoint_json=checkpoint.model_dump(mode="json"),
                    created_at=datetime.now(UTC),
                )
            )
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_CHECKPOINT_SAVED,
                CheckpointCreatedPayload(
                    checkpoint=checkpoint,
                    task_id=task.task_id,
                    attempt_id=attempt.attempt_id,
                ).model_dump(mode="json"),
                StableId(f"{checkpoint.checkpoint_id.root}.saved"),
                artifact_refs=(checkpoint.state_artifact_ref,),
            )
            return checkpoint

    def control(
        self,
        task_id: TaskId,
        *,
        command_id: StableId,
        action: str,
        actor_id: str,
        reason: str,
        observed_revision: int | None = None,
    ) -> TaskRecord:
        transitions = {"retry": TaskStatus.READY}
        if action not in {"pause", "cancel", *transitions}:
            raise ValueError("unsupported runtime control action")
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            task = self._load_task(session, task_id, lock=True)
            self._require_observed_revision(task, observed_revision)
            if task.current_attempt_id is not None and action in {"retry", "unblock"}:
                raise RuntimeCommandConflictError("cannot replace an active attempt")
            if action == "retry" and task.status is not TaskStatus.WAITING_RETRY:
                raise RuntimeCommandConflictError("retry requires WAITING_RETRY")
            status = transitions.get(action, task.status)
            if action == "pause" and task.current_attempt_id is None:
                status = TaskStatus.PENDING
            if action == "cancel":
                status = (
                    TaskStatus.CANCELLED
                    if task.current_attempt_id is None
                    else TaskStatus.RECOVERY_PENDING
                )
            updated = task.model_copy(
                update={
                    "task_revision": task.task_revision + 1,
                    "status": status,
                    "paused": action in {"pause", "cancel"},
                    "cancel_requested": action == "cancel",
                }
            )
            self._update_task(session, updated, now)
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_CONTROL_RECORDED,
                ControlIntentPayload(
                    command_id=command_id, action=action, actor_id=actor_id, reason=reason
                ).model_dump(mode="json"),
                command_id,
            )
            return updated

    def unblock(
        self,
        task_id: TaskId,
        *,
        command_id: StableId,
        actor_id: str,
        block_cause_fingerprint: str,
        changed_evidence_refs: tuple[ArtifactRef, ...],
        observed_revision: int | None = None,
    ) -> TaskRecord:
        if not changed_evidence_refs:
            raise ValueError("unblock requires changed prerequisite evidence")
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            task = self._load_task(session, task_id, lock=True)
            self._require_observed_revision(task, observed_revision)
            if task.status is not TaskStatus.BLOCKED or task.block_cause is None:
                raise RuntimeCommandConflictError("unblock requires a recorded block cause")
            if _digest(task.block_cause) != block_cause_fingerprint:
                raise RuntimeCommandConflictError("block cause fingerprint is stale")
            updated = task.model_copy(
                update={
                    "task_revision": task.task_revision + 1,
                    "status": TaskStatus.READY,
                    "block_cause": None,
                }
            )
            self._update_task(session, updated, now)
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_CONTROL_RECORDED,
                ControlIntentPayload(
                    command_id=command_id,
                    action="unblock",
                    actor_id=actor_id,
                    reason="block prerequisite changed with immutable evidence",
                ).model_dump(mode="json"),
                command_id,
                artifact_refs=changed_evidence_refs,
            )
            return updated

    def resume(
        self,
        task_id: TaskId,
        *,
        command_id: StableId,
        actor_id: str,
        reason: str,
        observed_revision: int | None = None,
    ) -> TaskRecord:
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            task = self._load_task(session, task_id, lock=True)
            self._require_observed_revision(task, observed_revision)
            if not task.paused or task.current_attempt_id is not None:
                raise RuntimeCommandConflictError(
                    "resume requires a paused task without active attempt"
                )
            updated = task.model_copy(
                update={
                    "task_revision": task.task_revision + 1,
                    "status": TaskStatus.READY,
                    "paused": False,
                    "cancel_requested": False,
                }
            )
            self._update_task(session, updated, now)
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_CONTROL_RECORDED,
                ControlIntentPayload(
                    command_id=command_id, action="resume", actor_id=actor_id, reason=reason
                ).model_dump(mode="json"),
                command_id,
            )
            return updated

    def claim_writer_lane(self, fence: AttemptFence) -> AttemptFence:
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            task, attempt = self._require_fence(session, fence)
            if task.kind not in {TaskKind.PLAN_COMMIT, TaskKind.DRAFT_COMMIT}:
                raise RuntimeCommandConflictError("only commit tasks may claim the writer lane")
            row = session.scalar(
                select(ProjectWriterClaimRow)
                .where(ProjectWriterClaimRow.project_id == task.project_id.root)
                .with_for_update()
            )
            generation = 1 if row is None else row.generation + 1
            if row is None:
                session.add(
                    ProjectWriterClaimRow(
                        project_id=task.project_id.root,
                        run_id=task.run_id.root,
                        task_id=task.task_id.root,
                        attempt_id=attempt.attempt_id.root,
                        generation=generation,
                        updated_at=now,
                    )
                )
            else:
                row.run_id = task.run_id.root
                row.task_id = task.task_id.root
                row.attempt_id = attempt.attempt_id.root
                row.generation = generation
                row.updated_at = now
            updated = task.model_copy(
                update={
                    "writer_generation": generation,
                    "task_revision": task.task_revision + 1,
                }
            )
            self._update_task(session, updated, now)
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_WRITER_CLAIMED,
                WriterClaimedPayload(
                    attempt_id=attempt.attempt_id,
                    writer_generation=generation,
                ).model_dump(mode="json"),
                StableId(f"{attempt.attempt_id.root}.writer.{generation}"),
            )
            return fence.model_copy(update={"writer_generation": generation})

    def verify_writer_lane(self, fence: AttemptFence) -> None:
        with self._session_factory() as session:
            task, attempt = self._require_fence(session, fence)
            row = session.get(ProjectWriterClaimRow, task.project_id.root)
            if (
                row is None
                or row.attempt_id != attempt.attempt_id.root
                or row.generation != task.writer_generation
            ):
                raise StaleAttemptFenceError("attempt no longer owns the project writer lane")

    def commit_accepted_candidate(
        self,
        fence: AttemptFence,
        request: CommitRequest,
        commits: CommitService,
    ) -> CommitResult:
        """Fence, Canon CAS, task projection, and RunEvent settle in one transaction."""

        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            task, attempt = self._require_fence(session, fence)
            if task.kind not in {TaskKind.PLAN_COMMIT, TaskKind.DRAFT_COMMIT}:
                raise RuntimeCommandConflictError("only a commit task may advance Canon")
            writer = session.scalar(
                select(ProjectWriterClaimRow)
                .where(ProjectWriterClaimRow.project_id == task.project_id.root)
                .with_for_update()
            )
            if (
                writer is None
                or writer.attempt_id != attempt.attempt_id.root
                or writer.generation != fence.writer_generation
            ):
                # _require_fence already validated the same lane
                raise StaleAttemptFenceError(  # pragma: no cover - unreachable after _require_fence
                    "attempt lost the project writer lane"
                )
            if request.project_id != task.project_id or request.base_commit != task.basis_commit:
                raise RuntimeCommandConflictError("commit request differs from task basis")
            result = commits._commit_in_session(session, request)
            if result.status is CommitStatus.ACCEPTED:
                status = TaskStatus.SUCCEEDED
                outcome = AttemptOutcome.SUCCEEDED
                failure = None
                event_type = RunEventType.COMMIT_ACCEPTED
            elif result.status is CommitStatus.CONFLICTED:
                status = TaskStatus.BLOCKED
                outcome = AttemptOutcome.FAILED
                failure = FailureClass.COMMIT_CONFLICT
                event_type = RunEventType.COMMIT_REJECTED
            else:
                status = TaskStatus.BLOCKED
                outcome = AttemptOutcome.FAILED
                failure = FailureClass.VALIDATION_REJECTED
                event_type = RunEventType.COMMIT_REJECTED
            settled_attempt = attempt.model_copy(
                update={"ended_at": now, "outcome": outcome, "failure_class": failure}
            )
            settled_task = task.model_copy(
                update={
                    "task_revision": task.task_revision + 1,
                    "status": status,
                    "current_attempt_id": None,
                    "terminal_artifact_refs": request.bundle.produced_artifacts,
                    "failure_budget": max(
                        0,
                        task.failure_budget
                        - (
                            1
                            if failure is not None and failure_policy(failure).consumes_task_budget
                            else 0
                        ),
                    ),
                    "block_cause": (
                        result.reason if status is TaskStatus.BLOCKED else task.block_cause
                    ),
                }
            )
            self._update_attempt(session, settled_attempt)
            self._update_task(session, settled_task, now)
            if status is TaskStatus.BLOCKED and result.reason is not None:
                self._append(
                    session,
                    task.run_id,
                    task.task_id,
                    RunEventType.RUNTIME_TASK_BLOCKED,
                    TaskBlockedPayload(
                        failure_class=failure or FailureClass.VALIDATION_REJECTED,
                        cause_fingerprint=_digest(result.reason),
                        sanitized_message=result.reason[:512],
                    ).model_dump(mode="json"),
                    StableId(f"{attempt.attempt_id.root}.blocked"),
                )
            self._append(
                session,
                task.run_id,
                task.task_id,
                event_type,
                result.model_dump(mode="json"),
                StableId(f"{attempt.attempt_id.root}.commit"),
                artifact_refs=request.bundle.produced_artifacts,
            )
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_ATTEMPT_SETTLED,
                TaskAttemptSettledPayload(
                    attempt_id=attempt.attempt_id,
                    outcome=outcome,
                    task_status=status,
                    failure_class=failure,
                    block_cause=(result.reason if status is TaskStatus.BLOCKED else None),
                    terminal_artifact_refs=request.bundle.produced_artifacts,
                    ended_at=now,
                ).model_dump(mode="json"),
                StableId(f"{attempt.attempt_id.root}.settled"),
                artifact_refs=request.bundle.produced_artifacts,
            )
            return result

    def _require_fence(
        self, session: Session, fence: AttemptFence
    ) -> tuple[TaskRecord, TaskAttempt]:
        task = self._load_task(session, fence.task_id, lock=True)
        if task.project_id != fence.project_id or task.current_attempt_id != fence.attempt_id:
            raise StaleAttemptFenceError("attempt is not current")
        row = session.get(RuntimeTaskAttemptRow, fence.attempt_id.root)
        if row is None:
            raise StaleAttemptFenceError("attempt does not exist")
        attempt = TaskAttempt.model_validate_json(json.dumps(row.attempt_json))
        if (
            attempt.claim_token_digest != _digest(fence.claim_token.root)
            or fence.task_revision > task.task_revision
            or fence.writer_generation != task.writer_generation
        ):
            raise StaleAttemptFenceError("attempt fence does not match current owner")
        if (
            task.kind in {TaskKind.PLAN_COMMIT, TaskKind.DRAFT_COMMIT}
            and task.writer_generation > 0
        ):
            writer = session.get(ProjectWriterClaimRow, task.project_id.root)
            if (
                writer is None
                or writer.attempt_id != attempt.attempt_id.root
                or writer.generation != task.writer_generation
            ):
                raise StaleAttemptFenceError("attempt lost the project writer generation")
        return task, attempt

    @staticmethod
    def _require_observed_revision(task: TaskRecord, observed_revision: int | None) -> None:
        if observed_revision is not None and task.task_revision != observed_revision:
            raise RuntimeCommandConflictError("observed task revision is stale")

    def _append(
        self,
        session: Session,
        run_id: RunId,
        task_id: TaskId,
        event_type: RunEventType,
        payload: JsonValue,
        identity: StableId,
        *,
        artifact_refs: tuple[ArtifactRef, ...] = (),
    ) -> None:
        stream = session.get(RunStreamRow, run_id.root)
        sequence = 1 if stream is None else stream.last_sequence_no + 1
        event = RunEvent(
            event_id=StableId(f"evt.{secrets.token_hex(24)}"),
            run_id=run_id,
            task_id=task_id,
            sequence_no=sequence,
            event_type=event_type,
            occurred_at=datetime.now(UTC),
            idempotency_identity=identity,
            payload_schema_version=STAGE5_EVENT_SCHEMA_VERSION,
            trace_id=f"runtime.{run_id.root}",
            payload=payload,
            artifact_refs=artifact_refs,
        )
        self._events._append_in_session(session, event)

    @staticmethod
    def _insert_task(session: Session, task: TaskRecord, now: datetime) -> None:
        session.add(
            RuntimeTaskProjectionRow(
                task_id=task.task_id.root,
                run_id=task.run_id.root,
                project_id=task.project_id.root,
                kind=task.kind.value,
                status=task.status.value,
                revision=task.task_revision,
                current_attempt_id=None,
                basis_commit=task.basis_commit.root,
                basis_snapshot=None if task.basis_snapshot is None else task.basis_snapshot.root,
                policy_hash=task.policy_hash,
                permission_hash=task.permission_hash,
                priority=task.priority,
                scheduled_for=task.scheduled_for,
                task_json=task.model_dump(mode="json"),
                updated_at=now,
            )
        )

    @staticmethod
    def _update_task(session: Session, task: TaskRecord, now: datetime) -> None:
        row = session.get(RuntimeTaskProjectionRow, task.task_id.root)
        if row is None:
            raise LookupError(task.task_id.root)  # pragma: no cover - guarded by _load_task
        row.status = task.status.value
        row.revision = task.task_revision
        row.current_attempt_id = (
            None if task.current_attempt_id is None else task.current_attempt_id.root
        )
        row.basis_commit = task.basis_commit.root
        row.basis_snapshot = None if task.basis_snapshot is None else task.basis_snapshot.root
        row.task_json = task.model_dump(mode="json")
        row.updated_at = now

    @staticmethod
    def _load_task(session: Session, task_id: TaskId, *, lock: bool) -> TaskRecord:
        statement = select(RuntimeTaskProjectionRow).where(
            RuntimeTaskProjectionRow.task_id == task_id.root
        )
        if lock:
            statement = statement.with_for_update()
        row = session.scalar(statement)
        if row is None:
            raise LookupError(task_id.root)
        return TaskRecord.model_validate_json(json.dumps(row.task_json))

    @staticmethod
    def _insert_attempt(session: Session, attempt: TaskAttempt) -> None:
        session.add(
            RuntimeTaskAttemptRow(
                attempt_id=attempt.attempt_id.root,
                task_id=attempt.task_id.root,
                attempt_no=attempt.attempt_no,
                worker_id=attempt.worker_id,
                claim_digest=attempt.claim_token_digest,
                fence_generation=attempt.fence_generation,
                claimed_at=attempt.claimed_at,
                started_at=attempt.started_at,
                ended_at=attempt.ended_at,
                outcome=None,
                failure_class=None,
                attempt_json=attempt.model_dump(mode="json"),
            )
        )

    @staticmethod
    def _update_attempt(session: Session, attempt: TaskAttempt) -> None:
        row = session.get(RuntimeTaskAttemptRow, attempt.attempt_id.root)
        if row is None:
            raise LookupError(  # pragma: no cover - guarded by _require_fence
                attempt.attempt_id.root
            )
        row.started_at = attempt.started_at
        row.ended_at = attempt.ended_at
        row.outcome = None if attempt.outcome is None else attempt.outcome.value
        row.failure_class = None if attempt.failure_class is None else attempt.failure_class.value
        row.attempt_json = attempt.model_dump(mode="json")


__all__ = [
    "RuntimeCommandConflictError",
    "RuntimeCommandError",
    "RuntimeCommandService",
    "StaleAttemptFenceError",
]
