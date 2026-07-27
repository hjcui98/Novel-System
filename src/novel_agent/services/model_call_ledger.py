"""CAS model-call ledger used for structured retry and crash reconciliation."""

from __future__ import annotations

from typing import Protocol

from novel_agent.domain.ids import StableId
from novel_agent.domain.model_calls import (
    ModelCallLedgerEntry,
    ModelCallLedgerStatus,
    ModelRequest,
)


class ModelCallLedgerCollision(RuntimeError):
    """A model request identity was reused with different immutable content."""


class ModelCallLedgerPort(Protocol):
    def create_requested(self, request: ModelRequest) -> ModelCallLedgerEntry: ...

    def settle(self, entry: ModelCallLedgerEntry) -> ModelCallLedgerEntry: ...

    def load(self, request_id: StableId) -> ModelCallLedgerEntry | None: ...

    def list_for_prefix(self, request_id_prefix: str) -> tuple[ModelCallLedgerEntry, ...]: ...


class InMemoryModelCallLedger:
    """Reference CAS implementation; durable stores can implement the same port."""

    def __init__(self) -> None:
        self._entries: dict[StableId, ModelCallLedgerEntry] = {}

    def create_requested(self, request: ModelRequest) -> ModelCallLedgerEntry:
        from datetime import UTC, datetime

        from novel_agent.domain.model_calls import ModelCallLedgerStatus
        from novel_agent.services.artifacts import sha256_id
        from novel_agent.services.content_addressing import canonical_json_bytes

        entry = ModelCallLedgerEntry(
            request_id=request.request_id,
            run_id=request.run_id,
            task_id=request.task_id,
            request_hash=sha256_id(canonical_json_bytes(request.model_dump(mode="json"))),
            status=ModelCallLedgerStatus.REQUESTED,
            requested_at=datetime.now(UTC),
        )
        existing = self._entries.get(request.request_id)
        if existing is not None:
            if existing.request_hash != entry.request_hash:
                raise ModelCallLedgerCollision("model request identity collision")
            return existing
        self._entries[request.request_id] = entry
        return entry

    def settle(self, entry: ModelCallLedgerEntry) -> ModelCallLedgerEntry:
        existing = self._entries.get(entry.request_id)
        if existing is None:
            raise KeyError(f"model request was not reserved: {entry.request_id.root}")
        if (
            existing.request_hash != entry.request_hash
            or existing.run_id != entry.run_id
            or existing.task_id != entry.task_id
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
        self._entries[entry.request_id] = entry
        return entry

    def load(self, request_id: StableId) -> ModelCallLedgerEntry | None:
        return self._entries.get(request_id)

    def list_for_prefix(self, request_id_prefix: str) -> tuple[ModelCallLedgerEntry, ...]:
        return tuple(
            entry
            for request_id, entry in sorted(self._entries.items(), key=lambda item: item[0].root)
            if request_id.root == request_id_prefix
            or request_id.root.startswith(f"{request_id_prefix}.")
        )
