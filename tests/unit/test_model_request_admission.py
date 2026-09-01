from __future__ import annotations

import threading
import time
from typing import Any, cast

import pytest

from novel_agent.services.claim_support import TrustedClaimSupportProducer
from novel_agent.services.model_request_admission import (
    ContextBudgetExceededError,
    ModelRequestAdmissionController,
    ModelRequestSchedulingInfo,
    SchedulingBudgetUnsatisfiableError,
    SchedulingTimeoutError,
)


def test_producer_delegates_capacity_to_shared_controller() -> None:
    controller = ModelRequestAdmissionController(
        endpoint_request_limit=2,
        kv_token_budget=1000,
    )
    producer = TrustedClaimSupportProducer(
        max_concurrent_needs=1,
        admission_controller=controller,
    )
    producer._acquire_kv_capacity(400)
    assert controller.inflight_requests == 1
    assert controller.inflight_kv_tokens == 400
    producer._release_kv_capacity(400)
    assert controller.inflight_requests == 0
    assert controller.inflight_kv_tokens == 0


def test_admission_controller_enforces_request_and_kv_limits() -> None:
    controller = ModelRequestAdmissionController(
        endpoint_request_limit=2,
        kv_token_budget=1000,
    )
    controller.acquire(300)
    controller.acquire(300)
    assert controller.inflight_requests == 2
    assert controller.inflight_kv_tokens == 600

    waiting: list[bool] = []

    def blocked_acquire() -> None:
        controller.acquire(300)
        waiting.append(True)

    thread = threading.Thread(target=blocked_acquire)
    thread.start()
    time.sleep(0.1)
    assert controller.inflight_requests == 2
    assert not waiting

    controller.release(300)
    thread.join(timeout=5)
    assert waiting == [True]
    assert controller.inflight_requests == 2
    assert controller.inflight_kv_tokens == 600

    controller.release(300)
    controller.release(300)
    assert controller.inflight_requests == 0
    assert controller.inflight_kv_tokens == 0


def test_admission_controller_waits_for_kv_budget_with_slots_free() -> None:
    controller = ModelRequestAdmissionController(
        endpoint_request_limit=4,
        kv_token_budget=500,
    )
    controller.acquire(400)
    assert controller.inflight_requests == 1

    waiting: list[bool] = []

    def blocked_acquire() -> None:
        controller.acquire(200)
        waiting.append(True)

    thread = threading.Thread(target=blocked_acquire)
    thread.start()
    time.sleep(0.1)
    assert controller.inflight_requests == 1
    assert not waiting

    controller.release(400)
    thread.join(timeout=5)
    assert waiting == [True]
    assert controller.inflight_kv_tokens == 200


def test_admission_controller_rejects_oversized_requests_without_bypass() -> None:
    controller = ModelRequestAdmissionController(
        endpoint_request_limit=1,
        kv_token_budget=100,
        kv_safety_reserve_ratio=0.2,
    )
    with pytest.raises(SchedulingBudgetUnsatisfiableError, match="UNSATISFIABLE"):
        controller.acquire(81)
    assert controller.inflight_kv_tokens == 0
    assert controller.snapshot()["effective_kv_token_budget"] == 80


def test_admission_controller_distinguishes_context_limit_from_application_budget() -> None:
    controller = ModelRequestAdmissionController(
        endpoint_request_limit=1,
        kv_token_budget=200_000,
        model_sequence_limit=100,
    )
    with pytest.raises(ContextBudgetExceededError, match="CONTEXT_BUDGET_EXCEEDED"):
        controller.acquire(101)


def test_admission_controller_timeout_and_lease_double_release_are_typed() -> None:
    controller = ModelRequestAdmissionController(
        endpoint_request_limit=1,
        kv_token_budget=1_000,
        kv_safety_reserve_ratio=0.0,
    )
    first = controller.acquire(100)
    with pytest.raises(SchedulingTimeoutError, match="SCHEDULING_TIMEOUT") as raised:
        controller.acquire(100, timeout_seconds=0.02)
    assert raised.value.queue_snapshot["queue_depth"] == 0
    assert controller.snapshot()["queue_depth"] == 0
    first.release()
    with pytest.raises(RuntimeError, match="released twice"):
        first.release()
    assert controller.snapshot()["scheduling_timeouts"] == 1
    assert controller.snapshot()["acquired_requests"] == controller.snapshot()["released_requests"]


def test_admission_timeout_reports_active_and_queued_request_ids() -> None:
    controller = ModelRequestAdmissionController(endpoint_request_limit=1)
    blocker = controller.acquire(1)

    with pytest.raises(SchedulingTimeoutError, match=r"active=.*legacy-0"):
        controller.acquire(1, timeout_seconds=0.02)

    blocker.release()


def test_admission_descriptor_has_stable_priority_queue_and_endpoint_telemetry() -> None:
    controller = ModelRequestAdmissionController(
        endpoint_request_limit=1,
        kv_token_budget=1_000,
        kv_safety_reserve_ratio=0.0,
    )

    def info(request_id: str, priority: int) -> ModelRequestSchedulingInfo:
        return ModelRequestSchedulingInfo(
            request_id=request_id,
            endpoint_id="local-8002",
            need_id=f"need-{request_id}",
            stage="planner",
            estimated_prompt_tokens=50,
            reserved_output_tokens=25,
            safety_allowance_tokens=5,
            reserved_sequence_tokens=80,
            dependency_ids=(),
            context_hash=f"sha256:{request_id}",
            priority=priority,
        )

    blocker = controller.acquire(info("blocker", 50))
    order: list[str] = []

    def worker(request_id: str, priority: int) -> None:
        with controller.acquire(info(request_id, priority), timeout_seconds=2):
            order.append(request_id)

    low = threading.Thread(target=worker, args=("low", 10))
    high = threading.Thread(target=worker, args=("high", 90))
    low.start()
    time.sleep(0.02)
    high.start()
    time.sleep(0.02)
    blocker.release()
    low.join(timeout=2)
    high.join(timeout=2)
    assert order == ["high", "low"]
    snapshot = controller.snapshot()
    assert snapshot["admissions_by_endpoint"] == {"local-8002": 3}
    assert snapshot["waits_by_stage"] == {"planner": 2}


def test_admission_controller_rejects_invalid_constructor_values() -> None:
    with pytest.raises(ValueError, match="request limit"):
        ModelRequestAdmissionController(endpoint_request_limit=0)
    with pytest.raises(ValueError, match="KV token budget"):
        ModelRequestAdmissionController(endpoint_request_limit=1, kv_token_budget=0)
    with pytest.raises(ValueError, match="reserve ratio"):
        ModelRequestAdmissionController(endpoint_request_limit=1, kv_safety_reserve_ratio=1.5)
    with pytest.raises(ValueError, match="sequence limit"):
        ModelRequestAdmissionController(endpoint_request_limit=1, model_sequence_limit=0)
    with pytest.raises(ValueError, match="scheduling timeout"):
        ModelRequestAdmissionController(
            endpoint_request_limit=1, default_scheduling_timeout_seconds=0
        )
    with pytest.raises(ValueError, match="effective KV"):
        ModelRequestAdmissionController(
            endpoint_request_limit=1,
            kv_token_budget=1,
            kv_safety_reserve_ratio=0.999,
        )
    with pytest.raises(ValueError, match="must be positive"):
        controller = ModelRequestAdmissionController(endpoint_request_limit=1)
        controller.acquire(0)
    with pytest.raises(ValueError, match="must be positive"):
        controller.release(0)


def test_admission_descriptor_and_release_invariant_edges() -> None:
    base = {
        "request_id": "request",
        "endpoint_id": "endpoint",
        "need_id": None,
        "stage": "test",
        "estimated_prompt_tokens": 2,
        "reserved_output_tokens": 1,
        "safety_allowance_tokens": 1,
        "reserved_sequence_tokens": 4,
        "dependency_ids": (),
        "context_hash": "sha256:test",
    }
    descriptor_type = cast(Any, ModelRequestSchedulingInfo)
    for field in ("request_id", "endpoint_id", "stage", "context_hash"):
        with pytest.raises(ValueError, match="identities"):
            descriptor_type(**(base | {field: ""}))
    with pytest.raises(ValueError, match="cannot be negative"):
        descriptor_type(**(base | {"estimated_prompt_tokens": -1}))
    with pytest.raises(ValueError, match="must equal"):
        descriptor_type(**(base | {"reserved_sequence_tokens": 3}))
    with pytest.raises(ValueError, match="priority"):
        descriptor_type(**(base | {"priority": 101}))
    with pytest.raises(ValueError, match="scheduling timeout"):
        descriptor_type(**(base | {"scheduling_timeout_seconds": 0}))

    controller = ModelRequestAdmissionController(endpoint_request_limit=1)
    descriptor = descriptor_type(**base)
    lease = controller.acquire(descriptor)
    assert lease.released is False
    with pytest.raises(RuntimeError, match="already active"):
        controller.acquire(descriptor)
    with pytest.raises(RuntimeError, match="no matching"):
        controller.release(99)
    with pytest.raises(RuntimeError, match="not active"):
        controller._release_reservation("missing")
    controller._inflight_kv_tokens = 0
    with pytest.raises(RuntimeError, match="underflow"):
        lease.release()


def test_admission_descriptor_deadline_can_preempt_default_timeout() -> None:
    controller = ModelRequestAdmissionController(endpoint_request_limit=1)
    blocker = controller.acquire(1)
    descriptor = ModelRequestSchedulingInfo(
        request_id="deadline",
        endpoint_id="endpoint",
        need_id=None,
        stage="test",
        estimated_prompt_tokens=1,
        reserved_output_tokens=0,
        safety_allowance_tokens=0,
        reserved_sequence_tokens=1,
        dependency_ids=(),
        context_hash="sha256:deadline",
        scheduling_deadline=time.monotonic() + 0.01,
    )
    with pytest.raises(SchedulingTimeoutError):
        controller.acquire(descriptor, timeout_seconds=1)
    blocker.release()


def test_admission_controller_snapshot_counts_and_wait_time() -> None:
    controller = ModelRequestAdmissionController(
        endpoint_request_limit=2,
        kv_token_budget=1000,
    )
    controller.acquire(250)
    controller.acquire(250)
    controller.release(250)
    snapshot = controller.snapshot()
    assert snapshot["acquired_requests"] == 2
    assert snapshot["released_requests"] == 1
    assert snapshot["acquired_kv_tokens"] == 500
    assert snapshot["released_kv_tokens"] == 250
    assert snapshot["inflight_requests"] == 1
    assert snapshot["inflight_kv_tokens"] == 250
    wait_seconds = snapshot["total_wait_seconds"]
    assert isinstance(wait_seconds, float)
    assert wait_seconds >= 0.0
    assert snapshot["kv_safety_reserve_ratio"] == 0.2
    assert snapshot["effective_kv_token_budget"] == 800


def test_admission_controller_is_shared_across_threads() -> None:
    controller = ModelRequestAdmissionController(
        endpoint_request_limit=2,
        kv_token_budget=400,
    )
    errors: list[BaseException] = []
    results: list[int] = []

    def worker(index: int) -> None:
        try:
            controller.acquire(100)
            time.sleep(0.02)
            controller.release(100)
            results.append(index)
        except BaseException as error:  # pragma: no cover - assertion path
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors
    assert sorted(results) == [0, 1, 2, 3]
    assert controller.inflight_requests == 0
    assert controller.inflight_kv_tokens == 0
    assert controller.acquired_request_count == 4
    snapshot = controller.snapshot()
    assert snapshot["released_requests"] == 4
    assert snapshot["acquired_kv_tokens"] == 400
    assert snapshot["released_kv_tokens"] == 400


def test_admission_limit_one_is_strictly_serial() -> None:
    controller = ModelRequestAdmissionController(endpoint_request_limit=1)
    first = controller.acquire(10)
    waiting: list[bool] = []

    def blocked() -> None:
        with controller.acquire(10, timeout_seconds=2):
            waiting.append(True)

    thread = threading.Thread(target=blocked)
    thread.start()
    time.sleep(0.05)
    assert controller.inflight_requests == 1
    assert not waiting
    first.release()
    thread.join(timeout=5)
    assert waiting == [True]
    assert controller.inflight_requests == 0
    snapshot = controller.snapshot()
    assert snapshot["acquired_requests"] == snapshot["released_requests"]
    assert snapshot["max_inflight_requests"] == 1


def test_admission_timeout_clears_queue_and_cancel_releases_waiter() -> None:
    controller = ModelRequestAdmissionController(endpoint_request_limit=1)
    blocker = controller.acquire(1)
    errors: list[BaseException] = []

    def blocked() -> None:
        try:
            controller.acquire(1, timeout_seconds=5)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=blocked)
    thread.start()
    time.sleep(0.05)
    snapshot = controller.snapshot()
    assert snapshot["queue_depth"] == 1
    queued_ids = cast(tuple[str, ...], snapshot["queued_request_ids"])
    assert controller.abandon_request(queued_ids[0]) is True
    thread.join(timeout=5)
    assert controller.snapshot()["queue_depth"] == 0
    assert errors
    assert "cancelled" in str(errors[0])
    blocker.release()
    assert controller.abandon_request("missing-request") is False
    assert controller.snapshot()["acquired_requests"] == controller.snapshot()["released_requests"]


def test_admission_wait_then_acquires_capacity() -> None:
    controller = ModelRequestAdmissionController(endpoint_request_limit=1)
    blocker = controller.acquire(1)
    acquired: list[str] = []

    def waiter() -> None:
        with controller.acquire(1, timeout_seconds=2) as lease:
            acquired.append(lease.info.request_id)

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.05)
    assert not acquired
    blocker.release()
    thread.join(timeout=5)
    assert len(acquired) == 1
    snapshot = controller.snapshot()
    assert snapshot["inflight_requests"] == 0
    assert snapshot["acquired_requests"] == snapshot["released_requests"]
