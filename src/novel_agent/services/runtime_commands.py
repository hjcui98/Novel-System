"""Atomic Stage 5 Task/Attempt/Effect command owner."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

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
from novel_agent.domain.artifacts import ArtifactRef, RootManifest
from novel_agent.domain.changes import CommitRequest, CommitResult, CommitStatus
from novel_agent.domain.creative_runtime import AcceptanceReceipt, CreativeRunRequest
from novel_agent.domain.ids import CommitId, RunId, StableId, TaskId
from novel_agent.domain.memory_write import (
    MemoryGapClassification,
    MemoryRepairFinding,
    MemoryWriteWorkflowResult,
    MemoryWriteWorkflowStatus,
)
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
    TaskPurpose,
    TaskRecord,
    TaskStatus,
    WriterClaimedPayload,
    evaluate_task_eligibility,
    failure_policy,
    normalize_failure_class,
)
from novel_agent.domain.world import PlanLevel
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.event_log import RunEventLogRepository

MEMORY_WRITE_RESULT_MEDIA_TYPE = "application/vnd.novel-agent.memory-write-workflow-result+json"


class RuntimeCommandError(RuntimeError):
    pass


class StaleAttemptFenceError(RuntimeCommandError):
    pass


class RuntimeCommandConflictError(RuntimeCommandError):
    pass


class WriterLaneBusyError(RuntimeCommandConflictError):
    """A different live attempt currently owns the project's write lane."""


def _digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def bounded_runtime_identity(
    primary: str,
    *fallbacks: str,
) -> StableId:
    """Keep runtime identities readable while honoring the StableId limit."""

    for value in (primary, *fallbacks):
        try:
            return StableId(value)
        except ValueError:
            continue
    raise RuntimeCommandConflictError("runtime identity is too long")


def _bounded_runtime_identity(
    primary: str,
    *fallbacks: str,
) -> StableId:
    """Compatibility wrapper for the runtime command owner's internal calls."""

    return bounded_runtime_identity(primary, *fallbacks)


def _task_created_identity(task_id: TaskId) -> StableId:
    """Use the task identity itself when the readable created suffix cannot fit."""

    return _bounded_runtime_identity(f"{task_id.root}.created", task_id.root)


class RuntimeCommandService:
    """The sole writer of runtime task, attempt, effect, and writer-lane projections."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        events: RunEventLogRepository,
        permission_hash_resolver: Callable[[str], str],
        *,
        attempt_lease_seconds: int = 300,
        artifacts: ArtifactRepository | None = None,
    ) -> None:
        if attempt_lease_seconds < 3:
            raise ValueError("Attempt lease must be at least three seconds")
        self._session_factory = session_factory
        self._events = events
        self._permission_hash_resolver = permission_hash_resolver
        self._attempt_lease = timedelta(seconds=attempt_lease_seconds)
        self._artifacts = artifacts

    @property
    def heartbeat_interval_seconds(self) -> float:
        return max(1.0, self._attempt_lease.total_seconds() / 3.0)

    def create_run_and_initial_task(self, request: CreativeRunRequest) -> TaskRecord:
        if request.plan_level in {PlanLevel.STORY, PlanLevel.ARC_VOLUME}:
            horizon_start: int | None = None
            horizon_end: int | None = None
        else:
            horizon_start = request.current_chapter + 1
            horizon_end = min(
                request.target_chapters,
                request.current_chapter + request.policy.planning_horizon,
            )
        task = TaskRecord(
            task_id=TaskId(
                _bounded_runtime_identity(
                    f"{request.run_id.root}.plan",
                    request.run_id.root,
                ).root
            ),
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
            terminal_artifact_refs=request.continuation_artifact_refs,
            failure_budget=request.policy.max_task_attempts,
            retry_tranche_size=request.policy.max_task_attempts,
            chapter_index=request.current_chapter,
            target_chapters=request.target_chapters,
            horizon_start=horizon_start,
            horizon_end=horizon_end,
            plan_level=request.plan_level,
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
                _task_created_identity(task.task_id),
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
                _task_created_identity(task.task_id),
            )
            self._insert_task(session, task, now)
            return task

    def supersede_task(self, task_id: TaskId, *, reason: str) -> TaskRecord:
        if not reason or len(reason) > 512:
            raise ValueError("supersede reason must be non-empty and bounded")
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            task = self._load_task(session, task_id, lock=True)
            if task.superseded:
                return task
            if task.status not in {
                TaskStatus.PENDING,
                TaskStatus.READY,
                TaskStatus.WAITING_INPUT,
                TaskStatus.WAITING_RETRY,
                TaskStatus.BUDGET_REVIEW,
                TaskStatus.BLOCKED,
            }:
                raise RuntimeCommandConflictError("only inactive work may be superseded")
            updated = task.model_copy(
                update={
                    "task_revision": task.task_revision + 1,
                    "status": TaskStatus.CANCELLED,
                    "superseded": True,
                    "block_cause": reason,
                }
            )
            self._update_task(session, updated, now)
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_CONTROL_RECORDED,
                ControlIntentPayload(
                    command_id=_bounded_runtime_identity(
                        f"supersede.{task.task_id.root}",
                        f"supersede.{task.run_id.root}.{task.task_revision}",
                    ),
                    action="supersede",
                    actor_id="creative-runtime",
                    reason=reason,
                ).model_dump(mode="json"),
                _bounded_runtime_identity(
                    f"{task.task_id.root}.superseded",
                    f"superseded.{task.run_id.root}.{task.task_revision}",
                ),
            )
            return updated

    def complete_waiting_task(
        self,
        task_id: TaskId,
        *,
        receipt: AcceptanceReceipt,
        receipt_ref: ArtifactRef,
        successor_tasks: tuple[TaskRecord, ...] = (),
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
            self._insert_successor_tasks(session, updated, successor_tasks, now)
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
                heartbeat_at=now,
                lease_expires_at=now + self._attempt_lease,
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

    def heartbeat(self, fence: AttemptFence) -> TaskAttempt:
        """Renew only the matching current Attempt; fencing remains authoritative."""

        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            _task, attempt = self._require_fence(session, fence)
            if attempt.ended_at is not None:
                raise StaleAttemptFenceError("settled Attempt cannot heartbeat")
            renewed = attempt.model_copy(
                update={
                    "heartbeat_at": now,
                    "lease_expires_at": now + self._attempt_lease,
                }
            )
            self._update_attempt(session, renewed)
            return renewed

    def suspect_expired_attempt(
        self,
        task_id: TaskId,
        *,
        command_id: StableId,
        actor_id: str,
        reason: str,
        observed_revision: int | None = None,
        now: datetime | None = None,
    ) -> TaskRecord:
        """Fence new work after lease expiry; do not infer that external work stopped."""

        observed_at = now or datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            task = self._load_task(session, task_id, lock=True)
            self._require_observed_revision(task, observed_revision)
            if task.status is TaskStatus.RECOVERY_PENDING:
                return task
            if task.status is not TaskStatus.RUNNING or task.current_attempt_id is None:
                raise RuntimeCommandConflictError("lease suspicion requires a running Attempt")
            row = session.get(RuntimeTaskAttemptRow, task.current_attempt_id.root)
            if row is None:
                raise RuntimeCommandConflictError("current Attempt projection is missing")
            attempt = TaskAttempt.model_validate_json(json.dumps(row.attempt_json))
            if attempt.lease_expires_at is None or attempt.lease_expires_at >= observed_at:
                raise RuntimeCommandConflictError("Attempt lease has not expired")
            updated = task.model_copy(
                update={
                    "task_revision": task.task_revision + 1,
                    "status": TaskStatus.RECOVERY_PENDING,
                }
            )
            self._update_task(session, updated, observed_at)
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_CONTROL_RECORDED,
                ControlIntentPayload(
                    command_id=command_id,
                    action="lease_expired",
                    actor_id=actor_id,
                    reason=reason,
                ).model_dump(mode="json"),
                command_id,
            )
            return updated

    def reclaim_expired_attempt(
        self,
        task_id: TaskId,
        *,
        command_id: StableId,
        actor_id: str,
        reason: str,
        observed_revision: int | None = None,
        now: datetime | None = None,
    ) -> TaskRecord:
        """Release an expired owner only after the effect frontier is settled."""

        observed_at = now or datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            task = self._load_task(session, task_id, lock=True)
            self._require_observed_revision(task, observed_revision)
            if task.status is not TaskStatus.RECOVERY_PENDING or task.current_attempt_id is None:
                raise RuntimeCommandConflictError(
                    "lease reclaim requires RECOVERY_PENDING with a current Attempt"
                )
            row = session.get(RuntimeTaskAttemptRow, task.current_attempt_id.root)
            if row is None:
                raise RuntimeCommandConflictError("current Attempt projection is missing")
            attempt = TaskAttempt.model_validate_json(json.dumps(row.attempt_json))
            if attempt.lease_expires_at is None or attempt.lease_expires_at >= observed_at:
                raise RuntimeCommandConflictError("Attempt lease is no longer expired")
            unresolved = session.scalar(
                select(RuntimeEffectProjectionRow.effect_identity).where(
                    RuntimeEffectProjectionRow.attempt_id == attempt.attempt_id.root,
                    RuntimeEffectProjectionRow.status.in_(
                        (EffectStatus.REQUESTED.value, EffectStatus.UNCERTAIN.value)
                    ),
                )
            )
            if unresolved is not None:
                raise RuntimeCommandConflictError(
                    "expired Attempt effect frontier requires reconciliation"
                )
            settled_attempt = attempt.model_copy(
                update={
                    "ended_at": observed_at,
                    "outcome": AttemptOutcome.SUSPENDED,
                    "failure_class": FailureClass.WORKER_LEASE_EXPIRED,
                }
            )
            settled_task = task.model_copy(
                update={
                    "task_revision": task.task_revision + 1,
                    "status": TaskStatus.WAITING_RETRY,
                    "current_attempt_id": None,
                }
            )
            self._update_attempt(session, settled_attempt)
            self._update_task(session, settled_task, observed_at)
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_CONTROL_RECORDED,
                ControlIntentPayload(
                    command_id=command_id,
                    action="lease_reclaim",
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
                    outcome=AttemptOutcome.SUSPENDED,
                    task_status=TaskStatus.WAITING_RETRY,
                    failure_class=FailureClass.WORKER_LEASE_EXPIRED,
                    ended_at=observed_at,
                ).model_dump(mode="json"),
                _bounded_runtime_identity(
                    f"{command_id.root}.attempt-settled",
                    f"{attempt.attempt_id.root}.attempt-settled",
                ),
            )
            return settled_task

    def settle_attempt(
        self,
        fence: AttemptFence,
        *,
        outcome: AttemptOutcome,
        terminal_status: TaskStatus,
        artifact_refs: tuple[ArtifactRef, ...] = (),
        failure_class: FailureClass | str | None = None,
        successor_tasks: tuple[TaskRecord, ...] = (),
    ) -> TaskRecord:
        failure_class = normalize_failure_class(failure_class)
        if failure_class is FailureClass.UNKNOWN:
            terminal_status = failure_policy(failure_class).fallback_status
        if terminal_status not in {
            TaskStatus.READY,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.BLOCKED,
            TaskStatus.RECOVERY_PENDING,
            TaskStatus.WAITING_RETRY,
            TaskStatus.WAITING_INPUT,
            TaskStatus.BUDGET_REVIEW,
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
            settled_status = (
                TaskStatus.BUDGET_REVIEW
                if terminal_status is TaskStatus.WAITING_RETRY
                and consumes_budget
                and remaining <= 0
                else terminal_status
            )
            settled_task = task.model_copy(
                update={
                    "task_revision": task.task_revision + 1,
                    "status": settled_status,
                    "current_attempt_id": None,
                    "terminal_artifact_refs": artifact_refs,
                    "failure_budget": max(0, remaining),
                    "block_cause": (
                        failure_class.value
                        if settled_status is TaskStatus.BLOCKED and failure_class is not None
                        else task.block_cause
                    ),
                }
            )
            self._update_attempt(session, settled_attempt)
            self._update_task(session, settled_task, now)
            if settled_status is TaskStatus.BLOCKED and settled_task.block_cause is not None:
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
                    task_status=settled_status,
                    failure_class=failure_class,
                    block_cause=(
                        failure_class.value
                        if settled_status is TaskStatus.BLOCKED and failure_class is not None
                        else None
                    ),
                    terminal_artifact_refs=artifact_refs,
                    ended_at=now,
                ).model_dump(mode="json"),
                StableId(f"{attempt.attempt_id.root}.settled"),
                artifact_refs=artifact_refs,
            )
            self._insert_successor_tasks(session, settled_task, successor_tasks, now)
            return settled_task

    def settle_gap_and_create_maintenance(
        self,
        fence: AttemptFence,
        *,
        finding_ref: ArtifactRef,
        maintenance_task: TaskRecord,
    ) -> TaskRecord:
        """Atomically block a Planner gap and create its derived maintenance task."""

        finding = self._load_memory_repair_finding(finding_ref)
        expected_task_id = self._maintenance_task_id(finding)
        if maintenance_task.task_id != expected_task_id:
            raise RuntimeCommandConflictError("maintenance task identity is not finding-bound")
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            current = self._load_task(session, fence.task_id, lock=True)
            existing = session.get(RuntimeTaskProjectionRow, maintenance_task.task_id.root)
            if current.status is TaskStatus.BLOCKED and current.current_attempt_id is None:
                if existing is None:
                    raise RuntimeCommandConflictError(
                        "blocked Planner gap is missing its maintenance task"
                    )
                restored = TaskRecord.model_validate_json(json.dumps(existing.task_json))
                if restored != maintenance_task:
                    raise RuntimeCommandConflictError("maintenance task identity collision")
                if restored.input_artifact_refs != (finding_ref,):
                    raise RuntimeCommandConflictError("maintenance task finding binding changed")
                return restored

            task, attempt = self._require_fence(session, fence)
            self._validate_gap_finding(task, attempt, finding)
            self._validate_maintenance_task(
                task,
                maintenance_task,
                finding_ref=finding_ref,
            )
            if existing is not None:
                restored = TaskRecord.model_validate_json(json.dumps(existing.task_json))
                if restored != maintenance_task:
                    raise RuntimeCommandConflictError("maintenance task identity collision")
                raise RuntimeCommandConflictError(
                    "maintenance task exists before Planner gap settlement"
                )

            settled_attempt = attempt.model_copy(
                update={
                    "ended_at": now,
                    "outcome": AttemptOutcome.FAILED,
                    "failure_class": FailureClass.CANON_EXTRACTION_GAP,
                }
            )
            blocked = task.model_copy(
                update={
                    "task_revision": task.task_revision + 1,
                    "status": TaskStatus.BLOCKED,
                    "current_attempt_id": None,
                    "terminal_artifact_refs": (finding_ref,),
                    "block_cause": FailureClass.CANON_EXTRACTION_GAP.value,
                }
            )
            self._update_attempt(session, settled_attempt)
            self._update_task(session, blocked, now)
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_TASK_BLOCKED,
                TaskBlockedPayload(
                    failure_class=FailureClass.CANON_EXTRACTION_GAP,
                    cause_fingerprint=_digest(finding.finding_id.root),
                    sanitized_message=FailureClass.CANON_EXTRACTION_GAP.value,
                    error_artifact_ref=finding_ref,
                ).model_dump(mode="json"),
                StableId(f"{attempt.attempt_id.root}.gap-blocked"),
                artifact_refs=(finding_ref,),
            )
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_ATTEMPT_SETTLED,
                TaskAttemptSettledPayload(
                    attempt_id=attempt.attempt_id,
                    outcome=AttemptOutcome.FAILED,
                    task_status=TaskStatus.BLOCKED,
                    failure_class=FailureClass.CANON_EXTRACTION_GAP,
                    block_cause=FailureClass.CANON_EXTRACTION_GAP.value,
                    terminal_artifact_refs=(finding_ref,),
                    ended_at=now,
                ).model_dump(mode="json"),
                StableId(f"{attempt.attempt_id.root}.settled"),
                artifact_refs=(finding_ref,),
            )
            self._append(
                session,
                maintenance_task.run_id,
                maintenance_task.task_id,
                RunEventType.RUNTIME_TASK_CREATED,
                TaskCreatedPayload(task=maintenance_task).model_dump(mode="json"),
                _task_created_identity(maintenance_task.task_id),
                artifact_refs=(finding_ref,),
            )
            self._insert_task(session, maintenance_task, now)
            return maintenance_task

    def settle_maintenance_and_retry_planner(
        self,
        fence: AttemptFence,
        *,
        workflow_result_ref: ArtifactRef,
        artifact_refs: tuple[ArtifactRef, ...] = (),
        retry_task: TaskRecord,
    ) -> TaskRecord:
        """Settle a committed Memory repair and atomically create the new-basis Planner task."""

        workflow_result = self._load_memory_write_result(workflow_result_ref)
        settlement_refs = tuple(dict.fromkeys((workflow_result_ref, *artifact_refs)))
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            task = self._load_task(session, fence.task_id, lock=True)
            if (
                task.kind is not TaskKind.MAINTENANCE
                or task.purpose is not TaskPurpose.DERIVED_MAINTENANCE
            ):
                raise RuntimeCommandConflictError("only derived maintenance may retry a Planner")
            if not task.input_artifact_refs:
                raise RuntimeCommandConflictError("maintenance task is missing its finding")
            finding = self._load_memory_repair_finding(task.input_artifact_refs[0])
            planner = self._load_task(session, finding.planner_task_id, lock=True)
            expected_retry_id = self._planner_retry_task_id(planner, workflow_result)
            if retry_task.task_id != expected_retry_id:
                raise RuntimeCommandConflictError("Planner retry identity is not basis-bound")
            existing = session.get(RuntimeTaskProjectionRow, retry_task.task_id.root)
            if task.status is TaskStatus.SUCCEEDED and task.current_attempt_id is None:
                if existing is None:
                    raise RuntimeCommandConflictError(
                        "settled maintenance is missing its Planner retry task"
                    )
                restored = TaskRecord.model_validate_json(json.dumps(existing.task_json))
                if restored != retry_task or not planner.superseded:
                    raise RuntimeCommandConflictError("Planner retry identity collision")
                return restored

            task, attempt = self._require_fence(session, fence)
            self._validate_gap_finding(planner, None, finding)
            project = session.get(ProjectRow, task.project_id.root)
            if project is None or project.current_commit_id is None:
                raise RuntimeCommandConflictError("project has no current commit")
            self._validate_committed_repair(
                task,
                planner,
                finding,
                workflow_result,
                workflow_result_ref=workflow_result_ref,
                retry_task=retry_task,
                current_commit=CommitId(project.current_commit_id),
            )
            if existing is not None:
                restored = TaskRecord.model_validate_json(json.dumps(existing.task_json))
                if restored != retry_task:
                    raise RuntimeCommandConflictError("Planner retry task identity collision")
                raise RuntimeCommandConflictError("Planner retry task exists before settlement")

            settled_attempt = attempt.model_copy(
                update={"ended_at": now, "outcome": AttemptOutcome.SUCCEEDED, "failure_class": None}
            )
            settled = task.model_copy(
                update={
                    "task_revision": task.task_revision + 1,
                    "status": TaskStatus.SUCCEEDED,
                    "current_attempt_id": None,
                    "terminal_artifact_refs": settlement_refs,
                    "block_cause": None,
                }
            )
            superseded_planner = planner.model_copy(
                update={
                    "task_revision": planner.task_revision + 1,
                    "status": TaskStatus.CANCELLED,
                    "superseded": True,
                    "block_cause": "canon extraction maintenance committed",
                }
            )
            self._update_attempt(session, settled_attempt)
            self._update_task(session, settled, now)
            self._update_task(session, superseded_planner, now)
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_ATTEMPT_SETTLED,
                TaskAttemptSettledPayload(
                    attempt_id=attempt.attempt_id,
                    outcome=AttemptOutcome.SUCCEEDED,
                    task_status=TaskStatus.SUCCEEDED,
                    terminal_artifact_refs=settlement_refs,
                    ended_at=now,
                ).model_dump(mode="json"),
                StableId(f"{attempt.attempt_id.root}.settled"),
                artifact_refs=settlement_refs,
            )
            self._append(
                session,
                planner.run_id,
                planner.task_id,
                RunEventType.RUNTIME_CONTROL_RECORDED,
                ControlIntentPayload(
                    command_id=_bounded_runtime_identity(
                        f"supersede.{task.task_id.root}",
                        f"supersede.{task.run_id.root}.{task.task_revision}",
                    ),
                    action="supersede",
                    actor_id="memory-maintenance",
                    reason="canon extraction maintenance committed on a new basis",
                ).model_dump(mode="json"),
                _bounded_runtime_identity(
                    f"{task.task_id.root}.planner-superseded",
                    f"{attempt.attempt_id.root}.planner-superseded",
                ),
                artifact_refs=(finding.planner_intent_ref, workflow_result_ref),
            )
            self._append(
                session,
                retry_task.run_id,
                retry_task.task_id,
                RunEventType.RUNTIME_TASK_CREATED,
                TaskCreatedPayload(task=retry_task).model_dump(mode="json"),
                _task_created_identity(retry_task.task_id),
                artifact_refs=(finding.planner_intent_ref, workflow_result_ref),
            )
            self._insert_task(session, retry_task, now)
            return retry_task

    def _load_memory_repair_finding(self, finding_ref: ArtifactRef) -> MemoryRepairFinding:
        if self._artifacts is None:
            raise RuntimeCommandConflictError(
                "memory repair commands require an ArtifactRepository"
            )
        try:
            return MemoryRepairFinding.model_validate_json(
                self._artifacts.read_verified(finding_ref)
            )
        except (ValueError, RuntimeError) as error:
            raise RuntimeCommandConflictError(
                "finding artifact is not a valid memory repair finding"
            ) from error

    def _load_memory_write_result(self, result_ref: ArtifactRef) -> MemoryWriteWorkflowResult:
        if self._artifacts is None:
            raise RuntimeCommandConflictError(
                "memory repair commands require an ArtifactRepository"
            )
        try:
            return MemoryWriteWorkflowResult.model_validate_json(
                self._artifacts.read_verified(result_ref)
            )
        except (ValueError, RuntimeError) as error:
            raise RuntimeCommandConflictError("workflow result artifact is invalid") from error

    @staticmethod
    def _maintenance_task_id(finding: MemoryRepairFinding) -> TaskId:
        owner = finding.repair_owner.value
        value = _bounded_runtime_identity(
            f"maintenance.{finding.finding_id.root}.{owner}",
            f"maintenance.{finding.incident_id.root}.{finding.planner_attempt_id.root}.{owner}",
            f"maintenance.{finding.no_progress_key.root}.{finding.planner_attempt_id.root}.{owner}",
            f"maintenance.{finding.planner_task_id.root}.{finding.planner_attempt_id.root}.{owner}",
        )
        return TaskId(value.root)

    @staticmethod
    def _planner_retry_task_id(
        planner: TaskRecord, workflow_result: MemoryWriteWorkflowResult
    ) -> TaskId:
        if workflow_result.resulting_commit is None:  # pragma: no cover - validated by caller
            raise RuntimeCommandConflictError("committed repair has no resulting commit")
        digest = workflow_result.resulting_commit.root.removeprefix("sha256:")[:32]
        value = _bounded_runtime_identity(
            f"{planner.task_id.root}.retry.{digest}",
            f"{planner.run_id.root}.retry.{digest}",
            f"retry.{digest}",
        )
        return TaskId(value.root)

    @staticmethod
    def _validate_gap_finding(
        planner: TaskRecord,
        attempt: TaskAttempt | None,
        finding: MemoryRepairFinding,
    ) -> None:
        if planner.kind is not TaskKind.PLAN_CANDIDATE:
            raise RuntimeCommandConflictError(
                "memory repair finding must originate at a Planner task"
            )
        if finding.classification is not MemoryGapClassification.CANON_EXTRACTION_GAP:
            raise RuntimeCommandConflictError("only CANON_EXTRACTION_GAP creates maintenance")
        if (
            finding.planner_run_id != planner.run_id
            or finding.planner_task_id != planner.task_id
            or finding.project_id != planner.project_id
            or finding.base_commit != planner.basis_commit
            or finding.basis_snapshot_id != planner.basis_snapshot
        ):
            raise RuntimeCommandConflictError("memory repair finding does not match Planner basis")
        if attempt is not None and finding.planner_attempt_id != attempt.attempt_id:
            raise RuntimeCommandConflictError(
                "memory repair finding does not match Planner attempt"
            )

    @staticmethod
    def _validate_maintenance_task(
        planner: TaskRecord,
        maintenance: TaskRecord,
        *,
        finding_ref: ArtifactRef,
    ) -> None:
        if (
            maintenance.kind is not TaskKind.MAINTENANCE
            or maintenance.purpose is not TaskPurpose.DERIVED_MAINTENANCE
            or maintenance.status is not TaskStatus.READY
            or maintenance.task_revision != 0
            or maintenance.current_attempt_id is not None
        ):
            raise RuntimeCommandConflictError("maintenance task is not an unclaimed derived task")
        if (
            maintenance.run_id != planner.run_id
            or maintenance.project_id != planner.project_id
            or maintenance.policy_hash != planner.policy_hash
            or maintenance.permission_hash != planner.permission_hash
            or maintenance.basis_commit != planner.basis_commit
            or maintenance.basis_snapshot != planner.basis_snapshot
        ):
            raise RuntimeCommandConflictError("maintenance task differs from Planner owner")
        if maintenance.input_artifact_refs != (finding_ref,):
            raise RuntimeCommandConflictError("maintenance task must carry exactly its finding")
        if maintenance.dependency_task_ids:
            raise RuntimeCommandConflictError(
                "maintenance task must not depend on the blocked Planner"
            )

    def _validate_committed_repair(
        self,
        maintenance: TaskRecord,
        planner: TaskRecord,
        finding: MemoryRepairFinding,
        workflow_result: MemoryWriteWorkflowResult,
        *,
        workflow_result_ref: ArtifactRef,
        retry_task: TaskRecord,
        current_commit: CommitId,
    ) -> None:
        if planner.status is not TaskStatus.BLOCKED or planner.superseded:
            raise RuntimeCommandConflictError("Planner must be blocked and not yet superseded")
        if workflow_result.status is not MemoryWriteWorkflowStatus.COMMITTED:
            raise RuntimeCommandConflictError("Planner retry requires a committed memory repair")
        if (
            not workflow_result.canonical_commit_accepted
            or workflow_result.resulting_commit is None
        ):
            raise RuntimeCommandConflictError("Planner retry requires an accepted resulting commit")
        if workflow_result.base_commit != maintenance.basis_commit:
            raise RuntimeCommandConflictError("workflow result base differs from maintenance basis")
        if workflow_result.projection_snapshot_id is None or workflow_result.freshness is None:
            raise RuntimeCommandConflictError("committed repair is missing projection freshness")
        if workflow_result.freshness.canonical_commit != workflow_result.resulting_commit:
            raise RuntimeCommandConflictError("workflow freshness is bound to another commit")
        if current_commit != workflow_result.resulting_commit:
            raise RuntimeCommandConflictError(
                "resulting repair commit is not the current project commit"
            )
        if planner.basis_snapshot is not None and (
            planner.basis_snapshot == workflow_result.projection_snapshot_id
        ):
            raise RuntimeCommandConflictError("Planner retry must establish a new basis namespace")
        expected = planner.model_copy(
            update={
                "task_id": self._planner_retry_task_id(planner, workflow_result),
                "task_revision": 0,
                "status": TaskStatus.READY,
                "basis_commit": workflow_result.resulting_commit,
                "basis_snapshot": workflow_result.projection_snapshot_id,
                "dependency_task_ids": (maintenance.task_id,),
                "terminal_artifact_refs": (),
                "block_cause": None,
                "superseded": False,
            }
        )
        if retry_task != expected:
            raise RuntimeCommandConflictError("Planner retry does not preserve task intent")
        self._validate_gap_finding(planner, None, finding)
        if workflow_result_ref in retry_task.input_artifact_refs:
            raise RuntimeCommandConflictError("Planner retry must not inject workflow output")

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
                _bounded_runtime_identity(
                    f"{receipt.effect_identity.root}.requested",
                    f"{attempt.attempt_id.root}.effect-requested",
                ),
            )
            return receipt

    def effect_for_current_attempt(
        self, task_id: TaskId, *, external_system: str
    ) -> EffectReceipt | None:
        """Read the outer effect owned by a task's still-current Attempt."""

        with self._session_factory() as session:
            task = self._load_task(session, task_id, lock=False)
            if task.current_attempt_id is None:
                return None
            row = session.scalar(
                select(RuntimeEffectProjectionRow).where(
                    RuntimeEffectProjectionRow.task_id == task_id.root,
                    RuntimeEffectProjectionRow.attempt_id == task.current_attempt_id.root,
                )
            )
            if row is None:
                return None
            receipt = EffectReceipt.model_validate_json(json.dumps(row.effect_json))
            if receipt.external_system != external_system:
                return None
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
                _bounded_runtime_identity(
                    f"{receipt.effect_identity.root}.{receipt.status.value}",
                    f"{attempt.attempt_id.root}.effect-{receipt.status.value}",
                ),
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
        failure_class: FailureClass | None = None,
        observed_revision: int | None = None,
        artifact_refs: tuple[ArtifactRef, ...] = (),
    ) -> TaskRecord:
        if terminal_status not in {
            TaskStatus.WAITING_RETRY,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
        }:
            raise ValueError("operator reconciliation may only retry, block, or cancel")
        settlement_refs = tuple(dict.fromkeys(artifact_refs))
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
            outcome = {
                TaskStatus.CANCELLED: AttemptOutcome.CANCELLED,
                TaskStatus.BLOCKED: AttemptOutcome.FAILED,
                TaskStatus.WAITING_RETRY: AttemptOutcome.SUSPENDED,
            }[terminal_status]
            failure = failure_class or (
                FailureClass.CANCELLED
                if terminal_status is TaskStatus.CANCELLED
                else FailureClass.WORKER_STARTUP
            )
            settled_attempt = attempt.model_copy(
                update={"ended_at": now, "outcome": outcome, "failure_class": failure}
            )
            consumes_budget = failure_policy(failure).consumes_task_budget
            remaining = task.failure_budget - (1 if consumes_budget else 0)
            settled_status = (
                TaskStatus.BUDGET_REVIEW
                if terminal_status is TaskStatus.WAITING_RETRY and remaining <= 0
                else terminal_status
            )
            settled_task = task.model_copy(
                update={
                    "task_revision": task.task_revision + 1,
                    "status": settled_status,
                    "current_attempt_id": None,
                    "terminal_artifact_refs": tuple(
                        dict.fromkeys((*task.terminal_artifact_refs, *settlement_refs))
                    ),
                    "failure_budget": max(0, remaining),
                    "paused": settled_status is TaskStatus.CANCELLED,
                    "block_cause": (
                        reason if settled_status is TaskStatus.BLOCKED else task.block_cause
                    ),
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
                artifact_refs=settlement_refs,
            )
            if settled_status is TaskStatus.BLOCKED:
                self._append(
                    session,
                    task.run_id,
                    task.task_id,
                    RunEventType.RUNTIME_TASK_BLOCKED,
                    TaskBlockedPayload(
                        failure_class=failure,
                        cause_fingerprint=_digest(reason),
                        sanitized_message=reason[:512],
                    ).model_dump(mode="json"),
                    _bounded_runtime_identity(
                        f"blocked.{command_id.root}",
                        f"{attempt.attempt_id.root}.operator-blocked",
                    ),
                    artifact_refs=settlement_refs,
                )
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_ATTEMPT_SETTLED,
                TaskAttemptSettledPayload(
                    attempt_id=attempt.attempt_id,
                    outcome=outcome,
                    task_status=settled_status,
                    failure_class=failure,
                    terminal_artifact_refs=settlement_refs,
                    ended_at=now,
                ).model_dump(mode="json"),
                _bounded_runtime_identity(
                    f"{command_id.root}.attempt-settled",
                    f"{attempt.attempt_id.root}.operator-settled",
                ),
                artifact_refs=settlement_refs,
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
                _bounded_runtime_identity(
                    f"{checkpoint.checkpoint_id.root}.saved",
                    f"{attempt.attempt_id.root}.checkpoint-saved.{checkpoint.event_position}",
                ),
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
            if action == "retry" and task.failure_budget <= 0:
                status = TaskStatus.BUDGET_REVIEW
            if action == "pause" and task.current_attempt_id is None:
                status = TaskStatus.PENDING
            if action == "cancel":
                status = (
                    TaskStatus.CANCELLED
                    if task.current_attempt_id is None
                    else TaskStatus.RECOVERY_PENDING
                )
            writer_generation_after = (
                0
                if (
                    action == "retry"
                    and task.kind in {TaskKind.PLAN_COMMIT, TaskKind.DRAFT_COMMIT}
                    and task.writer_generation > 0
                )
                else None
            )
            updated = task.model_copy(
                update={
                    "task_revision": task.task_revision + 1,
                    "status": status,
                    "paused": action in {"pause", "cancel"},
                    "cancel_requested": action == "cancel",
                    "writer_generation": (
                        task.writer_generation
                        if writer_generation_after is None
                        else writer_generation_after
                    ),
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
                    action=action,
                    actor_id=actor_id,
                    reason=reason,
                    writer_generation_after=writer_generation_after,
                ).model_dump(mode="json"),
                command_id,
            )
            return updated

    def extend_budget(
        self,
        task_id: TaskId,
        *,
        command_id: StableId,
        actor_id: str,
        reason: str,
        additional_attempts: int = 0,
        additional_planner_memory_tranches: int = 0,
        observed_revision: int | None = None,
    ) -> TaskRecord:
        """Add an explicit retry tranche to budget-waiting work and make it claimable."""

        if (
            isinstance(additional_attempts, bool)
            or isinstance(additional_planner_memory_tranches, bool)
            or additional_attempts < 0
            or additional_planner_memory_tranches < 0
            or not (additional_attempts or additional_planner_memory_tranches)
        ):
            raise ValueError("budget extension must add a retry or Planner Memory tranche")
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            task = self._load_task(session, task_id, lock=True)
            self._require_observed_revision(task, observed_revision)
            if task.status is not TaskStatus.BUDGET_REVIEW or task.current_attempt_id is not None:
                raise RuntimeCommandConflictError(
                    "budget extension requires inactive BUDGET_REVIEW work"
                )
            if task.failure_budget + additional_attempts <= 0:
                raise RuntimeCommandConflictError(
                    "budget extension must leave the task with a positive retry budget"
                )
            updated = task.model_copy(
                update={
                    "task_revision": task.task_revision + 1,
                    "status": TaskStatus.READY,
                    "failure_budget": task.failure_budget + additional_attempts,
                    "planner_memory_budget_extensions": (
                        task.planner_memory_budget_extensions + additional_planner_memory_tranches
                    ),
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
                    action="extend_budget",
                    actor_id=actor_id,
                    reason=reason,
                    additional_attempts=additional_attempts,
                    additional_planner_memory_tranches=(additional_planner_memory_tranches),
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
            writer_generation_after = (
                0
                if (
                    task.kind in {TaskKind.PLAN_COMMIT, TaskKind.DRAFT_COMMIT}
                    and task.writer_generation > 0
                )
                else None
            )
            updated = task.model_copy(
                update={
                    "task_revision": task.task_revision + 1,
                    "status": (
                        TaskStatus.READY if task.failure_budget > 0 else TaskStatus.BUDGET_REVIEW
                    ),
                    "block_cause": None,
                    "writer_generation": (
                        task.writer_generation
                        if writer_generation_after is None
                        else writer_generation_after
                    ),
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
                    writer_generation_after=writer_generation_after,
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
            if (
                task.kind is TaskKind.MAINTENANCE
                and task.status is TaskStatus.WAITING_INPUT
                and not task.paused
            ):
                self._require_waiting_input_checkpoint(task)
            elif not task.paused or task.current_attempt_id is not None:
                raise RuntimeCommandConflictError(
                    "resume requires a paused task without active attempt"
                )
            updated = task.model_copy(
                update={
                    "task_revision": task.task_revision + 1,
                    "status": (
                        TaskStatus.READY if task.failure_budget > 0 else TaskStatus.BUDGET_REVIEW
                    ),
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

    def _require_waiting_input_checkpoint(self, task: TaskRecord) -> None:
        if self._artifacts is None:
            raise RuntimeCommandConflictError("maintenance resume requires an ArtifactRepository")
        result_refs = tuple(
            reference
            for reference in task.terminal_artifact_refs
            if reference.media_type == MEMORY_WRITE_RESULT_MEDIA_TYPE
        )
        if len(result_refs) != 1:
            raise RuntimeCommandConflictError(
                "maintenance resume requires one workflow result artifact"
            )
        try:
            result = MemoryWriteWorkflowResult.model_validate_json(
                self._artifacts.read_verified(result_refs[0]), strict=True
            )
        except (KeyError, RuntimeError, ValueError) as error:
            raise RuntimeCommandConflictError(
                "maintenance resume workflow result is invalid"
            ) from error
        if result.status is not MemoryWriteWorkflowStatus.HUMAN_REQUIRED:
            raise RuntimeCommandConflictError(
                "maintenance resume requires a HUMAN_REQUIRED workflow result"
            )
        if result.checkpoint_ref is None or result.base_commit != task.basis_commit:
            raise RuntimeCommandConflictError(
                "maintenance resume requires a basis-bound workflow checkpoint"
            )

    def claim_writer_lane(self, fence: AttemptFence) -> AttemptFence:
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            task, attempt = self._require_fence(session, fence, require_writer_lane=False)
            if task.kind not in {
                TaskKind.PLAN_COMMIT,
                TaskKind.DRAFT_COMMIT,
                TaskKind.MAINTENANCE,
            }:
                raise RuntimeCommandConflictError(
                    "only commit tasks or maintenance tasks may claim the writer lane"
                )
            row = session.scalar(
                select(ProjectWriterClaimRow)
                .where(ProjectWriterClaimRow.project_id == task.project_id.root)
                .with_for_update()
            )
            if row is not None:
                if row.task_id == task.task_id.root and row.attempt_id == attempt.attempt_id.root:
                    # Repeated calls from the same owner are idempotent.  Do
                    # not advance the generation or emit a second claim event
                    # for the same active attempt.
                    return fence.model_copy(update={"writer_generation": row.generation})
                owner = session.get(RuntimeTaskProjectionRow, row.task_id)
                if (
                    owner is not None
                    and owner.status == TaskStatus.RUNNING.value
                    and owner.current_attempt_id == row.attempt_id
                ):
                    raise WriterLaneBusyError(
                        "project writer lane is already held by an active attempt"
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
        *,
        successor_tasks: tuple[TaskRecord, ...] = (),
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
            if result.status is CommitStatus.ACCEPTED:
                assert result.commit_id is not None
                self._insert_successor_tasks(
                    session,
                    settled_task,
                    successor_tasks,
                    now,
                    expected_basis=result.commit_id,
                )
            return result

    def record_external_commit(
        self,
        fence: AttemptFence,
        commit_id: CommitId,
        *,
        commits: CommitService,
        artifact_refs: tuple[ArtifactRef, ...] = (),
        effect_receipt: EffectReceipt | None = None,
        successor_tasks: tuple[TaskRecord, ...] = (),
    ) -> TaskRecord:
        """Settle a fenced Draft commit already accepted by Stage 2W's atomic owner."""

        manifest = commits.load_manifest(commit_id)
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            task, attempt = self._require_fence(session, fence)
            return self._settle_external_commit_in_session(
                session,
                task,
                attempt,
                commit_id=commit_id,
                manifest=manifest,
                artifact_refs=artifact_refs,
                effect_receipt=effect_receipt,
                successor_tasks=successor_tasks,
                now=now,
            )

    def reconcile_external_commit(
        self,
        task_id: TaskId,
        commit_id: CommitId,
        *,
        commits: CommitService,
        effect_receipt: EffectReceipt,
        successor_tasks: tuple[TaskRecord, ...],
        artifact_refs: tuple[ArtifactRef, ...] = (),
        observed_revision: int | None = None,
    ) -> TaskRecord:
        """Finish a proven Stage 2W commit after the worker fence was lost."""

        manifest = commits.load_manifest(commit_id)
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            task = self._load_task(session, task_id, lock=True)
            self._require_observed_revision(task, observed_revision)
            if task.current_attempt_id is None:
                raise RuntimeCommandConflictError("task has no interrupted external Attempt")
            row = session.get(RuntimeTaskAttemptRow, task.current_attempt_id.root)
            if row is None:
                raise RuntimeCommandConflictError("interrupted Attempt projection is missing")
            attempt = TaskAttempt.model_validate_json(json.dumps(row.attempt_json))
            return self._settle_external_commit_in_session(
                session,
                task,
                attempt,
                commit_id=commit_id,
                manifest=manifest,
                artifact_refs=artifact_refs,
                effect_receipt=effect_receipt,
                successor_tasks=successor_tasks,
                now=now,
            )

    def _settle_external_commit_in_session(
        self,
        session: Session,
        task: TaskRecord,
        attempt: TaskAttempt,
        *,
        commit_id: CommitId,
        manifest: RootManifest,
        artifact_refs: tuple[ArtifactRef, ...],
        effect_receipt: EffectReceipt | None,
        successor_tasks: tuple[TaskRecord, ...],
        now: datetime,
    ) -> TaskRecord:
        if task.kind is not TaskKind.DRAFT_COMMIT:
            raise RuntimeCommandConflictError(
                "only a Draft commit task may record Chapter Settlement"
            )
        project = session.scalar(
            select(ProjectRow)
            .where(ProjectRow.project_id == task.project_id.root)
            .with_for_update()
        )
        if (
            project is None
            or project.current_commit_id != commit_id.root
            or manifest.project_id != task.project_id
            or manifest.parent_commit_ids != (task.basis_commit,)
        ):
            raise RuntimeCommandConflictError(
                "Chapter Settlement commit does not advance the fenced task basis"
            )
        if effect_receipt is not None:
            if (
                effect_receipt.status is not EffectStatus.COMPLETED
                or effect_receipt.provider_request_id != commit_id.root
            ):
                raise RuntimeCommandConflictError(
                    "Chapter Settlement effect does not prove the accepted commit"
                )
            effect_row = session.get(
                RuntimeEffectProjectionRow, effect_receipt.effect_identity.root
            )
            if effect_row is None or effect_row.attempt_id != attempt.attempt_id.root:
                raise RuntimeCommandConflictError(
                    "Chapter Settlement effect belongs to another Attempt"
                )
            prior = EffectReceipt.model_validate_json(json.dumps(effect_row.effect_json))
            if (
                prior.request_identity != effect_receipt.request_identity
                or prior.external_system != effect_receipt.external_system
                or prior.attempt_no != effect_receipt.attempt_no
            ):
                raise RuntimeCommandConflictError("Chapter Settlement effect identity changed")
            effect_row.status = effect_receipt.status.value
            effect_row.provider_request_id = effect_receipt.provider_request_id
            effect_row.result_ref_json = (
                None
                if effect_receipt.result_artifact_ref is None
                else effect_receipt.result_artifact_ref.model_dump(mode="json")
            )
            effect_row.effect_json = effect_receipt.model_dump(mode="json")
            effect_refs = (
                ()
                if effect_receipt.result_artifact_ref is None
                else (effect_receipt.result_artifact_ref,)
            )
            self._append(
                session,
                task.run_id,
                task.task_id,
                RunEventType.RUNTIME_EFFECT_TERMINAL,
                EffectTerminalPayload(
                    effect=effect_receipt,
                    task_id=task.task_id,
                    attempt_id=attempt.attempt_id,
                ).model_dump(mode="json"),
                _bounded_runtime_identity(
                    f"{effect_receipt.effect_identity.root}.{effect_receipt.status.value}",
                    f"{attempt.attempt_id.root}.effect-{effect_receipt.status.value}",
                ),
                artifact_refs=effect_refs,
            )
        settled_attempt = attempt.model_copy(
            update={"ended_at": now, "outcome": AttemptOutcome.SUCCEEDED}
        )
        settled_task = task.model_copy(
            update={
                "task_revision": task.task_revision + 1,
                "status": TaskStatus.SUCCEEDED,
                "current_attempt_id": None,
                "terminal_artifact_refs": artifact_refs,
            }
        )
        self._update_attempt(session, settled_attempt)
        self._update_task(session, settled_task, now)
        self._append(
            session,
            task.run_id,
            task.task_id,
            RunEventType.COMMIT_ACCEPTED,
            {
                "commit_id": commit_id.root,
                "owner": "stage2w.chapter_reveal_atomic",
            },
            StableId(f"{attempt.attempt_id.root}.chapter-settlement"),
            artifact_refs=artifact_refs,
        )
        self._append(
            session,
            task.run_id,
            task.task_id,
            RunEventType.RUNTIME_ATTEMPT_SETTLED,
            TaskAttemptSettledPayload(
                attempt_id=attempt.attempt_id,
                outcome=AttemptOutcome.SUCCEEDED,
                task_status=TaskStatus.SUCCEEDED,
                terminal_artifact_refs=artifact_refs,
                ended_at=now,
            ).model_dump(mode="json"),
            StableId(f"{attempt.attempt_id.root}.settled"),
            artifact_refs=artifact_refs,
        )
        self._insert_successor_tasks(
            session,
            settled_task,
            successor_tasks,
            now,
            expected_basis=commit_id,
        )
        return settled_task

    def _require_fence(
        self,
        session: Session,
        fence: AttemptFence,
        *,
        require_writer_lane: bool = True,
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
            require_writer_lane
            and task.kind in {TaskKind.PLAN_COMMIT, TaskKind.DRAFT_COMMIT}
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

    def _insert_successor_tasks(
        self,
        session: Session,
        predecessor: TaskRecord,
        successors: tuple[TaskRecord, ...],
        now: datetime,
        *,
        expected_basis: CommitId | None = None,
    ) -> None:
        """Create fixed-topology successors inside the predecessor's transaction."""

        if not successors:
            return
        if predecessor.status is not TaskStatus.SUCCEEDED:
            raise RuntimeCommandConflictError("only a succeeded task may create successors")
        allowed_kinds = {
            TaskKind.PLAN_CANDIDATE: {TaskKind.PLAN_ACCEPTANCE},
            TaskKind.DRAFT_CANDIDATE: {TaskKind.DRAFT_ACCEPTANCE},
            TaskKind.PLAN_ACCEPTANCE: {TaskKind.PLAN_COMMIT},
            TaskKind.DRAFT_ACCEPTANCE: {TaskKind.DRAFT_COMMIT},
            TaskKind.PLAN_COMMIT: {TaskKind.PROJECTION_FRESHNESS},
            TaskKind.DRAFT_COMMIT: {TaskKind.PROJECTION_FRESHNESS},
            TaskKind.PROJECTION_FRESHNESS: {
                TaskKind.PLAN_CANDIDATE,
                TaskKind.DRAFT_CANDIDATE,
            },
        }.get(predecessor.kind, set())
        basis = expected_basis or predecessor.basis_commit
        seen_ids: set[str] = set()
        for successor in successors:
            if successor.task_id.root in seen_ids:
                raise RuntimeCommandConflictError("successor task identity is duplicated")
            seen_ids.add(successor.task_id.root)
            if successor.kind not in allowed_kinds:
                raise RuntimeCommandConflictError("successor skips the fixed runtime topology")
            if (
                successor.run_id != predecessor.run_id
                or successor.project_id != predecessor.project_id
                or successor.policy_hash != predecessor.policy_hash
                or successor.permission_hash != predecessor.permission_hash
            ):
                raise RuntimeCommandConflictError("successor differs from its runtime owner")
            if successor.basis_commit != basis:
                raise RuntimeCommandConflictError("successor differs from its settled basis")
            if predecessor.task_id not in successor.dependency_task_ids:
                raise RuntimeCommandConflictError("successor does not depend on its predecessor")
            if successor.task_revision != 0 or successor.current_attempt_id is not None:
                raise RuntimeCommandConflictError("successor must be an unclaimed initial task")
            if successor.status not in {
                TaskStatus.PENDING,
                TaskStatus.READY,
                TaskStatus.WAITING_INPUT,
            }:
                raise RuntimeCommandConflictError("successor has an invalid initial status")
            for dependency in successor.dependency_task_ids:
                if session.get(RuntimeTaskProjectionRow, dependency.root) is None:
                    raise RuntimeCommandConflictError("successor dependency does not exist")
            existing = session.get(RuntimeTaskProjectionRow, successor.task_id.root)
            if existing is not None:
                restored = TaskRecord.model_validate_json(json.dumps(existing.task_json))
                if restored != successor:
                    raise RuntimeCommandConflictError("successor task identity collision")
                continue
            self._append(
                session,
                successor.run_id,
                successor.task_id,
                RunEventType.RUNTIME_TASK_CREATED,
                TaskCreatedPayload(task=successor).model_dump(mode="json"),
                _task_created_identity(successor.task_id),
            )
            self._insert_task(session, successor, now)

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
            trace_id=_bounded_runtime_identity(f"runtime.{run_id.root}", run_id.root).root,
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
                heartbeat_at=attempt.heartbeat_at or attempt.claimed_at,
                lease_expires_at=attempt.lease_expires_at or attempt.claimed_at,
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
        row.heartbeat_at = attempt.heartbeat_at or attempt.claimed_at
        row.lease_expires_at = attempt.lease_expires_at or attempt.claimed_at
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
    "WriterLaneBusyError",
    "bounded_runtime_identity",
]
