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
    BudgetSource,
    EffectiveBudgetResult,
    ModelCallLedgerEntry,
    ModelCallLedgerStatus,
    ModelCallPurpose,
    ModelCallRecord,
    ModelRequest,
    ModelRole,
    ProviderModelResult,
)
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.effective_budget import ModelBudgetResolutionError
from novel_agent.services.model_call_ledger import (
    InMemoryModelCallLedger,
    ModelCallLedgerCollision,
    bounded_model_request_id,
)
from novel_agent.services.model_gateway import (
    ModelCallCumulativeBudgetExceeded,
    ModelCallForbiddenError,
    ModelCallUncertainError,
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


def ledger_budget(model_request: ModelRequest) -> EffectiveBudgetResult:
    gateway = ModelGateway((endpoint(model_request.model_role, FakeModelEndpoint("ledger")),))
    return gateway.resolve_effective_budget(model_request)


def test_registered_endpoint_owns_default_thinking_policy() -> None:
    class AdapterWithImplicitThinking(FakeModelEndpoint):
        default_thinking = True

    adapter = AdapterWithImplicitThinking("response")
    gateway = ModelGateway((endpoint(ModelRole.BATCH_TEST, adapter),))

    omitted = gateway.resolve_effective_budget(request())
    assert omitted.thinking_budget == 0

    explicit = RegisteredModelEndpoint(
        role=ModelRole.BATCH_TEST,
        endpoint_name="explicit-thinking-endpoint",
        model_name="fake-model",
        adapter=adapter,
        default_thinking=True,
    )
    explicit_gateway = ModelGateway((explicit,))
    declared = explicit_gateway.resolve_effective_budget(request())
    assert declared.thinking_budget == declared.context_limit // 64

    with pytest.raises(ValueError, match="default_thinking must be an explicit bool"):
        RegisteredModelEndpoint(
            role=ModelRole.BATCH_TEST,
            endpoint_name="missing-thinking-policy-endpoint",
            model_name="fake-model",
            adapter=adapter,
            default_thinking=cast(Any, None),
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
    assert fake.requests[0].request_id == request().request_id
    assert fake.requests[0].prompt == request().prompt
    ledger = gateway.call_ledger.load(request().request_id)
    assert ledger is not None
    assert ledger.status is ModelCallLedgerStatus.COMPLETED
    assert ledger.call_record == result.call_record


def test_cumulative_budget_preflight_rejects_before_provider_ledger() -> None:
    fake = FakeModelEndpoint("must not run")
    gateway = ModelGateway((endpoint(ModelRole.BATCH_TEST, fake),))
    model_request = request()

    with pytest.raises(ModelCallCumulativeBudgetExceeded, match="cannot admit request"):
        gateway.preflight_cumulative_token_budget(
            model_request,
            token_budget=1,
        )

    assert fake.requests == []
    assert gateway.call_ledger.load(model_request.request_id) is None


def test_cumulative_budget_preflight_allows_fit_and_does_not_send_early() -> None:
    fake = FakeModelEndpoint("fit")
    gateway = ModelGateway((endpoint(ModelRole.BATCH_TEST, fake),))
    model_request = request()

    resolved = gateway.preflight_cumulative_token_budget(
        model_request,
        token_budget=200_000,
    )

    assert resolved.estimated_input_tokens > 0
    assert fake.requests == []
    assert gateway.call_ledger.load(model_request.request_id) is None
    assert asyncio.run(gateway.generate_text(model_request)).text == "fit"
    assert len(fake.requests) == 1


def test_long_curator_like_request_requires_explicit_campaign_tranche() -> None:
    fake = FakeModelEndpoint("must not run during preflight")
    gateway = ModelGateway((endpoint(ModelRole.BATCH_TEST, fake),))
    model_request = request().model_copy(
        update={
            # The estimator is deliberately the same conservative UTF-8
            # byte/token estimate used by the gateway. This models the
            # observed ~52k-input + 8k-output Curator request without calling
            # a provider or depending on a tokenizer package.
            "prompt": "x" * (52_410 * 3),
            "max_output_tokens": 8_000,
        }
    )

    with pytest.raises(ModelCallCumulativeBudgetExceeded):
        gateway.preflight_cumulative_token_budget(model_request, token_budget=24_000)

    resolved = gateway.preflight_cumulative_token_budget(
        model_request,
        token_budget=128_000,
    )

    assert resolved.estimated_input_tokens >= 52_410
    assert resolved.total_output_budget == 8_000
    assert fake.requests == []
    assert gateway.call_ledger.load(model_request.request_id) is None


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
    assert batch.requests[0].request_id == request().request_id
    assert batch.requests[0].prompt == request().prompt


def test_development_call_can_explicitly_use_implementation_role() -> None:
    fake = FakeModelEndpoint("development")
    gateway = ModelGateway((endpoint(ModelRole.IMPLEMENTATION, fake),))
    development = request(role=ModelRole.IMPLEMENTATION, purpose=ModelCallPurpose.DEVELOPMENT)

    assert asyncio.run(gateway.generate_text(development)).text == "development"


def test_bound_request_without_cached_effective_budget_fails_closed() -> None:
    fake = FakeModelEndpoint("must not run")
    gateway = ModelGateway((endpoint(ModelRole.BATCH_TEST, fake),))
    bound = request().model_copy(
        update={
            "max_output_tokens": 400,
            "budget_source": BudgetSource.EXPLICIT_REQUEST,
        }
    )

    with pytest.raises(ModelBudgetResolutionError, match="no in-process EffectiveBudgetResult"):
        asyncio.run(gateway.generate_text(bound))
    assert fake.requests == []


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
    scheduled_request = request()
    scheduled_fake = FakeModelEndpoint("scheduled")
    scheduled = ModelGateway(
        (endpoint(ModelRole.BATCH_TEST, scheduled_fake),),
        admission_controller=controller,
    )
    assert scheduled.admission_controller is controller
    assert asyncio.run(scheduled.generate_text(scheduled_request)).text == "scheduled"
    scheduled_entry = scheduled.call_ledger.load(scheduled_request.request_id)
    assert scheduled_entry is not None
    scheduled_budget = scheduled_entry.effective_budget
    assert scheduled_fake.requests[0].max_output_tokens == scheduled_budget.total_output_budget
    snapshot = controller.snapshot()
    assert snapshot["released_requests"] == 1
    descriptors = cast(tuple[dict[str, object], ...], snapshot["admitted_descriptors"])
    assert len(descriptors) == 1
    descriptor = descriptors[0]
    assert descriptor["estimated_prompt_tokens"] == scheduled_budget.estimated_input_tokens
    assert descriptor["reserved_output_tokens"] == scheduled_budget.total_output_budget
    assert descriptor["safety_allowance_tokens"] == scheduled_budget.safety_allowance_tokens
    assert descriptor["reserved_sequence_tokens"] == scheduled_budget.reserved_sequence_tokens

    class FailingLedger(InMemoryModelCallLedger):
        def create_requested(self, *args: Any, **kwargs: Any) -> ModelCallLedgerEntry:
            raise RuntimeError("ledger reservation failed")

    failing_controller = ModelRequestAdmissionController(endpoint_request_limit=1)
    failing_gateway = ModelGateway(
        (endpoint(ModelRole.BATCH_TEST, FakeModelEndpoint("must not run")),),
        call_ledger=FailingLedger(),
        admission_controller=failing_controller,
    )
    with pytest.raises(RuntimeError, match="ledger reservation failed"):
        asyncio.run(failing_gateway.generate_text(request()))
    assert failing_controller.inflight_requests == 0
    assert failing_controller.snapshot()["released_requests"] == 1

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
        def __init__(self, response_text: str) -> None:
            super().__init__(response_text)
            self.dispatches = 0

        async def generate(self, model_request: ModelRequest) -> ProviderModelResult:
            self.dispatches += 1
            await asyncio.sleep(0.02)
            return await super().generate(model_request)

    slow = SlowFake("late")
    timeout_gateway = ModelGateway((endpoint(ModelRole.BATCH_TEST, slow),))
    with pytest.raises(TimeoutError):
        asyncio.run(timeout_gateway.generate_text(request(timeout=0.001)))
    timeout_entry = timeout_gateway.call_ledger.load(request().request_id)
    assert timeout_entry is not None
    assert timeout_entry.status is ModelCallLedgerStatus.UNCERTAIN
    with pytest.raises(ModelCallUncertainError, match="reconcile before retry"):
        asyncio.run(timeout_gateway.generate_text(request(timeout=0.001)))
    assert slow.dispatches == 1


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


def test_structured_output_repairs_one_observed_extra_leading_brace() -> None:
    class Output(BaseModel):
        model_config = ConfigDict(strict=True)
        answer: str

    fake = FakeModelEndpoint('\n\n{{"answer":"ok"}')
    gateway = ModelGateway((endpoint(ModelRole.BATCH_TEST, fake),))

    output, _record = asyncio.run(gateway.generate_structured(request(), Output))

    assert output.answer == "ok"
    assert len(fake.requests) == 1
    entry = gateway.call_ledger.load(request().request_id)
    assert entry is not None
    assert entry.status is ModelCallLedgerStatus.COMPLETED
    assert gateway.structured_validation_attempts == []


def test_structured_output_does_not_accept_other_brace_shapes() -> None:
    class Output(BaseModel):
        model_config = ConfigDict(strict=True)
        answer: str

    fake = FakeModelEndpoint('{{"answer":"ok"}}')
    gateway = ModelGateway((endpoint(ModelRole.BATCH_TEST, fake),))

    with pytest.raises(ValidationError):
        asyncio.run(gateway.generate_structured(request(), Output))


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


def test_structured_retry_preserves_attempt_scope_for_maximal_parent_id() -> None:
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

    parent = request().model_copy(
        update={
            "request_id": StableId("q" * 128),
            "attempt_id": StableId("attempt.structured-retry"),
        }
    )
    assert bounded_model_request_id(parent, ".schema-retry1").root == (
        "model-request.attempt.structured-retry.652713bcb795a6978fa84aa8.schema-retry1"
    )
    fake = SequenceEndpoint()
    gateway = ModelGateway((endpoint(ModelRole.BATCH_TEST, fake),), structured_max_retries=1)

    output, _record = asyncio.run(gateway.generate_structured(parent, Output))

    assert output.answer == "corrected"
    assert fake.requests[1].request_id.root == (
        "model-request.attempt.structured-retry.652713bcb795a6978fa84aa8.schema-retry1"
    )


def test_long_parent_child_ids_keep_sibling_identity_in_attempt_scope() -> None:
    parent = request().model_copy(
        update={
            "request_id": StableId("q" * 128),
            "attempt_id": StableId("attempt.graph"),
        }
    )
    sibling = parent.model_copy(update={"request_id": StableId("r" * 128)})

    first = bounded_model_request_id(parent, ".semantic-verifier")
    second = bounded_model_request_id(sibling, ".semantic-verifier")

    assert first != second
    assert first.root.startswith("model-request.attempt.graph.")
    assert second.root.startswith("model-request.attempt.graph.")


def test_child_model_request_identity_fails_closed_without_bounded_scope() -> None:
    parent = request().model_copy(
        update={
            "request_id": StableId("q" * 128),
            "task_id": TaskId("t" * 128),
            "attempt_id": None,
        }
    )

    with pytest.raises(ValueError, match="no bounded"):
        bounded_model_request_id(parent, ".schema-retry1")


def test_structured_retry_identity_failure_is_model_routing_error() -> None:
    class Output(BaseModel):
        model_config = ConfigDict(strict=True)
        answer: str

    parent = request().model_copy(
        update={
            "request_id": StableId("q" * 128),
            "task_id": TaskId("t" * 128),
            "attempt_id": None,
        }
    )
    fake = FakeModelEndpoint('{"answer":1}')
    gateway = ModelGateway((endpoint(ModelRole.BATCH_TEST, fake),), structured_max_retries=1)

    with pytest.raises(ModelRoutingError, match="structured retry request identity"):
        asyncio.run(gateway.generate_structured(parent, Output))

    assert len(fake.requests) == 1


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


def test_structured_terminal_validation_attaches_exact_attribution() -> None:
    # Round-19: on the terminal structured-validation failure, the gateway must
    # preserve the exact request id and raw-response hash of the FAILING
    # attempt (the schema-retry suffix included) so the rejection audit can
    # attribute the defect to the actual failing request.
    class Output(BaseModel):
        model_config = ConfigDict(strict=True)
        answer: str

    fake = FakeModelEndpoint('{"answer":1}')
    gateway = ModelGateway((endpoint(ModelRole.BATCH_TEST, fake),), structured_max_retries=1)

    with pytest.raises(ValidationError) as raised:
        asyncio.run(gateway.generate_structured(request(), Output))

    error = cast(Any, raised.value)
    assert error._structured_request_id == "model.request.1.schema-retry1"
    raw_hash = error._structured_raw_response_hash
    assert raw_hash == sha256_id(b'{"answer":1}')
    # The recorded failing entry matches the attached attribution exactly.
    failing = gateway.call_ledger.list_for_prefix("model.request.1.schema-retry1")
    assert len(failing) == 1
    assert failing[0].request_id.root == "model.request.1.schema-retry1"
    assert failing[0].raw_response_hash == raw_hash


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
    effective_budget = ledger_budget(model_request)
    requested = ledger.create_requested(
        model_request,
        effective_budget=effective_budget,
        reasoning_included_in_completion_tokens=False,
    )
    assert (
        ledger.create_requested(
            model_request,
            effective_budget=effective_budget,
            reasoning_included_in_completion_tokens=False,
        )
        == requested
    )
    with pytest.raises(ModelCallLedgerCollision, match="identity collision"):
        ledger.create_requested(
            model_request.model_copy(update={"prompt": "changed"}),
            effective_budget=effective_budget,
            reasoning_included_in_completion_tokens=False,
        )
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
    effective_budget = ledger_budget(parent)
    ledger.create_requested(
        parent,
        effective_budget=effective_budget,
        reasoning_included_in_completion_tokens=False,
    )
    ledger.create_requested(
        verifier,
        effective_budget=effective_budget,
        reasoning_included_in_completion_tokens=False,
    )
    ledger.create_requested(
        collision,
        effective_budget=effective_budget,
        reasoning_included_in_completion_tokens=False,
    )

    assert tuple(
        item.request_id.root for item in ledger.list_for_prefix(parent.request_id.root)
    ) == (
        parent.request_id.root,
        verifier.request_id.root,
    )


def test_model_call_ledger_entry_rejects_missing_terminal_evidence() -> None:
    ledger = InMemoryModelCallLedger()
    model_request = request()
    requested = ledger.create_requested(
        model_request,
        effective_budget=ledger_budget(model_request),
        reasoning_included_in_completion_tokens=False,
    )
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
