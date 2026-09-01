"""Append-only operational event log and event-bound checkpoint repository."""

import json
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.models import RunCheckpointRow, RunEventRow, RunStreamRow
from novel_agent.domain.ids import RunId
from novel_agent.domain.runtime import ResumabilityStatus, RunCheckpoint, RunEvent


class EventLogConflictError(RuntimeError):
    pass


class EventSequenceError(RuntimeError):
    pass


class CheckpointConflictError(RuntimeError):
    pass


class RunEventLogRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def next_sequence(self, run_id: RunId) -> int:
        """Return the next durable sequence number for one run stream."""

        with self._session_factory() as session:
            stream = session.get(RunStreamRow, run_id.root)
            return 1 if stream is None else stream.last_sequence_no + 1

    def append(self, event: RunEvent) -> RunEvent:
        with self._session_factory() as session, session.begin():
            return self._append_in_session(session, event)

    def _append_in_session(self, session: Session, event: RunEvent) -> RunEvent:
        """Append inside a caller-owned transaction for atomic runtime projections."""

        if session.get_bind().dialect.name == "postgresql":  # pragma: no cover
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:run_id, 0))"),
                {"run_id": event.run_id.root},
            )
        existing_identity = session.scalar(
            select(RunEventRow).where(
                RunEventRow.run_id == event.run_id.root,
                RunEventRow.idempotency_identity == event.idempotency_identity.root,
            )
        )
        if existing_identity is not None:
            existing = self._event_from_row(existing_identity)
            comparable_event = event.model_copy(
                update={"occurred_at": existing.occurred_at, "span_id": existing.span_id}
            )
            if existing != comparable_event:
                raise EventLogConflictError("idempotency identity refers to another event")
            return existing

        existing_event = session.get(RunEventRow, event.event_id.root)
        if existing_event is not None:
            raise EventLogConflictError("event_id already exists")

        stream = session.scalar(
            select(RunStreamRow).where(RunStreamRow.run_id == event.run_id.root).with_for_update()
        )
        expected = 1 if stream is None else stream.last_sequence_no + 1
        if event.sequence_no != expected:
            raise EventSequenceError(
                f"run expects sequence {expected}, received {event.sequence_no}"
            )
        if stream is None:
            stream = RunStreamRow(
                run_id=event.run_id.root,
                last_sequence_no=event.sequence_no,
                created_at=event.occurred_at,
            )
            session.add(stream)
            session.flush()
        else:
            stream.last_sequence_no = event.sequence_no
        session.add(
            RunEventRow(
                event_id=event.event_id.root,
                run_id=event.run_id.root,
                task_id=event.task_id.root if event.task_id else None,
                sequence_no=event.sequence_no,
                event_type=event.event_type.value,
                occurred_at=event.occurred_at,
                idempotency_identity=event.idempotency_identity.root,
                payload_schema_version=event.payload_schema_version.root,
                trace_id=event.trace_id,
                event_json=event.model_dump(mode="json"),
            )
        )
        return event

    def replay(self, run_id: RunId, *, after_sequence: int = 0) -> tuple[RunEvent, ...]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(RunEventRow)
                .where(
                    RunEventRow.run_id == run_id.root,
                    RunEventRow.sequence_no > after_sequence,
                )
                .order_by(RunEventRow.sequence_no)
            )
            return tuple(self._event_from_row(row) for row in rows)

    @staticmethod
    def _event_from_row(row: RunEventRow) -> RunEvent:
        return RunEvent.model_validate_json(json.dumps(row.event_json))


class RunCheckpointRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def save(self, checkpoint: RunCheckpoint) -> RunCheckpoint:
        with self._session_factory() as session, session.begin():
            stream = session.scalar(
                select(RunStreamRow)
                .where(RunStreamRow.run_id == checkpoint.run_id.root)
                .with_for_update()
            )
            if stream is None or checkpoint.event_position > stream.last_sequence_no:
                raise CheckpointConflictError("checkpoint exceeds the run event high watermark")

            existing = session.get(RunCheckpointRow, checkpoint.checkpoint_id.root)
            if existing is not None:
                restored = self._checkpoint_from_row(existing)
                if restored != checkpoint:
                    raise CheckpointConflictError("checkpoint_id refers to another checkpoint")
                return restored

            position_exists = session.scalar(
                select(RunEventRow.event_id).where(
                    RunEventRow.run_id == checkpoint.run_id.root,
                    RunEventRow.sequence_no == checkpoint.event_position,
                )
            )
            if position_exists is None:
                raise CheckpointConflictError("checkpoint event position does not exist")
            session.add(
                RunCheckpointRow(
                    checkpoint_id=checkpoint.checkpoint_id.root,
                    run_id=checkpoint.run_id.root,
                    event_position=checkpoint.event_position,
                    checkpoint_json=checkpoint.model_dump(mode="json"),
                    created_at=datetime.now(UTC),
                )
            )
            return checkpoint

    def latest(
        self,
        run_id: RunId,
        *,
        logical_stage: str | None = None,
    ) -> RunCheckpoint | None:
        """Return the latest checkpoint, optionally restricted to one logical stream."""

        with self._session_factory() as session:
            rows = session.scalars(
                select(RunCheckpointRow)
                .where(RunCheckpointRow.run_id == run_id.root)
                .order_by(RunCheckpointRow.event_position.desc())
            )
            for row in rows:
                checkpoint = self._checkpoint_from_row(row)
                if logical_stage is None or checkpoint.logical_stage == logical_stage:
                    return checkpoint
            return None

    def latest_resumable(
        self,
        run_id: RunId,
        *,
        logical_stage: str | None = None,
    ) -> RunCheckpoint | None:
        with self._session_factory() as session:
            rows = session.scalars(
                select(RunCheckpointRow)
                .where(RunCheckpointRow.run_id == run_id.root)
                .order_by(RunCheckpointRow.event_position.desc())
            )
            for row in rows:
                checkpoint = self._checkpoint_from_row(row)
                if checkpoint.resumability_status is ResumabilityStatus.RESUMABLE and (
                    logical_stage is None or checkpoint.logical_stage == logical_stage
                ):
                    return checkpoint
            return None

    @staticmethod
    def _checkpoint_from_row(row: RunCheckpointRow) -> RunCheckpoint:
        return RunCheckpoint.model_validate_json(json.dumps(row.checkpoint_json))
