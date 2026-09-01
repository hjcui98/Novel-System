"""Resolve one effective output budget for API payload, admission, and ledger."""

from __future__ import annotations

from dataclasses import dataclass

from novel_agent.domain.model_calls import (
    BudgetResolutionProfile,
    BudgetSource,
    EffectiveBudgetResult,
    ModelRequest,
)

DEFAULT_SEQUENCE_LIMIT = 131_072
DEFAULT_REASONING_RESERVE = 2_048


class ModelBudgetResolutionError(ValueError):
    """No named budget source exists for a production-strict request."""


@dataclass(frozen=True, slots=True)
class ProviderBudgetLimits:
    sequence_limit: int = DEFAULT_SEQUENCE_LIMIT
    output_limit: int | None = None
    safety_allowance_tokens: int | None = None
    estimated_reasoning_reserve: int = DEFAULT_REASONING_RESERVE
    default_thinking: bool = False
    reasoning_included_in_completion_tokens: bool = False
    global_output_cap: int = DEFAULT_SEQUENCE_LIMIT

    def __post_init__(self) -> None:
        if self.sequence_limit < 1 or self.global_output_cap < 1:
            raise ValueError("provider sequence limits must be positive")
        if self.output_limit is not None and self.output_limit < 1:
            raise ValueError("provider output limit must be positive")
        if self.safety_allowance_tokens is not None and self.safety_allowance_tokens < 0:
            raise ValueError("safety allowance cannot be negative")
        if self.estimated_reasoning_reserve < 0:
            raise ValueError("reasoning reserve cannot be negative")


class EffectiveBudgetResolver:
    """Parse one request's output reserve. Retrieval/tool/wall-clock stay elsewhere."""

    def resolve(
        self,
        request: ModelRequest,
        *,
        limits: ProviderBudgetLimits,
        profile: BudgetResolutionProfile,
        estimated_input_tokens: int,
        invocation_output_tokens: int | None = None,
    ) -> EffectiveBudgetResult:
        if estimated_input_tokens < 0:
            raise ValueError("estimated input tokens cannot be negative")
        thinking = self._thinking_reserve(request, limits)
        source, body, safety = self._body_and_safety(
            request,
            limits=limits,
            profile=profile,
            estimated_input_tokens=estimated_input_tokens,
            invocation_output_tokens=invocation_output_tokens,
            thinking=thinking,
        )
        total = self._total_output(body, thinking, limits)
        if source is not BudgetSource.MODEL_MAX_AUTO:
            safety = (
                limits.safety_allowance_tokens
                if limits.safety_allowance_tokens is not None
                else max(256, (estimated_input_tokens + total) // 20)
            )
        reserved = estimated_input_tokens + total + safety
        available = max(0, limits.sequence_limit - total - safety)
        return EffectiveBudgetResult(
            budget_source=source,
            context_limit=limits.sequence_limit,
            estimated_input_tokens=estimated_input_tokens,
            body_output_budget=body,
            thinking_budget=thinking,
            total_output_budget=total,
            safety_allowance_tokens=safety,
            reserved_sequence_tokens=reserved,
            available_input_tokens=available,
        )

    def _body_and_safety(
        self,
        request: ModelRequest,
        *,
        limits: ProviderBudgetLimits,
        profile: BudgetResolutionProfile,
        estimated_input_tokens: int,
        invocation_output_tokens: int | None,
        thinking: int,
    ) -> tuple[BudgetSource, int, int]:
        auto_safety = (
            limits.safety_allowance_tokens if limits.safety_allowance_tokens is not None else 256
        )
        if request.max_output_tokens is not None:
            return BudgetSource.EXPLICIT_REQUEST, request.max_output_tokens, auto_safety
        if invocation_output_tokens is not None:
            return BudgetSource.INVOCATION_BUDGET, invocation_output_tokens, auto_safety
        if limits.output_limit is not None:
            return BudgetSource.ENDPOINT_DEFAULT, limits.output_limit, auto_safety
        if profile is BudgetResolutionProfile.CANARY:
            cap = limits.output_limit or limits.global_output_cap
            room = limits.sequence_limit - estimated_input_tokens - auto_safety - thinking
            return (
                BudgetSource.MODEL_MAX_AUTO,
                max(1, min(room, cap, limits.global_output_cap)),
                auto_safety,
            )
        raise ModelBudgetResolutionError(
            "strict budget profile requires an explicit, invocation, or registered default"
        )

    @staticmethod
    def _thinking_reserve(request: ModelRequest, limits: ProviderBudgetLimits) -> int:
        if request.enable_thinking is False:
            return 0
        if request.thinking_token_budget is not None:
            return request.thinking_token_budget
        if request.enable_thinking is True or limits.default_thinking:
            return limits.estimated_reasoning_reserve
        return 0

    @staticmethod
    def _total_output(body: int, thinking: int, limits: ProviderBudgetLimits) -> int:
        if thinking == 0 or limits.reasoning_included_in_completion_tokens:
            return max(body, thinking) if thinking else body
        return body + thinking
