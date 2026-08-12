"""Stage 5 audit report projection derived only from durable runtime truth."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.models import (
    RuntimeEffectProjectionRow,
    RuntimeTaskAttemptRow,
    RuntimeTaskProjectionRow,
)
from novel_agent.domain.ids import ArtifactId, RunId
from novel_agent.domain.model_calls import ModelCallRecord
from novel_agent.domain.runtime import EffectReceipt, TaskAttempt, TaskRecord, failure_policy
from novel_agent.domain.stage5_evaluation import Stage5RuntimeAuditReport
from novel_agent.domain.stage5_manifest import load_stage5_manifest
from novel_agent.services.event_log import RunEventLogRepository


class RuntimeReportService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        events: RunEventLogRepository,
    ) -> None:
        self._session_factory = session_factory
        self._events = events

    def export(
        self,
        run_id: RunId,
        *,
        manifest_path: Path,
        executable_commit: str,
    ) -> Stage5RuntimeAuditReport:
        manifest = load_stage5_manifest(manifest_path)
        manifest_fingerprint = ArtifactId(
            f"sha256:{hashlib.sha256(manifest_path.read_bytes()).hexdigest()}"
        )
        events = self._events.replay(run_id)
        with self._session_factory() as session:
            task_rows = tuple(
                session.scalars(
                    select(RuntimeTaskProjectionRow)
                    .where(RuntimeTaskProjectionRow.run_id == run_id.root)
                    .order_by(RuntimeTaskProjectionRow.updated_at, RuntimeTaskProjectionRow.task_id)
                )
            )
            task_ids = tuple(row.task_id for row in task_rows)
            attempt_rows = (
                ()
                if not task_ids
                else tuple(
                    session.scalars(
                        select(RuntimeTaskAttemptRow)
                        .where(RuntimeTaskAttemptRow.task_id.in_(task_ids))
                        .order_by(
                            RuntimeTaskAttemptRow.claimed_at,
                            RuntimeTaskAttemptRow.attempt_id,
                        )
                    )
                )
            )
            effect_rows = tuple(
                session.scalars(
                    select(RuntimeEffectProjectionRow)
                    .where(RuntimeEffectProjectionRow.run_id == run_id.root)
                    .order_by(RuntimeEffectProjectionRow.effect_identity)
                )
            )
        tasks = tuple(
            TaskRecord.model_validate_json(json.dumps(row.task_json)) for row in task_rows
        )
        attempts = tuple(
            TaskAttempt.model_validate_json(json.dumps(row.attempt_json)) for row in attempt_rows
        )
        effects = tuple(
            EffectReceipt.model_validate_json(json.dumps(row.effect_json)) for row in effect_rows
        )
        model_records = tuple(
            event.model_call_record for event in events if event.model_call_record is not None
        )
        model_cost = sum(
            (
                record.usage.cost_usd
                for record in model_records
                if isinstance(record, ModelCallRecord)
            ),
            start=Decimal("0"),
        )
        skill_hashes = tuple(sorted({item for event in events for item in event.skill_hashes}))
        flags = manifest.feature_admission.model_dump()
        return Stage5RuntimeAuditReport(
            run_id=run_id,
            generated_at=datetime.now(UTC),
            manifest_fingerprint=manifest_fingerprint,
            executable_commit=executable_commit,
            tasks=tasks,
            attempts=attempts,
            retry_owners={
                attempt.attempt_id.root: failure_policy(attempt.failure_class).retry_owner.value
                for attempt in attempts
                if attempt.failure_class is not None
            },
            effects=effects,
            events=events,
            model_request_count=len(model_records),
            model_cost_usd=model_cost,
            skill_hashes=skill_hashes,
            active_feature_flags=tuple(sorted(name for name, active in flags.items() if active)),
            deferred_feature_flags=tuple(
                sorted(name for name, active in flags.items() if not active)
            ),
        )


__all__ = ["RuntimeReportService"]
