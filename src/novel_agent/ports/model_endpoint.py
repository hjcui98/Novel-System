"""Provider-neutral model endpoint boundary."""

from typing import Protocol

from novel_agent.domain.model_calls import ModelRequest, ProviderModelResult


class ModelEndpointError(RuntimeError):
    """Provider-neutral exhausted model transport or response failure."""


class ModelEndpointPort(Protocol):
    is_external: bool

    async def generate(self, request: ModelRequest) -> ProviderModelResult: ...
