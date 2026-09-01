"""U3-A raw-before-parse and provider-sent ledger evidence."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.model.fake import FakeModelEndpoint
from novel_agent.adapters.model.openai_chat import (
    OpenAIChatOutputLengthError,
    OpenAICompatibleChatEndpoint,
)
from novel_agent.domain.artifacts import MODEL_RAW_RESPONSE_MEDIA_TYPE, ArtifactRef
from novel_agent.domain.ids import RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.model_calls import (
    ModelCallLedgerEntry,
    ModelCallLedgerStatus,
    ModelCallPurpose,
    ModelRequest,
    ModelRole,
    ProviderModelResult,
    RawModelResponseArtifact,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.model_gateway import (
    ModelGateway,
    RawResponsePersistenceError,
    RawResponseReparseError,
    RegisteredModelEndpoint,
)


def _request(*, prompt: str = "raw evidence") -> ModelRequest:
    return ModelRequest(
        request_id=StableId("model.raw.request"),
        run_id=RunId("run.raw-evidence"),
        task_id=TaskId("task.raw-evidence"),
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id="trace.raw-evidence",
        prompt=prompt,
        response_schema=None,
    )


def _endpoint(adapter: FakeModelEndpoint) -> RegisteredModelEndpoint:
    return RegisteredModelEndpoint(
        role=ModelRole.BATCH_TEST,
        endpoint_name="raw-test-endpoint",
        model_name="raw-test-model",
        adapter=adapter,
    )


def _repository(tmp_path: Path) -> ArtifactRepository:
    return ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))


class _ProviderIdentityFake(FakeModelEndpoint):
    def provider_request_id(self, request: ModelRequest) -> str:
        return f"provider.{request.request_id.root}"


class _Output(BaseModel):
    model_config = ConfigDict(strict=True)
    answer: str


def test_gateway_marks_sent_and_persists_raw_before_returning_text(tmp_path: Path) -> None:
    fake = _ProviderIdentityFake('{"answer":"ok"}')
    repository = _repository(tmp_path)
    gateway = ModelGateway(
        (_endpoint(fake),),
        raw_artifacts=repository,
    )
    request = _request()

    result = asyncio.run(gateway.generate_text(request))

    entry = gateway.call_ledger.load(request.request_id)
    assert entry is not None
    assert entry.status is ModelCallLedgerStatus.COMPLETED
    assert entry.provider_sent_at is not None
    assert entry.provider_request_id == "provider.model.raw.request"
    assert entry.raw_response_hash is not None
    assert entry.raw_artifact_ref is not None
    assert entry.raw_artifact_ref.media_type == MODEL_RAW_RESPONSE_MEDIA_TYPE
    assert entry.raw_artifact_ref.artifact_id != entry.raw_response_hash
    # The gateway owns the same repository instance; this read verifies the persisted bytes.
    raw = RawModelResponseArtifact.model_validate_json(
        repository.read_verified(entry.raw_artifact_ref),
        strict=True,
    )
    assert raw.request_id == request.request_id
    assert raw.request_hash == entry.request_hash
    assert raw.raw_response_text == result.text
    assert raw.raw_response_hash == entry.raw_response_hash
    assert len(fake.requests) == 1


def test_structured_reparse_uses_raw_artifact_without_provider_call(tmp_path: Path) -> None:
    fake = FakeModelEndpoint('{"answer":"recoverable"}')
    gateway = ModelGateway(
        (_endpoint(fake),),
        raw_artifacts=_repository(tmp_path),
    )
    request = _request()
    asyncio.run(gateway.generate_text(request))

    parsed, record = gateway.reparse_structured_from_raw(request, _Output)

    assert parsed.answer == "recoverable"
    assert record.request_id == request.request_id
    assert len(fake.requests) == 1


def test_output_length_is_typed_and_retains_partial_raw_usage(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-length-1",
                "model": "qwen-test-v1",
                "choices": [{"finish_reason": "length", "message": {"content": '{"partial":'}}],
                "usage": {
                    "prompt_tokens": 101,
                    "completion_tokens": 4096,
                    "completion_tokens_details": {"reasoning_tokens": 3000},
                },
            },
        )

    adapter = OpenAICompatibleChatEndpoint(
        base_url="http://127.0.0.1:8005/v1",
        model="qwen-test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    repository = _repository(tmp_path)
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="raw-length-endpoint",
                model_name="qwen-test",
                adapter=adapter,
            ),
        ),
        raw_artifacts=repository,
    )
    request = _request(prompt="length evidence")

    with pytest.raises(OpenAIChatOutputLengthError):
        asyncio.run(gateway.generate_text(request))

    entry = gateway.call_ledger.load(request.request_id)
    assert entry is not None
    assert entry.status is ModelCallLedgerStatus.OUTPUT_INCOMPLETE
    assert entry.transport_error_type == "OutputLengthError"
    assert entry.raw_response_hash is not None
    assert entry.raw_artifact_ref is not None
    assert entry.call_record is not None
    assert entry.call_record.usage.input_tokens == 101
    assert entry.call_record.usage.output_tokens == 4096
    assert entry.call_record.usage.reasoning_tokens == 3000
    raw = RawModelResponseArtifact.model_validate_json(
        repository.read_verified(entry.raw_artifact_ref),
        strict=True,
    )
    assert raw.raw_response_text == '{"partial":'
    assert raw.finish_reason == "length"
    assert raw.provider_request_id == "chatcmpl-length-1"
    assert entry.provider_request_id == "chatcmpl-length-1"
    assert len(adapter.requests) == 1


def test_invalid_raw_reparse_marks_validation_rejected_without_retry(tmp_path: Path) -> None:
    fake = FakeModelEndpoint('{"answer":7}')
    gateway = ModelGateway(
        (_endpoint(fake),),
        raw_artifacts=_repository(tmp_path),
    )
    request = _request()
    asyncio.run(gateway.generate_text(request))

    with pytest.raises(ValidationError):
        gateway.reparse_structured_from_raw(request, _Output)

    entry = gateway.call_ledger.load(request.request_id)
    assert entry is not None
    assert entry.status is ModelCallLedgerStatus.VALIDATION_REJECTED
    assert entry.raw_artifact_ref is not None
    assert len(fake.requests) == 1


def test_reparse_rejects_a_raw_artifact_from_another_request(tmp_path: Path) -> None:
    first = FakeModelEndpoint('{"answer":"first"}')
    second = FakeModelEndpoint('{"answer":"second"}')
    repository = _repository(tmp_path)
    first_gateway = ModelGateway((_endpoint(first),), raw_artifacts=repository)
    first_request = _request()
    asyncio.run(first_gateway.generate_text(first_request))
    first_entry = first_gateway.call_ledger.load(first_request.request_id)
    assert first_entry is not None and first_entry.raw_artifact_ref is not None

    second_gateway = ModelGateway((_endpoint(second),), raw_artifacts=repository)
    second_request = _request(prompt="different request")
    # Reserve a second identity through the provider, then try to substitute the first raw.
    asyncio.run(second_gateway.generate_text(second_request))
    with pytest.raises(RawResponseReparseError, match="does not match"):
        second_gateway.reparse_structured_from_raw(
            second_request,
            _Output,
            raw_artifact_ref=first_entry.raw_artifact_ref,
        )
    assert len(second.requests) == 1


def test_timeout_after_sent_is_uncertain_and_has_no_raw_artifact(tmp_path: Path) -> None:
    class SlowFake(FakeModelEndpoint):
        async def generate(self, request: ModelRequest) -> ProviderModelResult:
            await asyncio.sleep(0.02)
            return await super().generate(request)

    fake = SlowFake("late")
    gateway = ModelGateway((_endpoint(fake),), raw_artifacts=_repository(tmp_path))
    request = _request()
    request = request.model_copy(update={"timeout_seconds": 0.001})

    with pytest.raises(TimeoutError):
        asyncio.run(gateway.generate_text(request))

    entry = gateway.call_ledger.load(request.request_id)
    assert entry is not None
    assert entry.status is ModelCallLedgerStatus.UNCERTAIN
    assert entry.provider_sent_at is not None
    assert entry.raw_artifact_ref is None


def test_worker_stop_after_sent_is_uncertain(tmp_path: Path) -> None:
    started = asyncio.Event()

    class BlockingFake(FakeModelEndpoint):
        async def generate(self, request: ModelRequest) -> ProviderModelResult:
            self.requests.append(request)
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("worker stop should cancel the provider call")

    async def exercise() -> ModelCallLedgerStatus:
        fake = BlockingFake("never")
        gateway = ModelGateway((_endpoint(fake),), raw_artifacts=_repository(tmp_path))
        task = asyncio.create_task(gateway.generate_text(_request()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        entry = gateway.call_ledger.load(StableId("model.raw.request"))
        assert entry is not None
        assert entry.provider_sent_at is not None
        assert entry.transport_error_type == "CancelledError"
        assert len(fake.requests) == 1
        return entry.status

    assert asyncio.run(exercise()) is ModelCallLedgerStatus.UNCERTAIN


def test_provider_success_with_raw_persistence_failure_is_uncertain(tmp_path: Path) -> None:
    class FailingRepository(ArtifactRepository):
        def __init__(self) -> None:
            pass

        def put(self, data: bytes, media_type: str, schema_version: SchemaVersion) -> ArtifactRef:
            raise OSError("object store stopped")

    fake = FakeModelEndpoint('{"answer":"lost"}')
    gateway = ModelGateway((_endpoint(fake),), raw_artifacts=FailingRepository())
    request = _request()

    with pytest.raises(RawResponsePersistenceError, match="not retained"):
        asyncio.run(gateway.generate_text(request))

    entry = gateway.call_ledger.load(request.request_id)
    assert entry is not None
    assert entry.status is ModelCallLedgerStatus.UNCERTAIN
    assert entry.transport_error_type == "RawResponsePersistenceError"
    assert entry.provider_sent_at is not None
    assert entry.raw_artifact_ref is None
    assert len(fake.requests) == 1


def test_model_call_schema_exports_match_raw_contracts() -> None:
    schemas = Path(__file__).parents[2] / "schemas" / "stage0"
    for filename, model in (
        ("ModelCallLedgerEntry.schema.json", ModelCallLedgerEntry),
        ("ProviderModelResult.schema.json", ProviderModelResult),
        ("RawModelResponseArtifact.schema.json", RawModelResponseArtifact),
    ):
        assert json.loads((schemas / filename).read_text()) == model.model_json_schema()
