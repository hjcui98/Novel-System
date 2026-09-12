"""Fail-closed endpoint preflight for one registered production profile.

The registered profile declares which model identity production expects.  A live
service can silently serve a different checkpoint (for example the 8003 service
serving a Qwen 3.8 NVFP4 weight while a legacy profile still declares Qwen 3.6).
This module compares the declared identity against the live ``/v1/models``
advertisement before any real run, and can additionally exercise the registered
adapter with a bounded schema-constrained generation.

It never rewrites a profile or falls back to another endpoint: a mismatch is
reported as a failed preflight.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import httpx

from novel_agent.domain.ids import RunId, StableId, TaskId
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelRequest,
    ModelRole,
)
from novel_agent.runtime.production_bootstrap import resolve_registered_model_endpoints
from novel_agent.services.model_gateway import RegisteredModelEndpoint

_PREFLIGHT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ok"]},
        "language": {"type": "string"},
    },
    "required": ["status", "language"],
    "additionalProperties": False,
}
_PREFLIGHT_PROMPT = (
    "Return only a JSON object with exactly two keys: "
    '"status" set to "ok" and "language" set to "zh".'
)


@dataclass(frozen=True, slots=True)
class EndpointPreflightResult:
    """Evidence for one endpoint-profile preflight."""

    endpoint_profile: str
    endpoint_name: str
    base_url: str
    declared_model: str
    adapter_model: str
    live_models: tuple[str, ...]
    identity_matches: bool
    generation_ran: bool
    issues: tuple[str, ...]

    def as_payload(self) -> dict[str, Any]:
        return {
            "endpoint_profile": self.endpoint_profile,
            "endpoint_name": self.endpoint_name,
            "base_url": self.base_url,
            "declared_model": self.declared_model,
            "adapter_model": self.adapter_model,
            "live_models": list(self.live_models),
            "identity_matches": self.identity_matches,
            "generation_ran": self.generation_ran,
            "issues": list(self.issues),
        }

    @property
    def ok(self) -> bool:
        return self.identity_matches and not self.issues


def _registered_endpoint(profile: str) -> RegisteredModelEndpoint:
    endpoints = resolve_registered_model_endpoints(profile)
    if len(endpoints) != 1:
        raise RuntimeError(f"endpoint profile {profile!r} did not register exactly one endpoint")
    return endpoints[0]


def _adapter_base_url(endpoint: RegisteredModelEndpoint) -> str:
    base_url = getattr(endpoint.adapter, "base_url", None)
    if not isinstance(base_url, str) or not base_url:
        raise RuntimeError(f"endpoint {endpoint.endpoint_name!r} has no explicit base URL")
    return base_url


async def _fetch_live_models(base_url: str, *, timeout_seconds: float) -> tuple[str, ...]:
    url = f"{base_url.rstrip('/')}/models"
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise RuntimeError(f"{url} did not return an OpenAI-compatible model list")
    models: list[str] = []
    for entry in data:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            models.append(entry["id"])
    return tuple(models)


async def _run_bounded_generation(
    endpoint: RegisteredModelEndpoint, *, timeout_seconds: float
) -> tuple[bool, tuple[str, ...]]:
    request = ModelRequest(
        request_id=StableId("preflight.endpoint"),
        run_id=RunId("run.preflight"),
        task_id=TaskId("task.preflight"),
        model_role=ModelRole.IMPLEMENTATION,
        purpose=ModelCallPurpose.DEVELOPMENT,
        trace_id="trace.preflight",
        prompt=_PREFLIGHT_PROMPT,
        response_schema=_PREFLIGHT_SCHEMA,
        max_output_tokens=256,
        timeout_seconds=timeout_seconds,
        enable_thinking=False,
    )
    try:
        result = await endpoint.adapter.generate(request)
    except Exception as error:
        return False, (f"bounded generation failed: {type(error).__name__}: {error}",)
    try:
        payload = json.loads(result.text)
    except json.JSONDecodeError as error:
        return False, (f"bounded generation returned non-JSON output: {error}",)
    issues: list[str] = []
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        issues.append("bounded generation did not return the required status field")
    if not result.model_version:
        issues.append("bounded generation returned no model version")
    return True, tuple(issues)


async def preflight_endpoint_profile(
    profile: str | None,
    *,
    live_generation: bool = False,
    list_timeout_seconds: float = 30.0,
    generation_timeout_seconds: float = 120.0,
) -> EndpointPreflightResult:
    """Preflight one registered profile and fail closed on identity drift."""

    if not profile:
        raise RuntimeError("endpoint preflight requires an explicit --endpoint-profile")
    endpoint = _registered_endpoint(profile)
    base_url = _adapter_base_url(endpoint)
    adapter_model = str(getattr(endpoint.adapter, "model", endpoint.model_name))
    issues: list[str] = []
    try:
        live_models = await _fetch_live_models(base_url, timeout_seconds=list_timeout_seconds)
    except Exception as error:
        live_models = ()
        issues.append(f"model list unavailable: {type(error).__name__}: {error}")
    identity_matches = endpoint.model_name in live_models
    if live_models and not identity_matches:
        issues.append(
            "declared model "
            f"{endpoint.model_name!r} is not served by {base_url}; live models: {list(live_models)}"
        )
    if adapter_model != endpoint.model_name:
        issues.append(
            f"adapter model {adapter_model!r} does not match declared model {endpoint.model_name!r}"
        )
    generation_ran = False
    if live_generation and identity_matches:
        generation_ran, generation_issues = await _run_bounded_generation(
            endpoint, timeout_seconds=generation_timeout_seconds
        )
        issues.extend(generation_issues)
    return EndpointPreflightResult(
        endpoint_profile=profile,
        endpoint_name=endpoint.endpoint_name,
        base_url=base_url,
        declared_model=endpoint.model_name,
        adapter_model=adapter_model,
        live_models=live_models,
        identity_matches=identity_matches,
        generation_ran=generation_ran,
        issues=tuple(issues),
    )


def run_endpoint_preflight(
    profile: str | None,
    *,
    live_generation: bool = False,
    list_timeout_seconds: float = 30.0,
    generation_timeout_seconds: float = 120.0,
) -> EndpointPreflightResult:
    """Synchronous entry point used by the CLI and operational scripts."""

    return asyncio.run(
        preflight_endpoint_profile(
            profile,
            live_generation=live_generation,
            list_timeout_seconds=list_timeout_seconds,
            generation_timeout_seconds=generation_timeout_seconds,
        )
    )
