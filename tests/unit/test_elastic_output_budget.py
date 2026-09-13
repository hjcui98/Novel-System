"""Truncated structured calls grow, retain evidence and resume without resending."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.model.openai_chat import OpenAICompatibleChatEndpoint
from novel_agent.domain.ids import RunId, StableId, TaskId
from novel_agent.domain.model_calls import (
    ModelCallLedgerStatus,
    ModelCallPurpose,
    ModelRequest,
    ModelRole,
    RawModelResponseArtifact,
)
from novel_agent.runtime.production_bootstrap import (
    _default_writing_policy,
    _validate_endpoint_contracts,
    load_production_assembly_spec,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.model_gateway import (
    ModelCallCumulativeBudgetExceeded,
    ModelCallUncertainError,
    ModelGateway,
    ModelOutputBudgetExhausted,
    RegisteredModelEndpoint,
)
from novel_agent.services.model_request_admission import ModelRequestAdmissionController


class Output(BaseModel):
    model_config = ConfigDict(strict=True)
    answer: str


def request() -> ModelRequest:
    return ModelRequest(
        request_id=StableId("model.elastic.request"),
        run_id=RunId("run.elastic"),
        task_id=TaskId("task.elastic"),
        model_role=ModelRole.IMPLEMENTATION,
        purpose=ModelCallPurpose.DEVELOPMENT,
        trace_id="trace.elastic",
        prompt="Return the complete planning grid.",
        max_output_tokens=4_000,
        enable_thinking=False,
    )


def gateway(
    tmp_path: Path,
    *,
    required: int = 20_000,
    cap: int = 131_072,
    sequence: int = 131_072,
    schema_failure: bool = False,
) -> tuple[ModelGateway, list[int], RegisteredModelEndpoint]:
    sizes: list[int] = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        payload = json.loads(incoming.content)
        size = payload["max_tokens"]
        sizes.append(size)
        incomplete = size < required
        content = '{"answer":' if incomplete else '{"answer":"ok"}'
        if schema_failure and not incomplete and len(sizes) == 2:
            content = '{"answer":12}'
        return httpx.Response(
            200,
            json={
                "model": "elastic-test",
                "choices": [
                    {
                        "finish_reason": "length" if incomplete else "stop",
                        "message": {"content": content},
                    }
                ],
                "usage": {"prompt_tokens": 101, "completion_tokens": size if incomplete else 10},
            },
        )

    adapter = OpenAICompatibleChatEndpoint(
        base_url="http://127.0.0.1:8003/v1",
        model="elastic-test",
        local_only=True,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    endpoint = RegisteredModelEndpoint(
        role=ModelRole.IMPLEMENTATION,
        endpoint_name="elastic-8003-test",
        model_name="elastic-test",
        adapter=adapter,
        sequence_limit=sequence,
        output_limit=4_000,
        global_output_cap=cap,
        safety_allowance_tokens=1_000,
    )
    instance = ModelGateway(
        (endpoint,),
        raw_artifacts=ArtifactRepository(FilesystemObjectStore(tmp_path / "objects")),
        admission_controller=ModelRequestAdmissionController(
            endpoint_request_limit=1, model_sequence_limit=sequence
        ),
        output_budget_growth_factor=2.0,
        structured_max_retries=1,
    )
    return instance, sizes, endpoint


def test_output_grows_past_the_initial_and_endpoint_defaults_with_exact_evidence(
    tmp_path: Path,
) -> None:
    instance, sizes, _ = gateway(tmp_path)
    output, record = asyncio.run(instance.generate_structured(request(), Output))

    assert output.answer == "ok"
    assert sizes == [4_000, 8_000, 16_000, 32_000]
    calls = instance.model_calls_for(record)
    assert len(calls) == 4
    assert sum(call.usage.output_tokens for call in calls) == 28_010
    assert instance.raw_artifacts is not None
    for index, call in enumerate(calls):
        entry = instance.call_ledger.load(call.request_id)
        assert entry is not None and entry.effective_budget is not None
        assert entry.effective_budget.total_output_budget == sizes[index]
        assert entry.status is (
            ModelCallLedgerStatus.OUTPUT_INCOMPLETE
            if index < 3
            else ModelCallLedgerStatus.COMPLETED
        )
        assert entry.raw_artifact_ref is not None
        raw = RawModelResponseArtifact.model_validate_json(
            instance.raw_artifacts.read_verified(entry.raw_artifact_ref)
        )
        assert raw.finish_reason == ("length" if index < 3 else None)
    assert instance.admission_controller is not None
    assert instance.admission_controller.snapshot()["acquired_requests"] == 4
    assert instance.admission_controller.snapshot()["released_requests"] == 4


def test_restart_replays_all_settled_attempts_without_another_provider_call(tmp_path: Path) -> None:
    first, sizes, endpoint = gateway(tmp_path)
    output, record = asyncio.run(first.generate_structured(request(), Output))
    restarted = ModelGateway(
        (endpoint,),
        call_ledger=first.call_ledger,
        raw_artifacts=first.raw_artifacts,
        output_budget_growth_factor=2.0,
    )
    assert asyncio.run(restarted.generate_structured(request(), Output)) == (output, record)
    assert sizes == [4_000, 8_000, 16_000, 32_000]
    assert len(restarted.model_calls_for(record)) == 4


def test_restart_after_retaining_a_truncation_resumes_with_a_larger_budget(tmp_path: Path) -> None:
    first, sizes, endpoint = gateway(tmp_path, required=8_000)
    with (
        patch.object(
            first, "_structured_retry_request", side_effect=RuntimeError("worker stopped")
        ),
        pytest.raises(RuntimeError, match="worker stopped"),
    ):
        asyncio.run(first.generate_structured(request(), Output))
    restarted = ModelGateway(
        (endpoint,),
        call_ledger=first.call_ledger,
        raw_artifacts=first.raw_artifacts,
        output_budget_growth_factor=2.0,
    )
    assert asyncio.run(restarted.generate_structured(request(), Output))[0].answer == "ok"
    assert sizes == [4_000, 8_000]


@pytest.mark.parametrize(
    ("cap", "sequence", "last"), [(10_000, 131_072, 10_000), (131_072, 11_011, 9_910)]
)
def test_growth_stops_at_provider_or_remaining_context_capacity(
    tmp_path: Path,
    cap: int,
    sequence: int,
    last: int,
) -> None:
    instance, sizes, _ = gateway(tmp_path, cap=cap, sequence=sequence)
    with pytest.raises(ModelOutputBudgetExhausted, match="MODEL_OUTPUT_BUDGET_EXHAUSTED"):
        asyncio.run(instance.generate_structured(request(), Output))
    assert sizes == [4_000, 8_000, last]


def test_schema_repair_keeps_expanded_budget_and_separate_identity(tmp_path: Path) -> None:
    instance, sizes, _ = gateway(tmp_path, required=8_000, schema_failure=True)
    output, record = asyncio.run(instance.generate_structured(request(), Output))
    assert output.answer == "ok"
    assert sizes == [4_000, 8_000, 8_000]
    assert record.request_id.root.endswith(".schema-retry1.output-retry1")
    assert len(instance.model_calls_for(record)) == 3


@pytest.mark.parametrize("tiers", [(7_000,), (7_000, 20_000)])
def test_growth_honors_the_callers_cumulative_budget_and_charges_partial_output(
    tmp_path: Path,
    tiers: tuple[int, ...],
) -> None:
    instance, sizes, _ = gateway(tmp_path, required=8_000)
    budget, _ = instance.preflight_elastic_cumulative_token_budget(
        request(), token_budgets=tiers, tokens_used=1_000
    )
    bound = request().model_copy(
        update={
            "max_output_tokens": budget.total_output_budget,
            "budget_source": budget.budget_source,
        }
    )
    if len(tiers) == 1:
        with pytest.raises(ModelCallCumulativeBudgetExceeded):
            asyncio.run(instance.generate_structured(bound, Output))
        assert sizes == [4_000]
    else:
        _, record = asyncio.run(instance.generate_structured(bound, Output))
        assert sizes == [4_000, 8_000]
        entry = instance.call_ledger.load(record.request_id)
        assert entry is not None and entry.effective_budget is not None
        assert entry.effective_budget.caller_token_budget == 20_000
        assert entry.effective_budget.caller_budget_tier == 1


def test_a_smaller_endpoint_default_does_not_reject_a_legal_planner_allowance(
    tmp_path: Path,
) -> None:
    _, _, endpoint = gateway(tmp_path)
    spec = load_production_assembly_spec()
    assert endpoint.output_limit == 4_000
    assert spec.model_policy.default_output_limit == 16_000
    _validate_endpoint_contracts((endpoint,), spec)
    with pytest.raises(RuntimeError, match="output capacity"):
        _validate_endpoint_contracts((replace(endpoint, global_output_cap=12_000),), spec)


def test_writer_reservation_tracks_the_configured_output_allowance() -> None:
    spec = load_production_assembly_spec()
    spec = spec.model_copy(
        update={
            "model_policy": spec.model_policy.model_copy(update={"default_output_limit": 24_000})
        }
    )
    policy = _default_writing_policy(spec)
    assert policy.budgets.reserved_output_tokens == 24_000
    assert policy.budgets.context_soft_limit_tokens == 131_072 - 24_000 - 1_000


def test_output_growth_also_expands_timeout_within_the_frozen_policy(tmp_path: Path) -> None:
    seed, _, endpoint = gateway(tmp_path)
    instance = ModelGateway(
        (endpoint,),
        raw_artifacts=seed.raw_artifacts,
        output_budget_growth_factor=2.0,
        output_budget_timeout_limit_seconds=180.0,
    )
    asyncio.run(instance.generate_structured(request(), Output))
    assert isinstance(endpoint.adapter, OpenAICompatibleChatEndpoint)
    assert [r.timeout_seconds for r in endpoint.adapter.requests] == [30.0, 60.0, 120.0, 180.0]


def test_growth_keeps_reasoning_reserve_inside_the_provider_output_capacity(tmp_path: Path) -> None:
    seed, sizes, endpoint = gateway(tmp_path, cap=12_000)
    instance = ModelGateway(
        (replace(endpoint, estimated_reasoning_reserve=2_000),),
        raw_artifacts=seed.raw_artifacts,
        output_budget_growth_factor=2.0,
    )
    thinking_request = request().model_copy(update={"enable_thinking": True})
    with pytest.raises(ModelOutputBudgetExhausted):
        asyncio.run(instance.generate_structured(thinking_request, Output))
    assert sizes == [6_000, 10_000, 12_000]
    entries = instance.call_ledger.list_for_prefix(request().request_id.root)
    assert sorted(
        entry.effective_budget.body_output_budget
        for entry in entries
        if entry.effective_budget is not None
    ) == [4_000, 8_000, 10_000]


def test_uncertain_timeout_does_not_trigger_an_output_budget_retry(tmp_path: Path) -> None:
    instance, sizes, endpoint = gateway(tmp_path)
    provider_call = AsyncMock(side_effect=TimeoutError("completion is unknown"))
    with patch.object(endpoint.adapter, "generate", provider_call):
        with pytest.raises(TimeoutError):
            asyncio.run(instance.generate_structured(request(), Output))
        with pytest.raises(ModelCallUncertainError):
            asyncio.run(instance.generate_structured(request(), Output))
    assert provider_call.await_count == 1
    assert sizes == []
    entries = instance.call_ledger.list_for_prefix(request().request_id.root)
    assert len(entries) == 1
    assert entries[0].status is ModelCallLedgerStatus.UNCERTAIN


def test_growth_policy_is_part_of_the_frozen_configuration_fingerprint(tmp_path: Path) -> None:
    """Changing growth or its timeout must invalidate the frozen run identity."""

    from novel_agent.adapters.model.fake import FakeModelEndpoint
    from novel_agent.domain.ids import ArtifactId
    from novel_agent.runtime.production_bootstrap import (
        production_configuration_fingerprint,
        production_contract_pins,
    )

    spec = load_production_assembly_spec()
    prompts, skills = production_contract_pins(schema_version=spec.spec_version)
    endpoint = RegisteredModelEndpoint(
        role=ModelRole.IMPLEMENTATION,
        endpoint_name="fingerprint-8003",
        model_name="fingerprint-model",
        adapter=FakeModelEndpoint('{"answer":"ok"}'),
        sequence_limit=131_072,
        output_limit=16_000,
        global_output_cap=131_072,
    )

    def fingerprint(candidate: object) -> object:
        return production_configuration_fingerprint(
            spec=candidate,
            migration_head="head",
            prompt_pins=prompts,
            skill_pins=skills,
            endpoints=(endpoint,),
            retrieval_backend_profile="real_hybrid",
            reranker_declared=True,
            reranker_resolved=True,
            profile_root_hash=None,
            settlement_policy_fingerprint=ArtifactId("sha256:" + "a" * 64),
        )

    base = fingerprint(spec)
    assert fingerprint(spec) == base
    for field in ("output_budget_growth_factor", "output_budget_timeout_limit_seconds"):
        changed = spec.model_copy(
            update={"model_policy": spec.model_policy.model_copy(update={field: None})}
        )
        assert fingerprint(changed) != base, field
