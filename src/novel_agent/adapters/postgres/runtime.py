"""Read/query adapter for rebuildable Stage 5 runtime projections."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.models import RuntimeTaskProjectionRow
from novel_agent.domain.ids import ProjectId, RunId, TaskId
from novel_agent.domain.runtime import TaskRecord, TaskStatus


class RuntimeTaskQueryRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def next_ready(
        self,
        *,
        project_id: ProjectId | None = None,
        run_id: RunId | None = None,
    ) -> TaskId | None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            statement = select(RuntimeTaskProjectionRow).where(
                RuntimeTaskProjectionRow.status == TaskStatus.READY.value,
                (
                    RuntimeTaskProjectionRow.scheduled_for.is_(None)
                    | (RuntimeTaskProjectionRow.scheduled_for <= now)
                ),
            )
            if project_id is not None:
                statement = statement.where(RuntimeTaskProjectionRow.project_id == project_id.root)
            if run_id is not None:
                statement = statement.where(RuntimeTaskProjectionRow.run_id == run_id.root)
            row = session.scalar(
                statement.order_by(
                    RuntimeTaskProjectionRow.priority.desc(),
                    RuntimeTaskProjectionRow.scheduled_for,
                    RuntimeTaskProjectionRow.task_id,
                ).limit(1)
            )
            return None if row is None else TaskId(row.task_id)

    def list_run(self, run_id: RunId) -> tuple[TaskRecord, ...]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(RuntimeTaskProjectionRow)
                .where(RuntimeTaskProjectionRow.run_id == run_id.root)
                .order_by(RuntimeTaskProjectionRow.updated_at, RuntimeTaskProjectionRow.task_id)
            )
            return tuple(TaskRecord.model_validate_json(json.dumps(row.task_json)) for row in rows)


__all__ = ["RuntimeTaskQueryRepository"]
