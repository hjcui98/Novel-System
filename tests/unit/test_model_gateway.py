from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.domain.ids import RunId, StableId, TaskId
from novel_agent.domain.model_calls import (
    ModelCallLedgerEntry,
    ModelCallLedgerStatus,
    ModelCallPurpose,
    ModelCallRecord,
    ModelRequest,
    ModelRole,
    ProviderModelResult,
)
from novel_agent.services.model_call_ledger import (
    InMemoryModelCallLedger,
    ModelCallLedgerCollision,
)
from novel_agent.services.model_gateway import (
    ModelCallForbiddenError,
    ModelGateway,
    ModelRoutingError,
    RegisteredModelEndpoint,
    StructuredGenerationExhausted,
)
from novel_agent.services.model_request_admission import (
    ModelRequestAdmissionController,
    SchedulingBudgetUnsatisfiableError,
)


def request(
    *,
    role: ModelRole = ModelRole.BATCH_TEST,
    purpose: ModelCallPurpose = ModelCallPurpose.BATCH_TEST,
    timeout: float = 1.0,
) -> ModelRequest:
    return ModelRequest(
        request_id=StableId("model.request.1"),
        run_id=RunId("run.test"),
        task_id=TaskId("task.test"),
        model_role=role,
        purpose=purpose,
        trace_id="trace-model-test",
        span_id="span-model-test",
        prompt="deterministic fixture",
        timeout_seconds=timeout,
    )


def endpoint(role: ModelRole, adapter: FakeModelEndpoint) -> RegisteredModelEndpoint:
    return RegisteredModelEndpoint(
        role=role,
        endpoint_name=f"{role.value}-endpoint",
        model_name="fake-model",
        adapter=adapter,
    )


def test_fake_model_records_complete_batch_role_audit() -> None:
    fake = FakeModelEndpoint("fixed response")
    gateway = ModelGateway((endpoint(ModelRole.BATCH_TEST, fake),))

    result = asyncio.run(gateway.generate_text(request()))

    assert result.text == "fixed response"
    assert result.call_record.model_role is ModelRole.BATCH_TEST
    assert result.call_record.endpoint == "batch_test_model-endpoint"
    assert result.call_record.model == "fake-model"
    assert result.call_record.model_version == "fake-v1"
    assert result.call_record.trace_id == "trace-model-test"
    assert result.call_record.span_id == "span-model-test"
    assert result.call_record.usage.cost_usd == Decimal("0")
    assert result.call_record.latency_ms >= 0
    assert fake.requests == [request()]
    ledger = gateway.call_ledger.load(request().request_id)
    assert ledger is not None
    assert ledger.status is ModelCallLedgerStatus.COMPLETED
    assert ledger.call_record == result.call_record


@pytest.mark.model_required
def test_model_required_smoke_routes_exclusively_to_batch_test_role() -> None:
    implementation = FakeModelEndpoint("must not run")
    batch = FakeModelEndpoint("batch smoke response")
    gateway = ModelGateway(
        (
            endpoint(ModelRole.IMPLEMENTATION, implementation),
            endpoint(ModelRole.BATCH_TEST, batch),
        )
    )

    result = asyncio.run(gateway.generate_text(request()))

    assert result.call_record.model_role is ModelRole.BATCH_TEST
    assert implementation.requests == []
    assert batch.requests == [request()]


def test_development_call_can_explicitly_use_implementation_role() -> None:
    fake = FakeModelEndpoint("development")
    gateway = ModelGateway((endpoint(ModelRole.IMPLEMENTATION, fake),))
    development = request(role=ModelRole.IMPLEMENTATION, purpose=ModelCallPurpose.DEVELOPMENT)

    assert asyncio.run(gateway.generate_text(development)).text == "development"


@pytest.mark.parametrize("purpose", [ModelCallPurpose.BATCH_TEST, ModelCallPurpose.EVALUATION])
def test_batch_purposes_cannot_route_to_implementation_model(
    purpose: ModelCallPurpose,
) -> None:
    fake = FakeModelEndpoint("must not run")
    gateway = ModelGateway((endpoint(ModelRole.IMPLEMENTATION, fake),))
    invalid = request(role=ModelRole.IMPLEMENTATION, purpose=purpose)

    with pytest.raises(ModelRoutingError, match="without fallback"):
        asyncio.run(gateway.generate_text(invalid))
    assert fake.requests == []


def test_missing_role_never_falls_back_to_another_endpoint() -> None:
    fake = FakeModelEndpoint("must not run")
    gateway = ModelGateway((endpoint(ModelRole.IMPLEMENTATION, fake),))

    with pytest.raises(ModelRoutingError, match="no endpoint configured"):
        asyncio.run(gateway.generate_text(request()))
    assert fake.requests == []


def test_duplicate_role_configuration_is_rejected() -> None:
    first = endpoint(ModelRole.BATCH_TEST, FakeModelEndpoint("one"))
    second = endpoint(ModelRole.BATCH_TEST, FakeModelEndpoint("two"))

    with pytest.raises(ModelRoutingError, match="at most one"):
        ModelGateway((first, second))


def test_gateway_scheduler_configuration_and_endpoint_policy_identity() -> None:
    with pytest.raises(ValueError, match="scheduling timeout"):
        ModelGateway((), scheduling_timeout_seconds=0)
    gateway = ModelGateway((endpoint(ModelRole.BATCH_TEST, FakeModelEndpoint("ok")),))
    policy = dict(gateway.endpoint_policy_identity(ModelRole.BATCH_TEST))
    assert policy["endpoint_name"] == "batch_test_model-endpoint"
    assert policy["registered_model"] == "fake-model"
    with pytest.raises(ModelRoutingError, match="no endpoint configured"):
        gateway.endpoint_policy_identity(ModelRole.IMPLEMENTATION)

    controller = ModelRequestAdmissionController(endpoint_request_limit=1)
    scheduled = ModelGateway(
        (endpoint(ModelRole.BATCH_TEST, FakeModelEndpoint("scheduled")),),
        admission_controller=controller,
    )
    assert scheduled.admission_controller is controller
    assert asyncio.run(scheduled.generate_text(request())).text == "scheduled"
    assert controller.snapshot()["released_requests"] == 1

    unsatisfiable = ModelGateway(
        (endpoint(ModelRole.BATCH_TEST, FakeModelEndpoint("never")),),
        admission_controller=ModelRequestAdmissionController(
            endpoint_request_limit=1,
            kv_token_budget=100,
            kv_safety_reserve_ratio=0.0,
        ),
    )
    with pytest.raises(SchedulingBudgetUnsatisfiableError):
        asyncio.run(unsatisfiable.generate_text(request()))


def test_gateway_cancelled_admission_releases_eventual_lease() -> None:
    async def exercise() -> None:
        controller = ModelRequestAdmissionController(endpoint_request_limit=1)
        blocker = controller.acquire(1)
        gateway = ModelGateway(
            (endpoint(ModelRole.BATCH_TEST, FakeModelEndpoint("never")),),
            admission_controller=controller,
        )
        task = asyncio.create_task(gateway.generate_text(request()))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        blocker.release()
        for _ in range(100):
            snapshot = controller.snapshot()
            if snapshot["acquired_requests"] == 2 and controller.inflight_requests == 0:
                break
            await asyncio.sleep(0.01)
        assert controller.snapshot()["acquired_requests"] == 2
        assert controller.inflight_requests == 0

        with pytest.raises(RuntimeError, match="not configured"):
            await ModelGateway(())._acquire_scheduled_lease(
                ModelGateway(())._scheduling_info(request(), "endpoint")
            )

    asyncio.run(exercise())


def test_cancelled_gateway_drops_late_admission_error() -> None:
    async def exercise() -> None:
        controller = ModelRequestAdmissionController(endpoint_request_limit=1)

        def delayed_failure(*_args: object, **_kwargs: object) -> None:
            time.sleep(0.03)
            raise RuntimeError("late scheduling failure")

        cast(Any, controller).acquire = delayed_failure
        gateway = ModelGateway(
            (endpoint(ModelRole.BATCH_TEST, FakeModelEndpoint("never")),),
            admission_controller=controller,
        )
        task = asyncio.create_task(gateway.generate_text(request()))
        await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)

    asyncio.run(exercise())


@pytest.mark.parametrize("retry_count", [-1, 3])
def test_invalid_structured_retry_configuration_is_rejected(retry_count: int) -> None:
    with pytest.raises(ValueError, match="structured retries"):
        ModelGateway((), structured_max_retries=retry_count)


def test_external_calls_can_be_forbidden_while_fake_calls_remain_available() -> None:
    class ExternalFake(FakeModelEndpoint):
        is_external = True

    external = ExternalFake("must not run")
    blocked = ModelGateway((endpoint(ModelRole.BATCH_TEST, external),), forbid_external_calls=True)
    local = FakeModelEndpoint("local")
    allowed = ModelGateway((endpoint(ModelRole.BATCH_TEST, local),), forbid_external_calls=True)

    with pytest.raises(ModelCallForbiddenError, match="disabled"):
        asyncio.run(blocked.generate_text(request()))
    assert asyncio.run(allowed.generate_text(request())).text == "local"


def test_fake_model_failure_and_timeout_are_not_silently_retried() -> None:
    failing = FakeModelEndpoint("", error=RuntimeError("provider failed"))
    gateway = ModelGateway((endpoint(ModelRole.BATCH_TEST, failing),))

    with pytest.raises(RuntimeError, match="provider failed"):
        asyncio.run(gateway.generate_text(request()))
    assert len(failing.requests) == 1
    failed_entry = gateway.call_ledger.load(request().request_id)
    assert failed_entry is not None
    assert failed_entry.status is ModelCallLedgerStatus.TRANSPORT_EXHAUSTED

    class SlowFake(FakeModelEndpoint):
        async def generate(self, model_request: ModelRequest) -> ProviderModelResult:
            await asyncio.sleep(0.02)
            return await super().generate(model_request)

    slow = SlowFake("late")
    timeout_gateway = ModelGateway((endpoint(ModelRole.BATCH_TEST, slow),))
    with pytest.raises(TimeoutError):
        asyncio.run(timeout_gateway.generate_text(request(timeout=0.001)))
    timeout_entry = timeout_gateway.call_ledger.load(request().request_id)
    assert timeout_entry is not None
    assert timeout_entry.status is ModelCallLedgerStatus.UNCERTAIN


def test_structured_output_uses_the_requested_domain_type() -> None:
    class Output(BaseModel):
        model_config = ConfigDict(strict=True)
        answer: str

    gateway = ModelGateway((endpoint(ModelRole.BATCH_TEST, FakeModelEndpoint('{"answer":"ok"}')),))
    output, record = asyncio.run(gateway.generate_structured(request(), Output))

    assert output.answer == "ok"
    assert record.model_role is ModelRole.BATCH_TEST

    invalid = ModelGateway((endpoint(ModelRole.BATCH_TEST, FakeModelEndpoint('{"answer":1}')),))
    with pytest.raises(ValidationError):
        asyncio.run(invalid.generate_structured(request(), Output))


def test_structured_output_retries_domain_validation_with_feedback() -> None:
    class Output(BaseModel):
        model_config = ConfigDict(strict=True)
        answer: str

    class SequenceEndpoint(FakeModelEndpoint):
        def __init__(self) -> None:
            super().__init__("")
            self.responses = iter(('{"answer":1}', '{"answer":"corrected"}'))

        async def generate(self, model_request: ModelRequest) -> ProviderModelResult:
            self.response_text = next(self.responses)
            return await super().generate(model_request)

    fake = SequenceEndpoint()
    gateway = ModelGateway((endpoint(ModelRole.BATCH_TEST, fake),), structured_max_retries=1)

    output, record = asyncio.run(gateway.generate_structured(request(), Output))

    assert output.answer == "corrected"
    assert len(fake.requests) == 2
    assert fake.requests[0].request_id.root == "model.request.1"
    assert fake.requests[1].request_id.root == "model.request.1.schema-retry1"
    assert fake.requests[1].response_schema == fake.requests[0].response_schema
    assert "STRUCTURED_OUTPUT_RETRY" in fake.requests[1].prompt
    assert "input_value" not in fake.requests[1].prompt
    assert record.request_id == fake.requests[1].request_id
    assert len(gateway.structured_validation_attempts) == 1
    assert gateway.structured_validation_attempts[0].request_id == "model.request.1"


def test_structured_output_retry_limit_is_enforced() -> None:
    class Output(BaseModel):
        model_config = ConfigDict(strict=True)
        answer: str

    fake = FakeModelEndpoint('{"answer":1}')
    gateway = ModelGateway((endpoint(ModelRole.BATCH_TEST, fake),), structured_max_retries=1)

    with pytest.raises(ValidationError):
        asyncio.run(gateway.generate_structured(request(), Output))

    assert len(fake.requests) == 2
    assert len(gateway.structured_validation_attempts) == 2
    entries = gateway.call_ledger.list_for_prefix(request().request_id.root)
    assert tuple(item.status for item in entries) == (
        ModelCallLedgerStatus.VALIDATION_REJECTED,
        ModelCallLedgerStatus.VALIDATION_REJECTED,
    )


def test_structured_audited_exhaustion_carries_all_durable_entries() -> None:
    class Output(BaseModel):
        model_config = ConfigDict(strict=True)
        answer: str

    gateway = ModelGateway(
        (endpoint(ModelRole.BATCH_TEST, FakeModelEndpoint('{"answer":1}')),),
        structured_max_retries=1,
    )

    with pytest.raises(StructuredGenerationExhausted) as raised:
        asyncio.run(gateway.generate_structured_audited(request(), Output))

    assert len(raised.value.entries) == 2
    assert all(
        item.status is ModelCallLedgerStatus.VALIDATION_REJECTED for item in raised.value.entries
    )


def test_model_call_ledger_cas_rejects_identity_and_terminal_overwrites() -> None:
    ledger = InMemoryModelCallLedger()
    model_request = request()
    requested = ledger.create_requested(model_request)
    assert ledger.create_requested(model_request) == requested
    with pytest.raises(ModelCallLedgerCollision, match="identity collision"):
        ledger.create_requested(model_request.model_copy(update={"prompt": "changed"}))
    with pytest.raises(KeyError, match="was not reserved"):
        ledger.settle(
            requested.model_copy(update={"request_id": StableId("model.request.missing")})
        )
    with pytest.raises(ModelCallLedgerCollision, match="settlement identity"):
        ledger.settle(requested.model_copy(update={"run_id": RunId("run.other")}))

    gateway = ModelGateway(
        (endpoint(ModelRole.BATCH_TEST, FakeModelEndpoint("ok")),),
        call_ledger=ledger,
    )
    asyncio.run(gateway.generate_text(model_request))
    completed = ledger.load(model_request.request_id)
    assert completed is not None
    with pytest.raises(ModelCallLedgerCollision, match="cannot be overwritten"):
        ledger.settle(
            completed.model_copy(
                update={
                    "status": ModelCallLedgerStatus.TRANSPORT_EXHAUSTED,
                    "transport_error_type": "late",
                }
            )
        )
    assert ledger.list_for_prefix("unrelated") == ()


def test_model_call_ledger_prefix_includes_all_scoped_child_calls() -> None:
    ledger = InMemoryModelCallLedger()
    parent = request()
    verifier = parent.model_copy(
        update={"request_id": StableId(f"{parent.request_id.root}.semantic-verifier")}
    )
    collision = parent.model_copy(update={"request_id": StableId(f"{parent.request_id.root}0")})
    ledger.create_requested(parent)
    ledger.create_requested(verifier)
    ledger.create_requested(collision)

    assert tuple(
        item.request_id.root for item in ledger.list_for_prefix(parent.request_id.root)
    ) == (
        parent.request_id.root,
        verifier.request_id.root,
    )


def test_model_call_ledger_entry_rejects_missing_terminal_evidence() -> None:
    ledger = InMemoryModelCallLedger()
    requested = ledger.create_requested(request())
    now = datetime.now(UTC)
    for updates, message in (
        (
            {"status": ModelCallLedgerStatus.COMPLETED},
            "requires response hash",
        ),
        (
            {
                "status": ModelCallLedgerStatus.VALIDATION_REJECTED,
                "raw_response_hash": requested.request_hash,
                "call_record": _call_record_for_test(),
                "completed_at": now,
            },
            "requires safe validation detail",
        ),
        (
            {"status": ModelCallLedgerStatus.TRANSPORT_EXHAUSTED},
            "requires error evidence",
        ),
    ):
        with pytest.raises(ValidationError, match=message):
            ModelCallLedgerEntry.model_validate(requested.model_dump(mode="python") | updates)


def _call_record_for_test() -> ModelCallRecord:
    gateway = ModelGateway((endpoint(ModelRole.BATCH_TEST, FakeModelEndpoint("ok")),))
    return asyncio.run(gateway.generate_text(request())).call_record


def test_structured_validation_fails_closed_when_ledger_loses_completed_call() -> None:
    class Output(BaseModel):
        answer: str

    class VanishingLedger(InMemoryModelCallLedger):
        def load(self, request_id: StableId) -> ModelCallLedgerEntry | None:
            entry = super().load(request_id)
            if entry is not None and entry.completed_at is not None:
                return None
            return entry

    gateway = ModelGateway(
        (endpoint(ModelRole.BATCH_TEST, FakeModelEndpoint('{"answer":1}')),),
        call_ledger=VanishingLedger(),
    )
    with pytest.raises(AssertionError, match="missing from ledger"):
        asyncio.run(gateway.generate_structured(request(), Output))


def test_structured_retry_serializes_validator_context_without_raw_input() -> None:
    from pydantic import model_validator

    class Output(BaseModel):
        answer: str

        @model_validator(mode="after")
        def reject_answer(self) -> Output:
            raise ValueError("answer violates business rule")

    fake = FakeModelEndpoint('{"answer":"bad"}')
    gateway = ModelGateway((endpoint(ModelRole.BATCH_TEST, fake),), structured_max_retries=1)

    with pytest.raises(ValidationError):
        asyncio.run(gateway.generate_structured(request(), Output))

    assert len(fake.requests) == 2
    assert "answer violates business rule" in fake.requests[1].prompt
    assert '"input"' not in fake.requests[1].prompt
