"""Provider-neutral model endpoint boundary."""

from typing import Protocol

from novel_agent.domain.model_calls import ModelRequest, ProviderModelResult


class ModelEndpointPort(Protocol):
    is_external: bool

    async def generate(self, request: ModelRequest) -> ProviderModelResult: ...
