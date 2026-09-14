"""Read/query adapter for rebuildable Stage 5 runtime projections."""

from __future__ import annotations

import json
from dataclasses import dataclass
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
from novel_agent.domain.artifacts import MODEL_RAW_RESPONSE_MEDIA_TYPE
from novel_agent.domain.ids import ProjectId, RunId, StableId, TaskId
from novel_agent.domain.model_calls import ModelCallLedgerStatus
from novel_agent.domain.runtime import (
    EffectStatus,
    TaskAttempt,
    TaskRecord,
    TaskStatus,
)


@dataclass(frozen=True, slots=True)
class AttemptEffectLedgerEvidence:
    """The provider/effect frontier that is safe to use for task recovery.

    Unsettled effects are retained across all historical attempts: an old request
    whose outcome is unknown still has to be reconciled before a new request can be
    issued.  Completed model responses are narrower: only a response attached to
    the current recovery attempt and backed by a raw-response artifact is a replay
    input.  A completed commit or projection effect is deliberately never returned
    as a model response.
    """

    frontier_attempt_id: StableId | None = None
    unsettled_sends: tuple[str, ...] = ()
    outstanding_request_ids: tuple[str, ...] = ()
    completed_response_refs: tuple[str, ...] = ()
    consumed_response_ids: tuple[str, ...] = ()
    unavailable_response_ids: tuple[str, ...] = ()


def _raw_response_artifact_id(raw_artifact_json: object) -> str | None:
    """Return an artifact id only when the ledger points at raw model evidence."""

    if not isinstance(raw_artifact_json, dict):
        return None
    artifact_id = raw_artifact_json.get("artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id:
        return None
    media_type = raw_artifact_json.get("media_type")
    if media_type is not None and media_type != MODEL_RAW_RESPONSE_MEDIA_TYPE:
        return None
    return artifact_id


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

    def attempt_effect_evidence(
        self,
        task_id: TaskId,
        *,
        attempt_id: StableId | None = None,
    ) -> AttemptEffectLedgerEvidence:
        """Read one task's current recovery frontier from both durable ledgers.

        ``RuntimeEffectProjectionRow`` and ``ModelCallLedgerRow`` have different
        responsibilities.  The former can fence any external effect; the latter
        is the only source that can provide a replayable model response.  Historical
        completed rows are therefore not replay candidates, while historical
        REQUESTED/UNCERTAIN rows remain visible so a retry cannot bypass them by
        changing the request prefix.
        """

        with self._session_factory() as session:
            task_row = session.get(RuntimeTaskProjectionRow, task_id.root)
            if task_row is None:
                raise KeyError(f"unknown task {task_id.root}")
            frontier = attempt_id
            if frontier is None and task_row.current_attempt_id is not None:
                frontier = StableId(task_row.current_attempt_id)
            if frontier is None:
                latest_attempt = session.scalars(
                    select(RuntimeTaskAttemptRow)
                    .where(
                        RuntimeTaskAttemptRow.task_id == task_id.root,
                        RuntimeTaskAttemptRow.ended_at.is_not(None),
                    )
                    .order_by(RuntimeTaskAttemptRow.attempt_no.desc())
                    .limit(1)
                ).first()
                if latest_attempt is not None:
                    frontier = StableId(latest_attempt.attempt_id)

            effect_rows = session.execute(
                select(
                    RuntimeEffectProjectionRow.effect_identity,
                    RuntimeEffectProjectionRow.status,
                ).where(RuntimeEffectProjectionRow.task_id == task_id.root)
            ).all()
            model_rows = session.execute(
                select(
                    ModelCallLedgerRow.request_id,
                    ModelCallLedgerRow.attempt_id,
                    ModelCallLedgerRow.status,
                    ModelCallLedgerRow.raw_artifact_json,
                    ModelCallLedgerRow.response_consumed_at,
                ).where(ModelCallLedgerRow.task_id == task_id.root)
            ).all()

        # One identity may be present in both ledgers.  An unresolved state always
        # wins over a terminal one, so a completed projection can never hide a
        # later uncertain provider send.
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
            # A completed commit/projection is not a model response and is not
            # placed in the replay bucket.

        unavailable: set[str] = set()
        consumed: set[str] = set()
        for (
            request_id,
            row_attempt_id,
            status,
            raw_artifact_json,
            response_consumed_at,
        ) in model_rows:
            if status == ModelCallLedgerStatus.REQUESTED.value:
                record(request_id, 2, "outstanding", request_id)
            elif status == ModelCallLedgerStatus.UNCERTAIN.value:
                record(request_id, 3, "unsettled", request_id)
            elif status == ModelCallLedgerStatus.COMPLETED.value:
                response_ref = _raw_response_artifact_id(raw_artifact_json)
                if response_ref is None:
                    # Do not turn an internally inconsistent terminal row into a
                    # free retry.  The caller must account for the response and
                    # usage before deciding whether a new provider call is legal.
                    unavailable.add(request_id)
                elif frontier is not None and row_attempt_id == frontier.root:
                    if response_consumed_at is not None:
                        consumed.add(request_id)
                    else:
                        record(request_id, 1, "completed", response_ref)

        unsettled = tuple(
            sorted(
                reference
                for _priority, bucket, reference in states.values()
                if bucket == "unsettled"
            )
        )
        outstanding = tuple(
            sorted(
                reference
                for _priority, bucket, reference in states.values()
                if bucket == "outstanding"
            )
        )
        completed = tuple(
            sorted(
                reference
                for _priority, bucket, reference in states.values()
                if bucket == "completed"
            )
        )
        return AttemptEffectLedgerEvidence(
            frontier_attempt_id=frontier,
            unsettled_sends=unsettled,
            outstanding_request_ids=outstanding,
            completed_response_refs=completed,
            consumed_response_ids=tuple(sorted(consumed)),
            unavailable_response_ids=tuple(sorted(unavailable)),
        )

    def attempt_effect_ledger(
        self, task_id: TaskId
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """Backward-compatible tuple view of the current recovery frontier."""

        evidence = self.attempt_effect_evidence(task_id)
        return (
            evidence.unsettled_sends,
            evidence.outstanding_request_ids,
            evidence.completed_response_refs,
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
