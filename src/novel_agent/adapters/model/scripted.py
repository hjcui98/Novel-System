"""Request-aware deterministic structured model endpoint for contract smoke runs."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from novel_agent.domain.model_calls import (
    ModelRequest,
    ModelUsage,
    ProviderModelResult,
)


class ScriptedModelEndpoint:
    """Return caller-supplied JSON while preserving the normal audited model boundary."""

    is_external = False

    def __init__(
        self,
        responder: Callable[[ModelRequest], str],
        *,
        model_version: str = "scripted-contract-smoke-v1",
    ) -> None:
        self._responder = responder
        self.model_version = model_version
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ProviderModelResult:
        self.requests.append(request)
        return ProviderModelResult(
            text=self._responder(request),
            model_version=self.model_version,
            usage=ModelUsage(
                input_tokens=0,
                output_tokens=0,
                cost_usd=Decimal("0"),
            ),
        )
