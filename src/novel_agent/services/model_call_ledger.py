"""CAS model-call ledger used for structured retry and crash reconciliation."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol

from novel_agent.domain.ids import ArtifactId, RunId, StableId, TaskId
from novel_agent.domain.model_calls import (
    EffectiveBudgetResult,
    ModelCallLedgerAggregate,
    ModelCallLedgerEntry,
    ModelCallLedgerStatus,
    ModelCallRecord,
    ModelCostAvailability,
    ModelRequest,
)


def model_request_hash(request: ModelRequest) -> ArtifactId:
    from novel_agent.services.artifacts import sha256_id
    from novel_agent.services.content_addressing import canonical_json_bytes

    return sha256_id(canonical_json_bytes(request.model_dump(mode="json")))


class ModelCallLedgerCollision(RuntimeError):
    """A model request identity was reused with different immutable content."""


def bounded_model_request_id(request: ModelRequest, suffix: str) -> StableId:
    """Derive a child request identity without truncating its parent.

    Model-call request IDs are global keys in both the in-memory and SQL
    ledgers. A child request therefore needs the complete parent identity when
    it fits, or an existing globally addressable attempt/task identity plus the
    child suffix. Falling back to a run-only or sequence-only shape would make
    two requests in one run indistinguishable, so an unrepresentable identity
    fails closed instead.
    """

    candidates = [f"{request.request_id.root}{suffix}"]
    # A long parent cannot be truncated to an attempt-only fallback: sibling
    # children (for example graph units u000 and u001) would then share one
    # global scheduling/ledger identity. Keep a bounded digest of the complete
    # parent request in every scoped fallback so child derivation remains
    # injective within the attempt/task scope.
    parent_digest = hashlib.sha256(request.request_id.root.encode("utf-8")).hexdigest()[:24]
    if request.attempt_id is not None:
        candidates.extend(
            (
                f"model-request.{request.attempt_id.root}.{parent_digest}{suffix}",
                f"{request.attempt_id.root}.{parent_digest}{suffix}",
            )
        )
    candidates.extend(
        (
            f"model-request.{request.task_id.root}.{parent_digest}{suffix}",
            f"{request.task_id.root}.{parent_digest}{suffix}",
        )
    )
    for candidate in candidates:
        try:
            return StableId(candidate)
        except ValueError:
            continue
    raise ValueError("model request child identity has no bounded request, attempt, or task scope")


class ModelCallLedgerPort(Protocol):
    def create_requested(
        self,
        request: ModelRequest,
        *,
        effective_budget: EffectiveBudgetResult,
        reasoning_included_in_completion_tokens: bool,
    ) -> ModelCallLedgerEntry: ...

    def rebind_requested(
        self,
        request: ModelRequest,
        *,
        expected_request_hash: ArtifactId,
        effective_budget: EffectiveBudgetResult,
        reasoning_included_in_completion_tokens: bool,
    ) -> ModelCallLedgerEntry: ...

    def settle(self, entry: ModelCallLedgerEntry) -> ModelCallLedgerEntry: ...

    def load(self, request_id: StableId) -> ModelCallLedgerEntry | None: ...

    def list_for_prefix(self, request_id_prefix: str) -> tuple[ModelCallLedgerEntry, ...]: ...

    def list_for_run(self, run_id: RunId) -> tuple[ModelCallLedgerEntry, ...]: ...


class InMemoryModelCallLedger:
    """Reference CAS implementation; durable stores can implement the same port."""

    def __init__(self) -> None:
        self._entries: dict[StableId, ModelCallLedgerEntry] = {}

    def create_requested(
        self,
        request: ModelRequest,
        *,
        effective_budget: EffectiveBudgetResult,
        reasoning_included_in_completion_tokens: bool,
    ) -> ModelCallLedgerEntry:
        from datetime import UTC, datetime

        entry = ModelCallLedgerEntry(
            request_id=request.request_id,
            run_id=request.run_id,
            task_id=request.task_id,
            attempt_id=request.attempt_id,
            request_hash=model_request_hash(request),
            effective_budget=effective_budget,
            reasoning_included_in_completion_tokens=reasoning_included_in_completion_tokens,
            status=ModelCallLedgerStatus.REQUESTED,
            logical_phase=request.scheduling_stage or request.purpose.value,
            requested_at=datetime.now(UTC),
        )
        existing = self._entries.get(request.request_id)
        if existing is not None:
            if existing.request_hash != entry.request_hash:
                raise ModelCallLedgerCollision("model request identity collision")
            if (
                existing.effective_budget != effective_budget
                or existing.reasoning_included_in_completion_tokens
                != reasoning_included_in_completion_tokens
            ):
                raise ModelCallLedgerCollision("effective budget identity collision")
            return existing
        self._entries[request.request_id] = entry
        return entry

    def rebind_requested(
        self,
        request: ModelRequest,
        *,
        expected_request_hash: ArtifactId,
        effective_budget: EffectiveBudgetResult,
        reasoning_included_in_completion_tokens: bool,
    ) -> ModelCallLedgerEntry:
        existing = self._entries.get(request.request_id)
        if existing is None:
            raise KeyError(f"model request was not reserved: {request.request_id.root}")
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
        rebound = existing.model_copy(update={"request_hash": model_request_hash(request)})
        self._entries[request.request_id] = rebound
        return rebound

    def settle(self, entry: ModelCallLedgerEntry) -> ModelCallLedgerEntry:
        existing = self._entries.get(entry.request_id)
        if existing is None:
            raise KeyError(f"model request was not reserved: {entry.request_id.root}")
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

    def list_for_run(self, run_id: RunId) -> tuple[ModelCallLedgerEntry, ...]:
        return tuple(
            entry
            for entry in sorted(self._entries.values(), key=lambda item: item.request_id.root)
            if entry.run_id == run_id
        )


def summarize_model_cost(
    records: Sequence[ModelCallRecord],
) -> tuple[Decimal | None, ModelCostAvailability]:
    availability = tuple(record.usage.cost_availability for record in records)
    if not availability or ModelCostAvailability.UNKNOWN in availability:
        return None, ModelCostAvailability.UNKNOWN
    if all(item is ModelCostAvailability.NOT_APPLICABLE for item in availability):
        return None, ModelCostAvailability.NOT_APPLICABLE
    return (
        sum((record.usage.cost_usd for record in records), start=Decimal("0")),
        ModelCostAvailability.KNOWN,
    )


def aggregate_model_calls(
    entries: tuple[ModelCallLedgerEntry, ...],
) -> tuple[ModelCallLedgerAggregate, ...]:
    """Rebuild phase usage from ledger entries, including non-terminal requests."""

    grouped: dict[tuple[RunId, TaskId, StableId | None, str], list[ModelCallLedgerEntry]] = (
        defaultdict(list)
    )
    for entry in entries:
        grouped[(entry.run_id, entry.task_id, entry.attempt_id, entry.logical_phase)].append(entry)

    aggregates: list[ModelCallLedgerAggregate] = []
    for (run_id, task_id, attempt_id, logical_phase), group in sorted(
        grouped.items(),
        key=lambda item: (
            item[0][0].root,
            item[0][1].root,
            "" if item[0][2] is None else item[0][2].root,
            item[0][3],
        ),
    ):
        records = tuple(entry.call_record for entry in group if entry.call_record is not None)
        availability = tuple(record.usage.cost_availability for record in records)
        if not availability or ModelCostAvailability.UNKNOWN in availability:
            cost_availability = ModelCostAvailability.UNKNOWN
            cost_usd = None
        elif all(item is ModelCostAvailability.NOT_APPLICABLE for item in availability):
            cost_availability = ModelCostAvailability.NOT_APPLICABLE
            cost_usd = None
        else:
            cost_availability = ModelCostAvailability.KNOWN
            cost_usd = sum(
                (record.usage.cost_usd for record in records),
                start=Decimal("0"),
            )
        aggregates.append(
            ModelCallLedgerAggregate(
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                logical_phase=logical_phase,
                request_count=len(group),
                schema_retry_count=sum(
                    1 for entry in group if _schema_retry_index(entry.request_id.root) > 0
                ),
                status_counts={
                    status.value: sum(1 for entry in group if entry.status is status)
                    for status in ModelCallLedgerStatus
                    if any(entry.status is status for entry in group)
                },
                input_tokens=sum(record.usage.input_tokens for record in records),
                output_tokens=sum(record.usage.output_tokens for record in records),
                reasoning_tokens=sum(record.usage.reasoning_tokens for record in records),
                latency_ms=sum(record.latency_ms for record in records),
                cost_usd=cost_usd,
                cost_availability=cost_availability,
            )
        )
    return tuple(aggregates)


def _schema_retry_index(request_id: str) -> int:
    marker = ".schema-retry"
    if marker not in request_id:
        return 0
    suffix = request_id.rsplit(marker, 1)[1]
    return int(suffix) if suffix.isdigit() else 0
