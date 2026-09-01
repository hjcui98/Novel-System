"""Endpoint-global, request-count and effective-KV model admission."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass
from enum import StrEnum


class ModelSchedulingState(StrEnum):
    WAITING_FOR_CAPACITY = "WAITING_FOR_CAPACITY"
    ADMITTED = "ADMITTED"
    RELEASED = "RELEASED"
    SCHEDULING_TIMEOUT = "SCHEDULING_TIMEOUT"
    SCHEDULING_BUDGET_UNSATISFIABLE = "SCHEDULING_BUDGET_UNSATISFIABLE"
    CONTEXT_BUDGET_EXCEEDED = "CONTEXT_BUDGET_EXCEEDED"


class ModelSchedulingError(RuntimeError):
    """Base class for failures before a provider request is submitted."""


class SchedulingTimeoutError(ModelSchedulingError):
    pass


class SchedulingBudgetUnsatisfiableError(ModelSchedulingError):
    pass


class ContextBudgetExceededError(ModelSchedulingError):
    pass


@dataclass(frozen=True, slots=True)
class ModelRequestSchedulingInfo:
    """Complete semantic scheduling descriptor for one provider request."""

    request_id: str
    endpoint_id: str
    need_id: str | None
    stage: str
    estimated_prompt_tokens: int
    reserved_output_tokens: int
    safety_allowance_tokens: int
    reserved_sequence_tokens: int
    dependency_ids: tuple[str, ...]
    context_hash: str
    priority: int = 50
    scheduling_timeout_seconds: float | None = None
    scheduling_deadline: float | None = None

    def __post_init__(self) -> None:
        if not self.request_id or not self.endpoint_id or not self.stage or not self.context_hash:
            raise ValueError("scheduling descriptor identities must be non-empty")
        if (
            min(
                self.estimated_prompt_tokens,
                self.reserved_output_tokens,
                self.safety_allowance_tokens,
            )
            < 0
        ):
            raise ValueError("scheduling token components cannot be negative")
        expected = (
            self.estimated_prompt_tokens
            + self.reserved_output_tokens
            + self.safety_allowance_tokens
        )
        if self.reserved_sequence_tokens != expected or expected < 1:
            raise ValueError("reserved sequence tokens must equal descriptor token components")
        if not 0 <= self.priority <= 100:
            raise ValueError("scheduling priority must be between zero and one hundred")
        if self.scheduling_timeout_seconds is not None and self.scheduling_timeout_seconds <= 0:
            raise ValueError("scheduling timeout must be positive")


@dataclass(slots=True)
class _QueueEntry:
    sequence: int
    info: ModelRequestSchedulingInfo


class ModelRequestLease:
    """Idempotence-checked context manager for one admitted reservation."""

    def __init__(
        self,
        controller: ModelRequestAdmissionController,
        reservation_id: str,
        info: ModelRequestSchedulingInfo,
    ) -> None:
        self._controller = controller
        self._reservation_id = reservation_id
        self.info = info
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            raise RuntimeError("model request lease was released twice")
        self._controller._release_reservation(self._reservation_id)
        self._released = True

    def __enter__(self) -> ModelRequestLease:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.release()


class ModelRequestAdmissionController:
    """Condition-queue scheduler shared by every caller of one endpoint."""

    def __init__(
        self,
        *,
        endpoint_request_limit: int,
        kv_token_budget: int | None = None,
        kv_safety_reserve_ratio: float = 0.20,
        model_sequence_limit: int = 131_072,
        default_scheduling_timeout_seconds: float = 120.0,
    ) -> None:
        if endpoint_request_limit < 1:
            raise ValueError("endpoint request limit must be positive")
        if kv_token_budget is not None and kv_token_budget < 1:
            raise ValueError("KV token budget must be positive")
        if not 0.0 <= kv_safety_reserve_ratio < 1.0:
            raise ValueError(
                "KV safety reserve ratio must be between zero inclusive and one exclusive"
            )
        if model_sequence_limit < 1:
            raise ValueError("model sequence limit must be positive")
        if default_scheduling_timeout_seconds <= 0:
            raise ValueError("scheduling timeout must be positive")
        self._endpoint_request_limit = endpoint_request_limit
        self._configured_kv_budget = kv_token_budget
        self._effective_kv_budget = (
            None
            if kv_token_budget is None
            else math.floor(kv_token_budget * (1.0 - kv_safety_reserve_ratio))
        )
        if self._effective_kv_budget == 0:
            raise ValueError("effective KV token budget must be positive")
        self._kv_safety_reserve_ratio = kv_safety_reserve_ratio
        self._model_sequence_limit = model_sequence_limit
        self._default_timeout = default_scheduling_timeout_seconds
        self._condition = threading.Condition(threading.Lock())
        self._queue: list[_QueueEntry] = []
        self._active: dict[str, ModelRequestSchedulingInfo] = {}
        self._sequence = 0
        self._legacy_sequence = 0
        self._inflight_kv_tokens = 0
        self._acquired_requests = 0
        self._released_requests = 0
        self._acquired_kv_tokens = 0
        self._released_kv_tokens = 0
        self._wait_seconds = 0.0
        self._timeouts = 0
        self._unsatisfiable = 0
        self._max_inflight_requests = 0
        self._max_inflight_kv_tokens = 0
        self._admitted_descriptors: list[ModelRequestSchedulingInfo] = []
        self._waits_by_stage: dict[str, int] = {}
        self._admissions_by_endpoint: dict[str, int] = {}

    @property
    def inflight_requests(self) -> int:
        with self._condition:
            return len(self._active)

    @property
    def inflight_kv_tokens(self) -> int:
        with self._condition:
            return self._inflight_kv_tokens

    @property
    def acquired_request_count(self) -> int:
        with self._condition:
            return self._acquired_requests

    def acquire(
        self,
        request: int | ModelRequestSchedulingInfo,
        *,
        timeout_seconds: float | None = None,
    ) -> ModelRequestLease:
        info = self._coerce_descriptor(request, timeout_seconds)
        self._validate_capacity(info)
        with self._condition:
            if info.request_id in self._active or any(
                queued.info.request_id == info.request_id for queued in self._queue
            ):
                raise RuntimeError("model scheduling request id is already active or queued")
            entry = _QueueEntry(self._sequence, info)
            self._sequence += 1
            self._queue.append(entry)
            started = time.monotonic()
            waited = False
            while True:
                self._queue.sort(key=lambda item: (-item.info.priority, item.sequence))
                is_head = self._queue[0] is entry
                has_capacity = len(self._active) < self._endpoint_request_limit and (
                    self._effective_kv_budget is None
                    or self._inflight_kv_tokens + info.reserved_sequence_tokens
                    <= self._effective_kv_budget
                )
                if is_head and has_capacity:
                    self._queue.remove(entry)
                    self._active[info.request_id] = info
                    self._inflight_kv_tokens += info.reserved_sequence_tokens
                    self._acquired_requests += 1
                    self._acquired_kv_tokens += info.reserved_sequence_tokens
                    self._max_inflight_requests = max(
                        self._max_inflight_requests, len(self._active)
                    )
                    self._max_inflight_kv_tokens = max(
                        self._max_inflight_kv_tokens, self._inflight_kv_tokens
                    )
                    self._admissions_by_endpoint[info.endpoint_id] = (
                        self._admissions_by_endpoint.get(info.endpoint_id, 0) + 1
                    )
                    self._admitted_descriptors.append(info)
                    self._wait_seconds += time.monotonic() - started
                    return ModelRequestLease(self, info.request_id, info)
                if not waited:
                    self._waits_by_stage[info.stage] = self._waits_by_stage.get(info.stage, 0) + 1
                    waited = True
                remaining = self._remaining_wait(info, started, timeout_seconds)
                if remaining <= 0:
                    self._queue.remove(entry)
                    self._timeouts += 1
                    self._wait_seconds += time.monotonic() - started
                    self._condition.notify_all()
                    active_request_ids = tuple(sorted(self._active))
                    queued_request_ids = tuple(queued.info.request_id for queued in self._queue)
                    raise SchedulingTimeoutError(
                        f"SCHEDULING_TIMEOUT: {info.request_id} waited for endpoint capacity; "
                        f"active={active_request_ids!r}; queued={queued_request_ids!r}"
                    )
                self._condition.wait(timeout=remaining)

    def release(self, estimated_tokens: int) -> None:
        """Compatibility release for older corridor callers."""

        if estimated_tokens < 1:
            raise ValueError("reserved sequence tokens must be positive")
        with self._condition:
            reservation_id = next(
                (
                    request_id
                    for request_id, info in self._active.items()
                    if info.stage == "legacy" and info.reserved_sequence_tokens == estimated_tokens
                ),
                None,
            )
        if reservation_id is None:
            raise RuntimeError("model request release has no matching active reservation")
        self._release_reservation(reservation_id)

    def _release_reservation(self, reservation_id: str) -> None:
        with self._condition:
            info = self._active.pop(reservation_id, None)
            if info is None:
                raise RuntimeError("model request lease is not active")
            self._inflight_kv_tokens -= info.reserved_sequence_tokens
            if self._inflight_kv_tokens < 0:
                raise RuntimeError("model request KV counter underflow")
            self._released_requests += 1
            self._released_kv_tokens += info.reserved_sequence_tokens
            self._condition.notify_all()

    def _coerce_descriptor(
        self,
        request: int | ModelRequestSchedulingInfo,
        timeout_seconds: float | None,
    ) -> ModelRequestSchedulingInfo:
        if isinstance(request, ModelRequestSchedulingInfo):
            return request
        if request < 1:
            raise ValueError("reserved sequence tokens must be positive")
        with self._condition:
            sequence = self._legacy_sequence
            self._legacy_sequence += 1
        return ModelRequestSchedulingInfo(
            request_id=f"legacy-{sequence}",
            endpoint_id="legacy-endpoint",
            need_id=None,
            stage="legacy",
            estimated_prompt_tokens=request,
            reserved_output_tokens=0,
            safety_allowance_tokens=0,
            reserved_sequence_tokens=request,
            dependency_ids=(),
            context_hash=f"legacy-{sequence}-{request}",
            scheduling_timeout_seconds=timeout_seconds,
            scheduling_deadline=(
                time.monotonic() + timeout_seconds if timeout_seconds is not None else None
            ),
        )

    def _validate_capacity(self, info: ModelRequestSchedulingInfo) -> None:
        if info.reserved_sequence_tokens > self._model_sequence_limit:
            raise ContextBudgetExceededError(
                f"CONTEXT_BUDGET_EXCEEDED: {info.reserved_sequence_tokens} > "
                f"{self._model_sequence_limit}"
            )
        if (
            self._effective_kv_budget is not None
            and info.reserved_sequence_tokens > self._effective_kv_budget
        ):
            with self._condition:
                self._unsatisfiable += 1
            raise SchedulingBudgetUnsatisfiableError(
                f"SCHEDULING_BUDGET_UNSATISFIABLE: {info.reserved_sequence_tokens} > "
                f"{self._effective_kv_budget}"
            )

    def _remaining_wait(
        self,
        info: ModelRequestSchedulingInfo,
        started: float,
        explicit_timeout: float | None,
    ) -> float:
        timeout = self._default_timeout if explicit_timeout is None else explicit_timeout
        deadline = started + timeout
        if info.scheduling_deadline is not None:
            deadline = min(deadline, info.scheduling_deadline)
        return deadline - time.monotonic()

    def snapshot(self) -> dict[str, object]:
        with self._condition:
            return {
                "endpoint_request_limit": self._endpoint_request_limit,
                "configured_kv_token_budget": self._configured_kv_budget,
                "effective_kv_token_budget": self._effective_kv_budget,
                "kv_token_budget": self._configured_kv_budget,
                "kv_safety_reserve_ratio": self._kv_safety_reserve_ratio,
                "model_sequence_limit": self._model_sequence_limit,
                "queue_depth": len(self._queue),
                "queued_request_ids": tuple(item.info.request_id for item in self._queue),
                "inflight_requests": len(self._active),
                "inflight_kv_tokens": self._inflight_kv_tokens,
                "inflight_reservations": tuple(
                    (request_id, info.stage, info.endpoint_id, info.reserved_sequence_tokens)
                    for request_id, info in sorted(self._active.items())
                ),
                "acquired_requests": self._acquired_requests,
                "released_requests": self._released_requests,
                "acquired_kv_tokens": self._acquired_kv_tokens,
                "released_kv_tokens": self._released_kv_tokens,
                "max_inflight_requests": self._max_inflight_requests,
                "max_inflight_kv_tokens": self._max_inflight_kv_tokens,
                "total_wait_seconds": round(self._wait_seconds, 3),
                "scheduling_timeouts": self._timeouts,
                "unsatisfiable_requests": self._unsatisfiable,
                "waits_by_stage": dict(sorted(self._waits_by_stage.items())),
                "admissions_by_endpoint": dict(sorted(self._admissions_by_endpoint.items())),
                "admitted_descriptors": tuple(asdict(info) for info in self._admitted_descriptors),
            }


__all__ = [
    "ContextBudgetExceededError",
    "ModelRequestAdmissionController",
    "ModelRequestLease",
    "ModelRequestSchedulingInfo",
    "ModelSchedulingError",
    "ModelSchedulingState",
    "SchedulingBudgetUnsatisfiableError",
    "SchedulingTimeoutError",
]
