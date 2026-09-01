"""Deterministic fake model endpoint used by tests and CI."""

from decimal import Decimal

from novel_agent.domain.model_calls import ModelRequest, ModelUsage, ProviderModelResult


class FakeModelEndpoint:
    is_external = False
    default_thinking = False

    def __init__(
        self,
        response_text: str,
        *,
        model_version: str = "fake-v1",
        error: Exception | None = None,
    ) -> None:
        self.response_text = response_text
        self.model_version = model_version
        self.error = error
        self.requests: list[ModelRequest] = []
        self.max_output_tokens: int | None = None

    async def generate(self, request: ModelRequest) -> ProviderModelResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ProviderModelResult(
            text=self.response_text,
            model_version=self.model_version,
            usage=ModelUsage(input_tokens=0, output_tokens=0, cost_usd=Decimal("0")),
        )
