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
        return ProviderModelResult(
            text=self._text,
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
    assert len(adapter.requests) == 1
    request = adapter.requests[0]
    assert request.response_schema is not None
    assert request.response_schema["required"] == ["status", "language"]


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
