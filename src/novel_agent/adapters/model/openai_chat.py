"""OpenAI-compatible chat-completions adapter with JSON Schema guidance."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import httpx

from novel_agent.domain.model_calls import (
    ModelRequest,
    ModelUsage,
    ProviderModelResult,
)


class OpenAIChatEndpointError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _AttemptRecord:
    attempt: int
    error_type: str
    error_detail: str


RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})


class OpenAICompatibleChatEndpoint:
    """Call one version-pinned chat model and require structured JSON output."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        max_output_tokens: int = 8192,
        temperature: float = 0.0,
        api_key: str | None = None,
        local_only: bool = True,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 0,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("chat endpoint base URL must be absolute HTTP(S)")
        if local_only and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("local-only chat endpoint must resolve to loopback")
        if not model:
            raise ValueError("chat endpoint model must not be empty")
        if max_output_tokens < 1:
            raise ValueError("chat endpoint max output tokens must be positive")
        if not 0 <= temperature <= 2:
            raise ValueError("chat endpoint temperature must be between zero and two")
        if max_retries < 0 or max_retries > 2:
            raise ValueError("chat endpoint retries must be between zero and two")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.model_version = model
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self.is_external = not local_only
        self.max_retries = max_retries
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        # The Stage 2 replay currently invokes agents through separate
        # ``asyncio.run`` calls.  A pooled AsyncClient created here would keep
        # sockets bound to the first (subsequently closed) event loop.  Use a
        # request-scoped client unless a caller explicitly injects one (tests
        # and applications that own a single long-lived loop do that).
        self._client = client
        self.requests: list[ModelRequest] = []
        self.attempts: list[_AttemptRecord] = []

    async def generate(self, request: ModelRequest) -> ProviderModelResult:
        self.requests.append(request)
        if self._client is not None:
            return await self._generate_with_client(request, self._client)
        async with httpx.AsyncClient() as client:
            return await self._generate_with_client(request, client)

    async def _generate_with_client(
        self,
        request: ModelRequest,
        client: httpx.AsyncClient,
    ) -> ProviderModelResult:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return await self._generate_once(request, client)
            except OpenAIChatEndpointError as error:
                if (
                    "truncated by output length limit" not in str(error)
                    or attempt >= self.max_retries
                ):
                    raise
                self.attempts.append(
                    _AttemptRecord(
                        attempt=attempt + 1,
                        error_type="OutputLengthError",
                        error_detail=str(error),
                    )
                )
                last_error = error
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
                self.attempts.append(
                    _AttemptRecord(
                        attempt=attempt + 1,
                        error_type=type(error).__name__,
                        error_detail=f"{error}",
                    )
                )
                last_error = error
        raise OpenAIChatEndpointError(
            "chat completion request failed after all retries"
        ) from last_error

    async def _generate_once(
        self,
        request: ModelRequest,
        client: httpx.AsyncClient,
    ) -> ProviderModelResult:
        payload = self._build_payload(request)
        try:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers,
                json=payload,
                timeout=request.timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            if error.response.status_code in RETRYABLE_STATUS_CODES:
                raise
            raise OpenAIChatEndpointError(
                f"chat completion HTTP {error.response.status_code}"
            ) from error

        try:
            payload_dict = response.json()
        except json.JSONDecodeError as error:
            raise OpenAIChatEndpointError("chat completion response is not valid JSON") from error

        try:
            choice = payload_dict["choices"][0]
        except (KeyError, IndexError, TypeError) as error:
            raise OpenAIChatEndpointError("chat completion response is missing choices") from error

        finish_reason = choice.get("finish_reason")
        message = choice.get("message") or {}
        content = message.get("content")

        if finish_reason == "length":
            raise OpenAIChatEndpointError("chat completion was truncated by output length limit")
        if finish_reason != "stop":
            raise OpenAIChatEndpointError(
                f"chat completion finished with unexpected reason: {finish_reason}"
            )
        if not isinstance(content, str) or not content.strip():
            raise OpenAIChatEndpointError("chat completion returned null or empty content")

        usage = payload_dict.get("usage") or {}
        return ProviderModelResult(
            text=content,
            model_version=str(payload_dict.get("model") or self.model_version),
            usage=ModelUsage(
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
                cost_usd=Decimal("0"),
            ),
        )

    def _build_payload(self, request: ModelRequest) -> dict[str, Any]:
        schema = request.response_schema
        response_format: dict[str, object]
        if schema is None:
            response_format = {"type": "json_object"}
        else:
            title = str(schema.get("title", "structured_output"))
            name = re.sub(r"[^A-Za-z0-9_-]", "_", title)[:64] or "structured_output"
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": name,
                    "schema": schema,
                    "strict": True,
                },
            }
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": request.prompt}],
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
            "response_format": response_format,
            "chat_template_kwargs": {"enable_thinking": False},
        }

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
