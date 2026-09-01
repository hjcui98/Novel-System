"""Deterministic Stage 5 maintenance pre-checks and supervisor findings."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.models import (
    ProjectionOutboxRow,
    RuntimeEffectProjectionRow,
    RuntimeTaskAttemptRow,
    RuntimeTaskProjectionRow,
)
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ProjectId, StableId, TaskId
from novel_agent.domain.runtime import EffectStatus, FailureClass, TaskRecord, TaskStatus


class MaintenanceKind(StrEnum):
    RECONCILE_PROJECTION_FRESHNESS = "reconcile_projection_freshness"
    AUDIT_RUNTIME_PROJECTION = "audit_runtime_projection"
    RECONCILE_UNCERTAIN_EFFECTS = "reconcile_uncertain_effects"
    VERIFY_ARTIFACT_REFERENCES = "verify_artifact_references"
    REBUILD_CONTEXT_PROJECTION = "rebuild_context_projection"
    RUN_DELAYED_EVALUATION = "run_delayed_evaluation"
    AUDIT_STUCK_OR_POISON_TASKS = "audit_stuck_or_poison_tasks"


class MaintenanceDisposition(StrEnum):
    NO_WORK = "NO_WORK"
    WORK_REQUIRED = "WORK_REQUIRED"


class MaintenanceCommand(DomainModel):
    command_id: StableId
    kind: MaintenanceKind
    project_id: ProjectId | None = None
    requested_at: datetime


class MaintenanceReceipt(DomainModel):
    command_id: StableId
    kind: MaintenanceKind
    disposition: MaintenanceDisposition
    item_count: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=512)
    model_requests_created: int = Field(default=0, ge=0, le=0)


class SupervisorFinding(DomainModel):
    finding_id: StableId
    task_id: TaskId
    code: str = Field(min_length=1, max_length=128)
    failure_class: FailureClass
    requires_operator: bool
    proposed_command: str | None = Field(default=None, max_length=64)


def _bounded_finding_id(
    task_id: TaskId,
    *,
    run_id: str,
    suffix: str,
) -> StableId:
    """Keep a supervisor finding addressable without truncating its task scope.

    A task identity is already globally unique in the runtime projection.  The
    readable finding prefix is preferred, then the existing run/revision
    scope, and finally the complete task identity itself when the task is at
    the public 128-character limit.  Supervisor emits at most one finding per
    task, so the final fallback remains unambiguous while ``code`` carries the
    diagnostic kind.
    """

    for value in (
        f"finding.{task_id.root}.{suffix}",
        f"finding.{run_id}.{suffix}",
        task_id.root,
    ):
        try:
            return StableId(value)
        except ValueError:
            continue
    raise ValueError("supervisor finding identity is too long")


class RuntimeMaintenanceService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def precheck(self, command: MaintenanceCommand) -> MaintenanceReceipt:
        with self._session_factory() as session:
            count = self._count(session, command)
        disposition = (
            MaintenanceDisposition.NO_WORK if count == 0 else MaintenanceDisposition.WORK_REQUIRED
        )
        return MaintenanceReceipt(
            command_id=command.command_id,
            kind=command.kind,
            disposition=disposition,
            item_count=count,
            reason="deterministic pre-check completed",
        )

    def expired_task_ids(self, *, now: datetime | None = None) -> tuple[TaskId, ...]:
        """Return lease-expired current Tasks for command-owner suspicion handling."""

        observed_at = now or datetime.now(UTC)
        with self._session_factory() as session:
            task_ids = tuple(
                session.scalars(
                    select(RuntimeTaskProjectionRow.task_id)
                    .join(
                        RuntimeTaskAttemptRow,
                        RuntimeTaskAttemptRow.attempt_id
                        == RuntimeTaskProjectionRow.current_attempt_id,
                    )
                    .where(
                        RuntimeTaskProjectionRow.status == TaskStatus.RUNNING.value,
                        RuntimeTaskAttemptRow.ended_at.is_(None),
                        RuntimeTaskAttemptRow.lease_expires_at < observed_at,
                    )
                )
            )
        return tuple(TaskId(item) for item in task_ids)

    @staticmethod
    def _count(session: Session, command: MaintenanceCommand) -> int:
        if command.kind is MaintenanceKind.RECONCILE_PROJECTION_FRESHNESS:
            statement = select(func.count(ProjectionOutboxRow.outbox_id)).where(
                ProjectionOutboxRow.status.in_(("pending", "failed"))
            )
            if command.project_id is not None:
                statement = statement.where(
                    ProjectionOutboxRow.project_id == command.project_id.root
                )
            return int(session.scalar(statement) or 0)
        if command.kind is MaintenanceKind.RECONCILE_UNCERTAIN_EFFECTS:
            return int(
                session.scalar(
                    select(func.count(RuntimeEffectProjectionRow.effect_identity)).where(
                        RuntimeEffectProjectionRow.status.in_(
                            (EffectStatus.REQUESTED.value, EffectStatus.UNCERTAIN.value)
                        )
                    )
                )
                or 0
            )
        if command.kind in {
            MaintenanceKind.AUDIT_RUNTIME_PROJECTION,
            MaintenanceKind.AUDIT_STUCK_OR_POISON_TASKS,
        }:
            return int(
                session.scalar(
                    select(func.count(RuntimeTaskProjectionRow.task_id)).where(
                        RuntimeTaskProjectionRow.status.in_(
                            (
                                TaskStatus.RUNNING.value,
                                TaskStatus.RECOVERY_PENDING.value,
                                TaskStatus.WAITING_RETRY.value,
                            )
                        )
                    )
                )
                or 0
            )
        return 0


class RuntimeSupervisor:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        stuck_after: timedelta = timedelta(hours=1),
    ) -> None:
        self._session_factory = session_factory
        self._stuck_after = stuck_after

    def inspect(self) -> tuple[SupervisorFinding, ...]:
        cutoff = datetime.now(UTC) - self._stuck_after
        findings: list[SupervisorFinding] = []
        with self._session_factory() as session:
            rows = tuple(
                session.scalars(
                    select(RuntimeTaskProjectionRow).where(
                        RuntimeTaskProjectionRow.status.in_(
                            (
                                TaskStatus.RUNNING.value,
                                TaskStatus.RECOVERY_PENDING.value,
                                TaskStatus.WAITING_RETRY.value,
                            )
                        ),
                        RuntimeTaskProjectionRow.updated_at < cutoff,
                    )
                )
            )
            budget_rows = tuple(
                session.scalars(
                    select(RuntimeTaskProjectionRow).where(
                        RuntimeTaskProjectionRow.status.not_in(
                            (
                                TaskStatus.SUCCEEDED.value,
                                TaskStatus.FAILED.value,
                                TaskStatus.CANCELLED.value,
                            )
                        )
                    )
                )
            )
            effect_rows = tuple(
                session.scalars(
                    select(RuntimeEffectProjectionRow).where(
                        RuntimeEffectProjectionRow.status.in_(
                            (EffectStatus.REQUESTED.value, EffectStatus.UNCERTAIN.value)
                        )
                    )
                )
            )
        for row in rows:
            failure = (
                FailureClass.EFFECT_UNCERTAIN
                if row.status == TaskStatus.RECOVERY_PENDING.value
                else FailureClass.POISON_LOOP
            )
            findings.append(
                SupervisorFinding(
                    finding_id=_bounded_finding_id(
                        TaskId(row.task_id),
                        run_id=row.run_id,
                        suffix=f"stuck.{row.revision}",
                    ),
                    task_id=TaskId(row.task_id),
                    code="runtime_task_stuck",
                    failure_class=failure,
                    requires_operator=True,
                    proposed_command="reconcile"
                    if failure is FailureClass.EFFECT_UNCERTAIN
                    else "pause",
                )
            )
        existing = {finding.task_id.root for finding in findings}
        for row in budget_rows:
            task = TaskRecord.model_validate_json(json.dumps(row.task_json))
            if task.failure_budget == 0 and task.task_id.root not in existing:
                findings.append(
                    SupervisorFinding(
                        finding_id=_bounded_finding_id(
                            task.task_id,
                            run_id=task.run_id.root,
                            suffix=f"budget.{row.revision}",
                        ),
                        task_id=task.task_id,
                        code="runtime_failure_budget_exhausted",
                        failure_class=FailureClass.BUDGET_EXHAUSTED,
                        requires_operator=True,
                        proposed_command="extend_budget",
                    )
                )
                existing.add(task.task_id.root)
        for effect in effect_rows:
            if effect.task_id not in existing:
                findings.append(
                    SupervisorFinding(
                        finding_id=_bounded_finding_id(
                            TaskId(effect.task_id),
                            run_id=effect.run_id,
                            suffix="effect",
                        ),
                        task_id=TaskId(effect.task_id),
                        code="runtime_effect_unresolved",
                        failure_class=FailureClass.EFFECT_UNCERTAIN,
                        requires_operator=True,
                        proposed_command="reconcile",
                    )
                )
                existing.add(effect.task_id)
        return tuple(findings)


__all__ = [
    "MaintenanceCommand",
    "MaintenanceKind",
    "MaintenanceReceipt",
    "RuntimeMaintenanceService",
    "RuntimeSupervisor",
    "SupervisorFinding",
]
