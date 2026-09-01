"""Durable SQLAlchemy implementation of the model-call ledger port."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.models import ModelCallLedgerRow
from novel_agent.domain.ids import ArtifactId, RunId, StableId
from novel_agent.domain.model_calls import (
    EffectiveBudgetResult,
    ModelCallLedgerEntry,
    ModelCallLedgerStatus,
    ModelRequest,
)
from novel_agent.services.model_call_ledger import (
    ModelCallLedgerCollision,
    ModelCallLedgerPort,
    model_request_hash,
)


class SqlModelCallLedger(ModelCallLedgerPort):
    """One durable row per provider request identity."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create_requested(
        self,
        request: ModelRequest,
        *,
        effective_budget: EffectiveBudgetResult,
        reasoning_included_in_completion_tokens: bool,
    ) -> ModelCallLedgerEntry:
        request_hash = model_request_hash(request)
        with self._session_factory() as session, session.begin():
            row = session.get(ModelCallLedgerRow, request.request_id.root, with_for_update=True)
            if row is not None:
                existing = self._to_domain(row)
                if existing.request_hash != request_hash:
                    raise ModelCallLedgerCollision("model request identity collision")
                if (
                    existing.effective_budget != effective_budget
                    or existing.reasoning_included_in_completion_tokens
                    != reasoning_included_in_completion_tokens
                ):
                    raise ModelCallLedgerCollision("effective budget identity collision")
                return existing
            entry = ModelCallLedgerEntry(
                request_id=request.request_id,
                run_id=request.run_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                request_hash=request_hash,
                effective_budget=effective_budget,
                reasoning_included_in_completion_tokens=reasoning_included_in_completion_tokens,
                status=ModelCallLedgerStatus.REQUESTED,
                logical_phase=request.scheduling_stage or request.purpose.value,
                requested_at=datetime.now(UTC),
            )
            session.add(self._from_domain(entry))
            return entry

    def rebind_requested(
        self,
        request: ModelRequest,
        *,
        expected_request_hash: ArtifactId,
        effective_budget: EffectiveBudgetResult,
        reasoning_included_in_completion_tokens: bool,
    ) -> ModelCallLedgerEntry:
        request_hash = model_request_hash(request)
        with self._session_factory() as session, session.begin():
            row = session.get(ModelCallLedgerRow, request.request_id.root, with_for_update=True)
            if row is None:
                raise KeyError(f"model request was not reserved: {request.request_id.root}")
            existing = self._to_domain(row)
            if (
                existing.request_hash != expected_request_hash
                or existing.status is not ModelCallLedgerStatus.REQUESTED
                or existing.completed_at is not None
            ):
                raise ModelCallLedgerCollision("model request cannot rebind after reservation")
            if (
                existing.effective_budget != effective_budget
                or existing.reasoning_included_in_completion_tokens
                != reasoning_included_in_completion_tokens
            ):
                raise ModelCallLedgerCollision("effective budget identity collision")
            rebound = existing.model_copy(update={"request_hash": request_hash})
            self._update_row(row, rebound)
            return rebound

    def settle(self, entry: ModelCallLedgerEntry) -> ModelCallLedgerEntry:
        with self._session_factory() as session, session.begin():
            row = session.get(ModelCallLedgerRow, entry.request_id.root, with_for_update=True)
            if row is None:
                raise KeyError(f"model request was not reserved: {entry.request_id.root}")
            existing = self._to_domain(row)
            if (
                existing.request_hash != entry.request_hash
                or existing.run_id != entry.run_id
                or existing.task_id != entry.task_id
                or existing.effective_budget != entry.effective_budget
                or existing.reasoning_included_in_completion_tokens
                != entry.reasoning_included_in_completion_tokens
            ):
                raise ModelCallLedgerCollision("model call settlement identity collision")
            if (
                existing.status != entry.status
                and existing.completed_at is not None
                and not (
                    existing.status is ModelCallLedgerStatus.COMPLETED
                    and entry.status is ModelCallLedgerStatus.VALIDATION_REJECTED
                )
            ):
                raise ModelCallLedgerCollision("terminal model call cannot be overwritten")
            self._update_row(row, entry)
            return entry

    def load(self, request_id: StableId) -> ModelCallLedgerEntry | None:
        with self._session_factory() as session:
            row = session.get(ModelCallLedgerRow, request_id.root)
            return None if row is None else self._to_domain(row)

    def list_for_prefix(self, request_id_prefix: str) -> tuple[ModelCallLedgerEntry, ...]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(ModelCallLedgerRow)
                .where(
                    (ModelCallLedgerRow.request_id == request_id_prefix)
                    | ModelCallLedgerRow.request_id.startswith(f"{request_id_prefix}.")
                )
                .order_by(ModelCallLedgerRow.request_id)
            ).all()
            return tuple(self._to_domain(row) for row in rows)

    def list_for_run(self, run_id: RunId) -> tuple[ModelCallLedgerEntry, ...]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(ModelCallLedgerRow)
                .where(ModelCallLedgerRow.run_id == run_id.root)
                .order_by(ModelCallLedgerRow.request_id)
            ).all()
            return tuple(self._to_domain(row) for row in rows)

    @staticmethod
    def _from_domain(entry: ModelCallLedgerEntry) -> ModelCallLedgerRow:
        return ModelCallLedgerRow(
            request_id=entry.request_id.root,
            run_id=entry.run_id.root,
            task_id=entry.task_id.root,
            attempt_id=entry.attempt_id.root if entry.attempt_id is not None else None,
            request_hash=entry.request_hash.root,
            status=entry.status.value,
            logical_phase=entry.logical_phase,
            effective_budget_json=entry.effective_budget.model_dump(mode="json"),
            reasoning_included_in_completion_tokens=entry.reasoning_included_in_completion_tokens,
            provider_request_id=entry.provider_request_id,
            provider_sent_at=entry.provider_sent_at,
            raw_response_hash=(
                entry.raw_response_hash.root if entry.raw_response_hash is not None else None
            ),
            raw_artifact_json=(
                entry.raw_artifact_ref.model_dump(mode="json")
                if entry.raw_artifact_ref is not None
                else None
            ),
            call_record_json=(
                entry.call_record.model_dump(mode="json") if entry.call_record is not None else None
            ),
            validation_error=entry.validation_error,
            transport_error_type=entry.transport_error_type,
            requested_at=entry.requested_at,
            completed_at=entry.completed_at,
        )

    @classmethod
    def _update_row(cls, row: ModelCallLedgerRow, entry: ModelCallLedgerEntry) -> None:
        replacement = cls._from_domain(entry)
        for column in (
            "run_id",
            "task_id",
            "attempt_id",
            "request_hash",
            "status",
            "logical_phase",
            "effective_budget_json",
            "reasoning_included_in_completion_tokens",
            "provider_request_id",
            "provider_sent_at",
            "raw_response_hash",
            "raw_artifact_json",
            "call_record_json",
            "validation_error",
            "transport_error_type",
            "requested_at",
            "completed_at",
        ):
            setattr(row, column, getattr(replacement, column))

    @staticmethod
    def _as_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @staticmethod
    def _to_domain(row: ModelCallLedgerRow) -> ModelCallLedgerEntry:
        from novel_agent.domain.artifacts import ArtifactRef
        from novel_agent.domain.ids import ArtifactId, RunId, TaskId
        from novel_agent.domain.model_calls import EffectiveBudgetResult, ModelCallRecord
        from novel_agent.services.content_addressing import canonical_json_bytes

        raw_ref = (
            ArtifactRef.model_validate_json(
                canonical_json_bytes(row.raw_artifact_json), strict=True
            )
            if row.raw_artifact_json is not None
            else None
        )
        call_record = (
            ModelCallRecord.model_validate_json(
                canonical_json_bytes(row.call_record_json), strict=True
            )
            if row.call_record_json is not None
            else None
        )
        requested_at = SqlModelCallLedger._as_utc(row.requested_at)
        assert requested_at is not None
        return ModelCallLedgerEntry(
            request_id=StableId(row.request_id),
            run_id=RunId(row.run_id),
            task_id=TaskId(row.task_id),
            attempt_id=StableId(row.attempt_id) if row.attempt_id is not None else None,
            request_hash=ArtifactId(row.request_hash),
            effective_budget=EffectiveBudgetResult.model_validate_json(
                canonical_json_bytes(row.effective_budget_json), strict=True
            ),
            reasoning_included_in_completion_tokens=row.reasoning_included_in_completion_tokens,
            status=ModelCallLedgerStatus(row.status),
            logical_phase=row.logical_phase,
            provider_request_id=row.provider_request_id,
            provider_sent_at=SqlModelCallLedger._as_utc(row.provider_sent_at),
            raw_response_hash=(
                ArtifactId(row.raw_response_hash) if row.raw_response_hash is not None else None
            ),
            raw_artifact_ref=raw_ref,
            call_record=call_record,
            validation_error=row.validation_error,
            transport_error_type=row.transport_error_type,
            requested_at=requested_at,
            completed_at=SqlModelCallLedger._as_utc(row.completed_at),
        )
