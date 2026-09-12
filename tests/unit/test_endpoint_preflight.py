"""Contract tests for the fail-closed registered-endpoint preflight."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from novel_agent.domain.model_calls import (
    ModelRequest,
    ModelRole,
    ModelUsage,
    ProviderModelResult,
)
from novel_agent.runtime import endpoint_preflight
from novel_agent.runtime.endpoint_preflight import (
    preflight_endpoint_profile,
)
from novel_agent.runtime.production_bootstrap import (
    QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE,
)
from novel_agent.services.model_gateway import RegisteredModelEndpoint


class _StubAdapter:
    """Minimal ModelEndpointPort double with a scripted result or failure."""

    is_external = False
    model = "stub-model"
    max_retries = 0
    default_thinking = False

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "http://127.0.0.1:8003/v1",
        text: str = '{"status": "ok", "language": "zh"}',
        error: Exception | None = None,
        model_version: str = "qwen38-27b-nvfp4",
    ) -> None:
        self.model = model
        self.base_url = base_url
        self._text = text
        self._error = error
        self._model_version = model_version
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ProviderModelResult:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        text = self._text
        schema = request.response_schema or {}
        if "paragraph" in (schema.get("properties") or {}):
            # The long-form probe asks for sustained target-language prose; answer it
            # with Chinese text so the probe exercises the language contract rather
            # than reporting a stub shape as a language failure.
            paragraph = "陆沉舟在旧城中寻找失落的信物。" * 30
            text = json.dumps(
                {
                    "status": "ok",
                    "paragraph": paragraph,
                    "character_count": len(paragraph),
                },
                ensure_ascii=False,
            )
        return ProviderModelResult(
            text=text,
            model_version=self._model_version,
            usage=ModelUsage(input_tokens=1, output_tokens=1, cost_usd=Decimal("0")),
        )


def _registration(adapter: _StubAdapter, *, model_name: str) -> RegisteredModelEndpoint:
    return RegisteredModelEndpoint(
        role=ModelRole.IMPLEMENTATION,
        endpoint_name="stub@8003",
        model_name=model_name,
        adapter=adapter,
        revision=model_name,
        output_limit=12_000,
        safety_allowance_tokens=1_000,
        default_thinking=False,
    )


def _patch_registration(
    monkeypatch: pytest.MonkeyPatch,
    adapter: _StubAdapter,
    *,
    model_name: str = "qwen38-27b-nvfp4",
) -> None:
    monkeypatch.setattr(
        endpoint_preflight,
        "_registered_endpoint",
        lambda profile: _registration(adapter, model_name=model_name),
    )


async def _live_models(values: tuple[str, ...]):
    return values


def test_preflight_requires_an_explicit_profile() -> None:
    with pytest.raises(RuntimeError, match="explicit --endpoint-profile"):
        import asyncio

        asyncio.run(preflight_endpoint_profile(None))


def test_preflight_accepts_a_matching_live_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    adapter = _StubAdapter(model="qwen38-27b-nvfp4")
    _patch_registration(monkeypatch, adapter)
    monkeypatch.setattr(
        endpoint_preflight,
        "_fetch_live_models",
        lambda base_url, *, timeout_seconds: _live_models(("qwen38-27b-nvfp4",)),
    )

    result = asyncio.run(preflight_endpoint_profile(QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE))

    assert result.ok is True
    assert result.identity_matches is True
    assert result.live_models == ("qwen38-27b-nvfp4",)
    assert result.generation_ran is False
    assert result.issues == ()
    assert result.as_payload()["declared_model"] == "qwen38-27b-nvfp4"


def test_preflight_rejects_a_live_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    adapter = _StubAdapter(model="qwen38-27b-nvfp4")
    _patch_registration(monkeypatch, adapter)
    monkeypatch.setattr(
        endpoint_preflight,
        "_fetch_live_models",
        lambda base_url, *, timeout_seconds: _live_models(("qwen36-27b-nvfp4",)),
    )

    result = asyncio.run(preflight_endpoint_profile(QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE))

    assert result.ok is False
    assert result.identity_matches is False
    assert any("is not served by" in issue for issue in result.issues)


def test_preflight_reports_an_unavailable_model_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    adapter = _StubAdapter(model="qwen38-27b-nvfp4")
    _patch_registration(monkeypatch, adapter)

    async def _boom(base_url: str, *, timeout_seconds: float) -> tuple[str, ...]:
        raise ConnectionError("connection refused")

    monkeypatch.setattr(endpoint_preflight, "_fetch_live_models", _boom)

    result = asyncio.run(preflight_endpoint_profile(QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE))

    assert result.ok is False
    assert result.live_models == ()
    assert any("model list unavailable" in issue for issue in result.issues)


def test_preflight_rejects_an_adapter_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    adapter = _StubAdapter(model="qwen36-27b-nvfp4")
    _patch_registration(
        monkeypatch,
        adapter,
        model_name="qwen38-27b-nvfp4",
    )
    monkeypatch.setattr(
        endpoint_preflight,
        "_fetch_live_models",
        lambda base_url, *, timeout_seconds: _live_models(("qwen38-27b-nvfp4",)),
    )

    result = asyncio.run(preflight_endpoint_profile(QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE))

    assert result.ok is False
    assert any("adapter model" in issue for issue in result.issues)


def test_optional_live_generation_uses_the_registered_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    adapter = _StubAdapter(model="qwen38-27b-nvfp4")
    _patch_registration(monkeypatch, adapter)
    monkeypatch.setattr(
        endpoint_preflight,
        "_fetch_live_models",
        lambda base_url, *, timeout_seconds: _live_models(("qwen38-27b-nvfp4",)),
    )

    result = asyncio.run(
        preflight_endpoint_profile(QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE, live_generation=True)
    )

    assert result.ok is True
    assert result.generation_ran is True
    # Two probes: the bounded schema check and the target-language long-form check.
    assert len(adapter.requests) == 2
    schema_probe, long_form_probe = adapter.requests
    assert schema_probe.response_schema is not None
    assert schema_probe.response_schema["required"] == ["status", "language"]
    assert long_form_probe.response_schema is not None
    assert long_form_probe.response_schema["required"] == [
        "status",
        "paragraph",
        "character_count",
    ]
    assert long_form_probe.request_id.root.endswith("long-form")


def test_live_generation_failure_is_reported_and_skips_on_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    failing = _StubAdapter(model="qwen38-27b-nvfp4", error=TimeoutError("deadline"))
    _patch_registration(monkeypatch, failing)
    monkeypatch.setattr(
        endpoint_preflight,
        "_fetch_live_models",
        lambda base_url, *, timeout_seconds: _live_models(("qwen38-27b-nvfp4",)),
    )

    result = asyncio.run(
        preflight_endpoint_profile(QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE, live_generation=True)
    )

    assert result.ok is False
    assert result.generation_ran is False
    assert any("bounded generation failed" in issue for issue in result.issues)

    mismatch = _StubAdapter(model="qwen38-27b-nvfp4")
    _patch_registration(monkeypatch, mismatch)
    monkeypatch.setattr(
        endpoint_preflight,
        "_fetch_live_models",
        lambda base_url, *, timeout_seconds: _live_models(("other-model",)),
    )
    skipped = asyncio.run(
        preflight_endpoint_profile(QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE, live_generation=True)
    )

    assert skipped.generation_ran is False
    assert mismatch.requests == []


def test_live_generation_rejects_non_json_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    adapter = _StubAdapter(model="qwen38-27b-nvfp4", text="not json")
    _patch_registration(monkeypatch, adapter)
    monkeypatch.setattr(
        endpoint_preflight,
        "_fetch_live_models",
        lambda base_url, *, timeout_seconds: _live_models(("qwen38-27b-nvfp4",)),
    )

    result = asyncio.run(
        preflight_endpoint_profile(QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE, live_generation=True)
    )

    assert result.ok is False
    assert any("non-JSON" in issue for issue in result.issues)


def test_live_generation_rejects_a_missing_status_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    adapter = _StubAdapter(model="qwen38-27b-nvfp4", text=json.dumps({"language": "zh"}))
    _patch_registration(monkeypatch, adapter)
    monkeypatch.setattr(
        endpoint_preflight,
        "_fetch_live_models",
        lambda base_url, *, timeout_seconds: _live_models(("qwen38-27b-nvfp4",)),
    )

    result = asyncio.run(
        preflight_endpoint_profile(QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE, live_generation=True)
    )

    assert result.ok is False
    assert any("required status field" in issue for issue in result.issues)


class _StubRetrievalClient:
    """Stub httpx client for the retrieval preflight probes."""

    def __init__(self, *, embedding_status: int = 200, reranker_status: int = 200) -> None:
        self._embedding_status = embedding_status
        self._reranker_status = reranker_status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, url: str, json: dict[str, object]) -> object:
        assert json.get("model"), "the probe must name the locked model identity"

        class _Response:
            def __init__(self, status: int, payload: dict[str, object]) -> None:
                self.status_code = status
                self._payload = payload
                self.text = "stub"

            def json(self) -> dict[str, object]:
                return self._payload

        if "embedding" in url:
            return _Response(
                self._embedding_status,
                {
                    "model": "BAAI/bge-m3",
                    "data": [{"embedding": [0.1] * 1024}],
                },
            )
        return _Response(
            self._reranker_status,
            {
                "id": "BAAI/bge-reranker-v2-m3@953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 1, "relevance_score": 0.1},
                ],
            },
        )


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: object) -> None:
    monkeypatch.setattr(endpoint_preflight.httpx, "AsyncClient", lambda *args, **kwargs: client)


def test_retrieval_preflight_reports_identity_and_discrimination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    _patch_client(monkeypatch, _StubRetrievalClient())

    result = asyncio.run(
        endpoint_preflight.preflight_retrieval_services(
            embedding_url="http://127.0.0.1:8081/v1/embeddings",
            reranker_url="http://127.0.0.1:8082/rerank",
        )
    )

    assert result.ok is True
    assert result.embedding_model == "BAAI/bge-m3"
    assert result.embedding_dimensions == 1024
    assert result.discriminative is True
    assert result.relevant_score == 0.9
    assert "bge-reranker" in str(result.reranker_model)


def test_retrieval_preflight_rejects_a_non_discriminating_reranker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    class _FlatClient(_StubRetrievalClient):
        async def post(self, url: str, json: dict[str, object]) -> object:
            response = await super().post(url, json)
            if "embedding" not in url:
                response._payload["results"] = [
                    {"index": 0, "relevance_score": 0.5},
                    {"index": 1, "relevance_score": 0.5},
                ]
            return response

    _patch_client(monkeypatch, _FlatClient())

    result = asyncio.run(
        endpoint_preflight.preflight_retrieval_services(
            embedding_url="http://127.0.0.1:8081/v1/embeddings",
            reranker_url="http://127.0.0.1:8082/rerank",
        )
    )

    assert result.ok is False
    assert result.discriminative is False
    assert any("does not rank the relevant document" in issue for issue in result.issues)


def test_retrieval_preflight_reports_a_failing_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    _patch_client(monkeypatch, _StubRetrievalClient(embedding_status=409))

    result = asyncio.run(
        endpoint_preflight.preflight_retrieval_services(
            embedding_url="http://127.0.0.1:8081/v1/embeddings",
            reranker_url="http://127.0.0.1:8082/rerank",
        )
    )

    assert result.ok is False
    assert any("embedding service returned 409" in issue for issue in result.issues)
