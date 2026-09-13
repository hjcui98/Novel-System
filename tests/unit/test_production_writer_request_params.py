from __future__ import annotations

import asyncio
import json

import httpx

from novel_agent.adapters.model.openai_chat import OpenAICompatibleChatEndpoint
from novel_agent.domain.generation import WritingLengthPolicy, WritingLoopBudgets
from novel_agent.domain.ids import RunId, StableId, TaskId
from novel_agent.domain.model_calls import (
    BudgetSource,
    ModelCallPurpose,
    ModelRequest,
    ModelRole,
)
from novel_agent.runtime.production_bootstrap import (
    QWEN38_27B_FP8_8005_ENDPOINT_PROFILE,
    _default_writing_policy,
    load_production_assembly_spec,
    resolve_registered_model_endpoints,
)
from novel_agent.runtime.production_components import ProductionWriterModelRequestFactory


def test_production_writer_factory_uses_longform_sampling_and_thinking() -> None:
    factory = ProductionWriterModelRequestFactory(
        role=ModelRole.IMPLEMENTATION,
        purpose=ModelCallPurpose.DEVELOPMENT,
        max_output_tokens=12_000,
        timeout_seconds=120.0,
        temperature=0.8,
        enable_thinking=True,
        thinking_token_budget=2_048,
    )
    request = factory(
        type(
            "Loop",
            (),
            {
                "task_id": TaskId("task.writer-params"),
                "run_id": RunId("run.writer-params"),
                "attempt_id": None,
            },
        )()
    )
    assert request.temperature == 0.8
    assert request.enable_thinking is True
    assert request.thinking_token_budget == 2_048
    assert request.max_output_tokens == 12_000


def test_production_writing_policy_uses_longform_length_and_loop_budgets() -> None:
    policy = _default_writing_policy(load_production_assembly_spec())
    assert policy.length_policy == WritingLengthPolicy(
        minimum_characters=3_000,
        target_characters=5_000,
        maximum_characters=8_000,
    )
    assert policy.budgets.max_reactive_memory_rounds == 2
    assert policy.budgets.max_memory_questions == 6
    assert policy.budgets.max_writer_turns == 3
    assert policy.budgets.max_local_repairs == 2
    assert policy.budgets.max_major_rewrites == 1
    assert policy.budgets.max_post_draft_model_calls == 6
    assert policy.budgets.reserved_output_tokens == 14_048
    assert isinstance(policy.budgets, WritingLoopBudgets)


def test_8005_profile_raises_provider_output_capability_to_writer_body() -> None:
    endpoints = resolve_registered_model_endpoints(QWEN38_27B_FP8_8005_ENDPOINT_PROFILE)
    assert endpoints[0].output_limit == 12_000
    assert endpoints[0].adapter.max_output_tokens == 12_000
    assert endpoints[0].adapter.temperature == 0.0


def test_openai_adapter_prefers_request_temperature() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}],
                "model": "qwen38-27b-fp8",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    endpoint = OpenAICompatibleChatEndpoint(
        base_url="http://127.0.0.1:8005/v1",
        model="qwen38-27b-fp8",
        temperature=0.0,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    asyncio.run(
        endpoint.generate(
            ModelRequest(
                request_id=StableId("request.temp"),
                run_id=RunId("run.temp"),
                task_id=TaskId("task.temp"),
                model_role=ModelRole.IMPLEMENTATION,
                purpose=ModelCallPurpose.DEVELOPMENT,
                trace_id="trace-temp",
                prompt="{}",
                max_output_tokens=12_000,
                timeout_seconds=10,
                temperature=0.8,
                enable_thinking=True,
                thinking_token_budget=2_048,
                budget_source=BudgetSource.EXPLICIT_REQUEST,
            )
        )
    )
    assert captured[0]["temperature"] == 0.8


def test_non_canon_world_states_are_not_injected_as_current_truth() -> None:
    """Planned content must not reach the chapter as ``Canon current state``.

    A live genesis recorded a volume-4 event as ``accepted_world_fact``, and the
    Writer labelled every participating character's state as canon.  The injection
    now separates accepted facts from classified non-facts by truth class, so the
    classifier's decision actually reaches the draft.
    """

    from novel_agent.domain.memory import TruthClass
    from novel_agent.domain.world import StateRecord, StoryTime

    accepted = StateRecord(
        state_id=StableId("state.accepted"),
        subject_id=StableId("entity.bootstrap.1"),
        predicate="character",
        value="边镇少年，斩星武者。",
        valid_time=StoryTime(worldline="main", start_ordinal=0),
        truth_class=TruthClass.ACCEPTED_WORLD_FACT,
    )
    planned = StateRecord(
        state_id=StableId("state.planned"),
        subject_id=StableId("entity.bootstrap.1"),
        predicate="character",
        value="第四卷：锻打而成武器沉曜。",
        valid_time=StoryTime(worldline="main", start_ordinal=0),
        truth_class=TruthClass.PREDICTION,
    )

    canon = tuple(
        state
        for state in (accepted, planned)
        if state.truth_class is TruthClass.ACCEPTED_WORLD_FACT
    )
    planned_only = tuple(
        state
        for state in (accepted, planned)
        if state.truth_class is not TruthClass.ACCEPTED_WORLD_FACT
    )

    assert [state.state_id.root for state in canon] == ["state.accepted"]
    assert [state.state_id.root for state in planned_only] == ["state.planned"]
    assert planned_only[0].truth_class.value == "prediction"
