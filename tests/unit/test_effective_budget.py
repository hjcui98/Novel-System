"""Unit tests for effective budget resolution."""

from __future__ import annotations

import pytest

from novel_agent.domain.ids import RunId, StableId, TaskId
from novel_agent.domain.model_calls import (
    BudgetResolutionProfile,
    BudgetSource,
    ModelCallPurpose,
    ModelRequest,
    ModelRole,
)
from novel_agent.services.effective_budget import (
    EffectiveBudgetResolver,
    ModelBudgetResolutionError,
    ProviderBudgetLimits,
)


def _request(**updates: object) -> ModelRequest:
    base = {
        "request_id": StableId("req.budget"),
        "run_id": RunId("run.budget"),
        "task_id": TaskId("task.budget"),
        "model_role": ModelRole.IMPLEMENTATION,
        "purpose": ModelCallPurpose.DEVELOPMENT,
        "trace_id": "trace.budget",
        "prompt": "prompt",
    }
    base.update(updates)
    return ModelRequest.model_validate(base)


def test_provider_budget_limits_reject_invalid_sequence() -> None:
    with pytest.raises(ValueError, match="provider sequence limits must be positive"):
        ProviderBudgetLimits(sequence_limit=0)
    with pytest.raises(ValueError, match="provider output limit must be positive"):
        ProviderBudgetLimits(output_limit=0)
    with pytest.raises(ValueError, match="safety allowance cannot be negative"):
        ProviderBudgetLimits(safety_allowance_tokens=-1)
    with pytest.raises(ValueError, match="reasoning reserve cannot be negative"):
        ProviderBudgetLimits(estimated_reasoning_reserve=-1)


def test_resolve_explicit_request_budget() -> None:
    resolver = EffectiveBudgetResolver()
    limits = ProviderBudgetLimits(
        sequence_limit=10_000,
        safety_allowance_tokens=100,
    )
    request = _request(max_output_tokens=500)
    result = resolver.resolve(
        request,
        limits=limits,
        profile=BudgetResolutionProfile.STRICT,
        estimated_input_tokens=1000,
    )
    assert result.budget_source is BudgetSource.EXPLICIT_REQUEST
    assert result.body_output_budget == 500
    assert result.thinking_budget == 0


def test_resolve_invocation_budget() -> None:
    resolver = EffectiveBudgetResolver()
    limits = ProviderBudgetLimits(sequence_limit=10_000)
    request = _request()
    result = resolver.resolve(
        request,
        limits=limits,
        profile=BudgetResolutionProfile.STRICT,
        estimated_input_tokens=500,
        invocation_output_tokens=800,
    )
    assert result.budget_source is BudgetSource.INVOCATION_BUDGET
    assert result.body_output_budget == 800


def test_resolve_endpoint_default() -> None:
    resolver = EffectiveBudgetResolver()
    limits = ProviderBudgetLimits(sequence_limit=10_000, output_limit=600)
    request = _request()
    result = resolver.resolve(
        request,
        limits=limits,
        profile=BudgetResolutionProfile.STRICT,
        estimated_input_tokens=500,
    )
    assert result.budget_source is BudgetSource.ENDPOINT_DEFAULT
    assert result.body_output_budget == 600


def test_resolve_canary_auto_budget() -> None:
    resolver = EffectiveBudgetResolver()
    limits = ProviderBudgetLimits(
        sequence_limit=10_000,
        global_output_cap=4000,
        safety_allowance_tokens=200,
    )
    request = _request()
    result = resolver.resolve(
        request,
        limits=limits,
        profile=BudgetResolutionProfile.CANARY,
        estimated_input_tokens=1000,
    )
    assert result.budget_source is BudgetSource.MODEL_MAX_AUTO
    assert result.body_output_budget >= 1


def test_strict_profile_requires_named_budget() -> None:
    resolver = EffectiveBudgetResolver()
    limits = ProviderBudgetLimits(sequence_limit=10_000)
    request = _request()
    with pytest.raises(ModelBudgetResolutionError, match="strict budget profile"):
        resolver.resolve(
            request,
            limits=limits,
            profile=BudgetResolutionProfile.STRICT,
            estimated_input_tokens=500,
        )


def test_thinking_reserve_explicit_budget() -> None:
    resolver = EffectiveBudgetResolver()
    limits = ProviderBudgetLimits(sequence_limit=10_000)
    request = _request(thinking_token_budget=300, max_output_tokens=400)
    result = resolver.resolve(
        request,
        limits=limits,
        profile=BudgetResolutionProfile.STRICT,
        estimated_input_tokens=500,
    )
    assert result.thinking_budget == 300
    assert result.total_output_budget == 700


def test_thinking_reserve_disabled() -> None:
    resolver = EffectiveBudgetResolver()
    limits = ProviderBudgetLimits(sequence_limit=10_000, estimated_reasoning_reserve=2048)
    request = _request(enable_thinking=False, max_output_tokens=400)
    result = resolver.resolve(
        request,
        limits=limits,
        profile=BudgetResolutionProfile.STRICT,
        estimated_input_tokens=500,
    )
    assert result.thinking_budget == 0
    assert result.total_output_budget == 400


def test_thinking_reserve_enabled_default() -> None:
    resolver = EffectiveBudgetResolver()
    limits = ProviderBudgetLimits(
        sequence_limit=20_000,
        estimated_reasoning_reserve=1024,
        reasoning_included_in_completion_tokens=True,
    )
    request = _request(enable_thinking=True, max_output_tokens=400)
    result = resolver.resolve(
        request,
        limits=limits,
        profile=BudgetResolutionProfile.STRICT,
        estimated_input_tokens=500,
    )
    assert result.thinking_budget == 1024
    assert result.total_output_budget == 1024


def test_thinking_reserve_uses_provider_default_when_request_is_unspecified() -> None:
    resolver = EffectiveBudgetResolver()
    limits = ProviderBudgetLimits(
        sequence_limit=20_000,
        estimated_reasoning_reserve=1024,
        default_thinking=True,
    )
    request = _request(max_output_tokens=400)
    result = resolver.resolve(
        request,
        limits=limits,
        profile=BudgetResolutionProfile.STRICT,
        estimated_input_tokens=500,
    )
    assert result.thinking_budget == 1024
    assert result.total_output_budget == 1_424


def test_negative_estimated_input_rejected() -> None:
    resolver = EffectiveBudgetResolver()
    limits = ProviderBudgetLimits(sequence_limit=10_000)
    request = _request(max_output_tokens=100)
    with pytest.raises(ValueError, match="estimated input tokens cannot be negative"):
        resolver.resolve(
            request,
            limits=limits,
            profile=BudgetResolutionProfile.STRICT,
            estimated_input_tokens=-1,
        )
