"""Unit tests for OpenAICompatibleChatEndpoint with httpx.MockTransport."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic import JsonValue

from novel_agent.adapters.model.openai_chat import (
    OpenAIChatEndpointError,
    OpenAIChatOutputLengthError,
    OpenAICompatibleChatEndpoint,
)
from novel_agent.domain.ids import RunId, StableId, TaskId
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelRequest,
    ModelRole,
)


def _request(
    prompt: str = "test",
    response_schema: dict[str, JsonValue] | None = None,
    max_output_tokens: int | None = None,
    enable_thinking: bool | None = None,
    thinking_token_budget: int | None = None,
) -> ModelRequest:
    return ModelRequest(
        request_id=StableId("request.test"),
        run_id=RunId("run.test"),
        task_id=TaskId("task.test"),
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id="trace-test",
        prompt=prompt,
        response_schema=response_schema,
        max_output_tokens=max_output_tokens,
        timeout_seconds=10,
        enable_thinking=enable_thinking,
        thinking_token_budget=thinking_token_budget,
    )


def _success_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"finish_reason": "stop", "message": {"content": '{"key":"value"}'}}],
            "model": "qwen36-27b",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def _empty_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
        },
    )


def _endpoint(handler: object, model: str = "qwen36-27b") -> OpenAICompatibleChatEndpoint:
    assert callable(handler)
    return OpenAICompatibleChatEndpoint(
        base_url="http://127.0.0.1:8002/v1",
        model=model,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


# --- Construction ---


def test_endpoint_rejects_non_loopback_when_local_only() -> None:
    with pytest.raises(ValueError, match="loopback"):
        OpenAICompatibleChatEndpoint(
            base_url="http://192.168.1.1:8002/v1",
            model="test",
            local_only=True,
        )


def test_endpoint_rejects_relative_url() -> None:
    with pytest.raises(ValueError, match="absolute HTTP"):
        OpenAICompatibleChatEndpoint(
            base_url="relative/path",
            model="test",
        )


def test_endpoint_rejects_empty_model() -> None:
    with pytest.raises(ValueError, match="model must not be empty"):
        OpenAICompatibleChatEndpoint(
            base_url="http://127.0.0.1:8002/v1",
            model="",
        )


def test_endpoint_rejects_zero_max_tokens() -> None:
    with pytest.raises(ValueError, match="positive"):
        OpenAICompatibleChatEndpoint(
            base_url="http://127.0.0.1:8002/v1",
            model="test",
            max_output_tokens=0,
        )


def test_endpoint_rejects_invalid_temperature() -> None:
    with pytest.raises(ValueError, match="temperature"):
        OpenAICompatibleChatEndpoint(
            base_url="http://127.0.0.1:8002/v1",
            model="test",
            temperature=3.0,
        )


def test_endpoint_rejects_invalid_retries() -> None:
    with pytest.raises(ValueError, match="retries"):
        OpenAICompatibleChatEndpoint(
            base_url="http://127.0.0.1:8002/v1",
            model="test",
            max_retries=3,
        )


def test_endpoint_sets_is_external_false_for_loopback() -> None:
    endpoint = OpenAICompatibleChatEndpoint(
        base_url="http://127.0.0.1:8002/v1",
        model="test",
    )
    assert endpoint.is_external is False


def test_endpoint_sets_is_external_true_for_non_local() -> None:
    endpoint = OpenAICompatibleChatEndpoint(
        base_url="http://models.internal:8002/v1",
        model="test",
        local_only=False,
    )
    assert endpoint.is_external is True


# --- Success ---


def test_generate_returns_provider_result() -> None:
    endpoint = _endpoint(_success_handler)
    result = asyncio.run(endpoint.generate(_request()))

    assert result.text == '{"key":"value"}'
    assert result.model_version == "qwen36-27b"
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5
    assert result.usage.cost_usd == 0


def test_generate_records_request() -> None:
    endpoint = _endpoint(_empty_handler)
    asyncio.run(endpoint.generate(_request()))
    assert len(endpoint.requests) == 1
    assert endpoint.requests[0].request_id.root == "request.test"


# --- HTTP failures ---


def test_generate_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "server error"})

    endpoint = _endpoint(handler)
    with pytest.raises(OpenAIChatEndpointError, match="HTTP 500"):
        asyncio.run(endpoint.generate(_request()))


def test_generate_raises_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    endpoint = _endpoint(handler)
    with pytest.raises(OpenAIChatEndpointError, match="after all retries"):
        asyncio.run(endpoint.generate(_request()))


def test_generate_raises_on_non_json_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    endpoint = _endpoint(handler)
    with pytest.raises(OpenAIChatEndpointError, match="not valid JSON"):
        asyncio.run(endpoint.generate(_request()))


# --- Missing fields ---


def test_generate_raises_on_missing_choices() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "test"})

    endpoint = _endpoint(handler)
    with pytest.raises(OpenAIChatEndpointError, match="missing choices"):
        asyncio.run(endpoint.generate(_request()))


def test_generate_raises_on_empty_choices() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    endpoint = _endpoint(handler)
    with pytest.raises(OpenAIChatEndpointError, match="missing choices"):
        asyncio.run(endpoint.generate(_request()))


# --- Content checks ---


def test_generate_raises_on_null_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": None}}],
            },
        )

    endpoint = _endpoint(handler)
    with pytest.raises(OpenAIChatEndpointError, match="null or empty content"):
        asyncio.run(endpoint.generate(_request()))


def test_generate_raises_on_empty_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "   "}}],
            },
        )

    endpoint = _endpoint(handler)
    with pytest.raises(OpenAIChatEndpointError, match="null or empty content"):
        asyncio.run(endpoint.generate(_request()))


def test_generate_raises_on_missing_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop"}],
            },
        )

    endpoint = _endpoint(handler)
    with pytest.raises(OpenAIChatEndpointError, match="null or empty content"):
        asyncio.run(endpoint.generate(_request()))


# --- Finish reason checks ---


def test_generate_raises_on_length_finish_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "length", "message": {"content": "partial"}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 4096,
                    "completion_tokens_details": {"reasoning_tokens": 4000},
                },
            },
        )

    endpoint = _endpoint(handler)
    with pytest.raises(OpenAIChatEndpointError, match="truncated") as raised:
        asyncio.run(endpoint.generate(_request()))
    assert raised.value.finish_reason == "length"
    assert raised.value.input_tokens == 100
    assert raised.value.output_tokens == 4096
    assert raised.value.reasoning_tokens == 4000
    assert raised.value.raw_content == "partial"
    assert raised.value.latency_ms is not None


def test_generate_raises_on_unexpected_finish_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "content_filter",
                        "message": {"content": "blocked"},
                    }
                ],
            },
        )

    endpoint = _endpoint(handler)
    with pytest.raises(OpenAIChatEndpointError, match="unexpected reason"):
        asyncio.run(endpoint.generate(_request()))


# --- Usage handling ---


def test_generate_handles_missing_usage() -> None:
    endpoint = _endpoint(_empty_handler)
    result = asyncio.run(endpoint.generate(_request()))
    assert result.usage.input_tokens == 0
    assert result.usage.output_tokens == 0


def test_generate_handles_missing_model_in_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            },
        )

    endpoint = _endpoint(handler, model="qwen36-27b")
    result = asyncio.run(endpoint.generate(_request()))
    assert result.model_version == "qwen36-27b"


# --- Schema guidance ---


def test_generate_sends_json_object_when_no_schema() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            },
        )

    endpoint = _endpoint(handler)
    asyncio.run(endpoint.generate(_request(response_schema=None)))


def test_generate_uses_request_output_token_override() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["max_tokens"] == 2048
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            },
        )

    endpoint = _endpoint(handler)
    asyncio.run(endpoint.generate(_request(max_output_tokens=2048)))


def test_generate_sends_json_schema_when_schema_provided() -> None:
    schema: dict[str, JsonValue] = {
        "title": "TestOutput",
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        rf = body["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["name"] == "TestOutput"
        assert rf["json_schema"]["strict"] is True
        assert "name" in rf["json_schema"]["schema"]["properties"]
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": '{"name":"x"}'}}],
            },
        )

    endpoint = _endpoint(handler)
    asyncio.run(endpoint.generate(_request(response_schema=schema)))


def test_generate_enforces_enable_thinking_true() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["chat_template_kwargs"] == {"enable_thinking": True}
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            },
        )

    endpoint = _endpoint(handler)
    asyncio.run(endpoint.generate(_request()))


def test_generate_honors_explicit_enable_thinking_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            },
        )

    endpoint = _endpoint(handler)
    asyncio.run(endpoint.generate(_request(enable_thinking=False)))


def test_generate_passes_through_thinking_token_budget() -> None:
    seen_kwargs: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_kwargs.append("thinking_token_budget" in body.get("chat_template_kwargs", {}))
        if seen_kwargs[-1]:
            assert body["chat_template_kwargs"]["thinking_token_budget"] == 3000
        assert "thinking_token_budget" not in body or "chat_template_kwargs" not in body
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            },
        )

    endpoint = _endpoint(handler)
    asyncio.run(endpoint.generate(_request(thinking_token_budget=3000)))
    asyncio.run(endpoint.generate(_request()))
    assert seen_kwargs == [True, False]


# --- Retry ---


def test_generate_retries_on_transport_error() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 2:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            },
        )

    endpoint = OpenAICompatibleChatEndpoint(
        base_url="http://127.0.0.1:8002/v1",
        model="test",
        max_retries=1,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = asyncio.run(endpoint.generate(_request()))
    assert len(calls) == 2
    assert len(endpoint.attempts) == 1
    assert endpoint.attempts[0].attempt == 1
    assert endpoint.attempts[0].error_type == "ConnectError"
    assert result.text == "{}"


def test_generate_does_not_retry_output_length_error() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]},
        )

    endpoint = OpenAICompatibleChatEndpoint(
        base_url="http://127.0.0.1:8002/v1",
        model="test",
        max_retries=1,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(OpenAIChatOutputLengthError) as raised:
        asyncio.run(endpoint.generate(_request()))

    assert raised.value.finish_reason == "length"
    assert raised.value.raw_content == "partial"
    assert len(calls) == 1
    assert endpoint.attempts[0].error_type == "OutputLengthError"


def test_generate_fails_after_all_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    endpoint = OpenAICompatibleChatEndpoint(
        base_url="http://127.0.0.1:8002/v1",
        model="test",
        max_retries=2,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(OpenAIChatEndpointError, match="after all retries"):
        asyncio.run(endpoint.generate(_request()))
    assert len(endpoint.attempts) == 3


def test_generate_retries_on_retryable_http_status() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 2:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            },
        )

    endpoint = OpenAICompatibleChatEndpoint(
        base_url="http://127.0.0.1:8002/v1",
        model="test",
        max_retries=2,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = asyncio.run(endpoint.generate(_request()))
    assert len(calls) == 2
    assert result.text == "{}"


def test_generate_does_not_retry_on_non_retryable_http_status() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(400, json={"error": "bad request"})

    endpoint = OpenAICompatibleChatEndpoint(
        base_url="http://127.0.0.1:8002/v1",
        model="test",
        max_retries=2,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(OpenAIChatEndpointError, match="HTTP 400"):
        asyncio.run(endpoint.generate(_request()))
    assert len(calls) == 1


# --- aclose ---


def test_aclose_closes_client() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(_empty_handler))
    endpoint = OpenAICompatibleChatEndpoint(
        base_url="http://127.0.0.1:8002/v1",
        model="test",
        client=client,
    )
    asyncio.run(endpoint.aclose())
    assert client.is_closed


def test_owned_client_is_request_scoped_across_event_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(_success_handler))

    class RequestScopedClient:
        async def __aenter__(self) -> httpx.AsyncClient:
            return client

        async def __aexit__(self, *args: object) -> None:
            await client.aclose()

    monkeypatch.setattr(
        "novel_agent.adapters.model.openai_chat.httpx.AsyncClient", RequestScopedClient
    )
    endpoint = OpenAICompatibleChatEndpoint(
        base_url="http://127.0.0.1:8002/v1",
        model="test",
    )

    assert endpoint._client is None
    result = asyncio.run(endpoint.generate(_request()))
    assert result.text == '{"key":"value"}'
    assert client.is_closed
    asyncio.run(endpoint.aclose())


# --- Secrets handling ---


def test_generate_does_not_include_api_key_in_error_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("cannot reach 127.0.0.1")

    endpoint = OpenAICompatibleChatEndpoint(
        base_url="http://127.0.0.1:8002/v1",
        model="test",
        api_key="sk-secret-do-not-leak",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(OpenAIChatEndpointError, match="after all retries"):
        asyncio.run(endpoint.generate(_request()))
