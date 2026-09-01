from novel_agent.adapters.model.openai_chat import OpenAICompatibleChatEndpoint
from novel_agent.runtime.production_bootstrap import (
    DETERMINISTIC_FAKE_ENDPOINT_PROFILE,
    QWEN38_27B_FP8_8005_ENDPOINT_PROFILE,
    resolve_registered_model_endpoints,
)


def test_production_endpoint_resolution_remains_fail_closed_without_profile() -> None:
    assert resolve_registered_model_endpoints(None) == ()


def test_real_qwen38_8005_profile_is_explicit_and_contract_bounded() -> None:
    endpoints = resolve_registered_model_endpoints(QWEN38_27B_FP8_8005_ENDPOINT_PROFILE)

    assert len(endpoints) == 1
    registration = endpoints[0]
    adapter = registration.adapter
    assert isinstance(adapter, OpenAICompatibleChatEndpoint)
    assert registration.endpoint_name == "qwen38-27b-fp8@8005"
    assert registration.model_name == "qwen38-27b-fp8"
    assert registration.revision == "qwen38-27b-fp8"
    assert adapter.base_url == "http://127.0.0.1:8005/v1"
    assert adapter.model == "qwen38-27b-fp8"
    assert adapter.max_output_tokens == 8_000
    assert adapter.max_retries == 0
    assert adapter.is_external is False
    assert registration.sequence_limit == 131_072
    assert registration.output_limit == 8_000
    assert registration.safety_allowance_tokens == 1_000
    assert registration.estimated_reasoning_reserve == 2_048
    assert registration.default_thinking is False


def test_fake_profile_stays_explicitly_available() -> None:
    endpoints = resolve_registered_model_endpoints(DETERMINISTIC_FAKE_ENDPOINT_PROFILE)

    assert len(endpoints) == 1
    assert endpoints[0].endpoint_name == "deterministic-fake-production"
