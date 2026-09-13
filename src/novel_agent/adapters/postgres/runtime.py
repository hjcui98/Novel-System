"""Read/query adapter for rebuildable Stage 5 runtime projections."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.models import (
    ModelCallLedgerRow,
    ProjectRow,
    RuntimeEffectProjectionRow,
    RuntimeTaskAttemptRow,
    RuntimeTaskProjectionRow,
)
from novel_agent.domain.ids import ProjectId, RunId, TaskId
from novel_agent.domain.model_calls import ModelCallLedgerStatus
from novel_agent.domain.runtime import (
    EffectStatus,
    TaskAttempt,
    TaskRecord,
    TaskStatus,
)


class RuntimeTaskQueryRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @property
    def session_factory(self) -> sessionmaker[Session]:
        return self._session_factory

    def next_ready(
        self,
        *,
        project_id: ProjectId | None = None,
        run_id: RunId | None = None,
    ) -> TaskId | None:
        ready = self.ready_batch(limit=1, project_id=project_id, run_id=run_id)
        return None if not ready else ready[0].task_id

    def ready_batch(
        self,
        *,
        limit: int,
        project_id: ProjectId | None = None,
        run_id: RunId | None = None,
    ) -> tuple[TaskRecord, ...]:
        if limit < 1:
            raise ValueError("ready batch limit must be positive")
        now = datetime.now(UTC)
        with self._session_factory() as session:
            statement = (
                select(RuntimeTaskProjectionRow)
                .join(ProjectRow, ProjectRow.project_id == RuntimeTaskProjectionRow.project_id)
                .where(
                    RuntimeTaskProjectionRow.status == TaskStatus.READY.value,
                    RuntimeTaskProjectionRow.basis_commit == ProjectRow.current_commit_id,
                    (
                        RuntimeTaskProjectionRow.scheduled_for.is_(None)
                        | (RuntimeTaskProjectionRow.scheduled_for <= now)
                    ),
                )
            )
            if project_id is not None:
                statement = statement.where(RuntimeTaskProjectionRow.project_id == project_id.root)
            if run_id is not None:
                statement = statement.where(RuntimeTaskProjectionRow.run_id == run_id.root)
            rows = session.scalars(
                statement.order_by(
                    RuntimeTaskProjectionRow.priority.desc(),
                    RuntimeTaskProjectionRow.scheduled_for,
                    RuntimeTaskProjectionRow.task_id,
                ).limit(limit)
            )
            return tuple(TaskRecord.model_validate_json(json.dumps(row.task_json)) for row in rows)

    def list_run(self, run_id: RunId) -> tuple[TaskRecord, ...]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(RuntimeTaskProjectionRow)
                .where(RuntimeTaskProjectionRow.run_id == run_id.root)
                .order_by(RuntimeTaskProjectionRow.updated_at, RuntimeTaskProjectionRow.task_id)
            )
            return tuple(TaskRecord.model_validate_json(json.dumps(row.task_json)) for row in rows)

    def get_task(self, task_id: TaskId) -> TaskRecord:
        """One task by id, read from the projection the runtime already keeps."""

        with self._session_factory() as session:
            row = session.get(RuntimeTaskProjectionRow, task_id.root)
        if row is None:
            raise KeyError(f"unknown task {task_id.root}")
        return TaskRecord.model_validate_json(json.dumps(row.task_json))

    def last_settled_attempt(self, task_id: TaskId) -> TaskAttempt | None:
        """The most recent attempt that has settled, or ``None`` if none has.

        This is where a failure's canonical classification lives.  ``block_cause`` is
        only populated for a ``BLOCKED`` task, so a task waiting for a retry reports
        no cause at all while its own attempt row holds the real answer.
        """

        with self._session_factory() as session:
            row = session.scalars(
                select(RuntimeTaskAttemptRow)
                .where(
                    RuntimeTaskAttemptRow.task_id == task_id.root,
                    RuntimeTaskAttemptRow.ended_at.is_not(None),
                )
                .order_by(RuntimeTaskAttemptRow.attempt_no.desc())
                .limit(1)
            ).first()
        if row is None:
            return None
        return TaskAttempt.model_validate_json(json.dumps(row.attempt_json))

    def attempt_effect_ledger(
        self, task_id: TaskId
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """Provider sends for one task: unsettled, still outstanding, and answered.

        A send that happened and whose result is unknown is why a blind retry is
        unsafe, so a driver has to be able to see it.  The three groups are returned
        separately because they call for three different handlings.
        """

        with self._session_factory() as session:
            effect_rows = session.execute(
                select(
                    RuntimeEffectProjectionRow.effect_identity,
                    RuntimeEffectProjectionRow.status,
                ).where(RuntimeEffectProjectionRow.task_id == task_id.root)
            ).all()
            model_rows = session.execute(
                select(
                    ModelCallLedgerRow.request_id,
                    ModelCallLedgerRow.status,
                    ModelCallLedgerRow.raw_artifact_json,
                ).where(ModelCallLedgerRow.task_id == task_id.root)
            ).all()

        # One identity may be projected by both ledgers during recovery.  Keep the
        # strongest unresolved state so a completed effect can never hide a later
        # uncertain provider send.  A completed response is only a response reference;
        # rejected/incomplete/transport-terminal rows are deliberately not replayable.
        states: dict[str, tuple[int, str, str]] = {}

        def record(identity: str, priority: int, bucket: str, reference: str) -> None:
            current = states.get(identity)
            if current is None or priority > current[0]:
                states[identity] = (priority, bucket, reference)

        for effect_identity, status in effect_rows:
            if status == EffectStatus.REQUESTED.value:
                record(effect_identity, 2, "outstanding", effect_identity)
            elif status == EffectStatus.UNCERTAIN.value:
                record(effect_identity, 3, "unsettled", effect_identity)
            elif status == EffectStatus.COMPLETED.value:
                record(effect_identity, 1, "completed", effect_identity)
        for request_id, status, raw_artifact_json in model_rows:
            if status == ModelCallLedgerStatus.REQUESTED.value:
                record(request_id, 2, "outstanding", request_id)
            elif status == ModelCallLedgerStatus.UNCERTAIN.value:
                record(request_id, 3, "unsettled", request_id)
            elif status == ModelCallLedgerStatus.COMPLETED.value:
                response_ref = request_id
                if isinstance(raw_artifact_json, dict):
                    artifact_id = raw_artifact_json.get("artifact_id")
                    if isinstance(artifact_id, str) and artifact_id:
                        response_ref = artifact_id
                record(request_id, 1, "completed", response_ref)
        unsettled = [
            reference for _priority, bucket, reference in states.values() if bucket == "unsettled"
        ]
        outstanding = [
            reference for _priority, bucket, reference in states.values() if bucket == "outstanding"
        ]
        completed = [
            reference for _priority, bucket, reference in states.values() if bucket == "completed"
        ]
        return (
            tuple(sorted(unsettled)),
            tuple(sorted(outstanding)),
            tuple(sorted(completed)),
        )

    def next_scheduled_at(
        self,
        *,
        project_id: ProjectId | None = None,
        run_id: RunId | None = None,
        now: datetime | None = None,
    ) -> datetime | None:
        """Return the earliest future runnable schedule persisted in the projection."""

        times = self._future_scheduled_times(
            project_id=project_id,
            run_id=run_id,
            now=now,
        )
        return times[0] if times else None

    def future_scheduled_count(
        self,
        *,
        project_id: ProjectId | None = None,
        run_id: RunId | None = None,
        now: datetime | None = None,
    ) -> int:
        return len(
            self._future_scheduled_times(
                project_id=project_id,
                run_id=run_id,
                now=now,
            )
        )

    def _future_scheduled_times(
        self,
        *,
        project_id: ProjectId | None,
        run_id: RunId | None,
        now: datetime | None,
    ) -> tuple[datetime, ...]:
        observed_at = now or datetime.now(UTC)
        with self._session_factory() as session:
            statement = select(RuntimeTaskProjectionRow).where(
                RuntimeTaskProjectionRow.status == TaskStatus.READY.value,
                RuntimeTaskProjectionRow.scheduled_for.is_not(None),
                RuntimeTaskProjectionRow.scheduled_for > observed_at,
            )
            if project_id is not None:
                statement = statement.where(RuntimeTaskProjectionRow.project_id == project_id.root)
            if run_id is not None:
                statement = statement.where(RuntimeTaskProjectionRow.run_id == run_id.root)
            scheduled: list[datetime] = []
            for row in session.scalars(statement):
                task = TaskRecord.model_validate_json(json.dumps(row.task_json))
                task_time = task.scheduled_for
                if (
                    task_time is None
                    or task_time <= observed_at
                    or task.paused
                    or task.superseded
                    or task.current_attempt_id is not None
                    or task.failure_budget <= 0
                ):
                    continue
                scheduled.append(task_time)
            return tuple(sorted(scheduled))


__all__ = ["RuntimeTaskQueryRepository"]
