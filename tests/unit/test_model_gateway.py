from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.domain.ids import RunId, StableId, TaskId
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelRequest,
    ModelRole,
    ProviderModelResult,
)
from novel_agent.services.model_gateway import (
    ModelCallForbiddenError,
    ModelGateway,
    ModelRoutingError,
    RegisteredModelEndpoint,
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

    class SlowFake(FakeModelEndpoint):
        async def generate(self, model_request: ModelRequest) -> ProviderModelResult:
            await asyncio.sleep(0.02)
            return await super().generate(model_request)

    slow = SlowFake("late")
    timeout_gateway = ModelGateway((endpoint(ModelRole.BATCH_TEST, slow),))
    with pytest.raises(TimeoutError):
        asyncio.run(timeout_gateway.generate_text(request(timeout=0.001)))


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
