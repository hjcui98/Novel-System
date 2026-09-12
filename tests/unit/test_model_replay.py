"""R5: a completed model call is replayed, never paid for twice.

A restart must not re-issue a request the ledger already completed, and it must not
replay anything whose identity drifted: the ledger request hash is the source of truth
for "same request", and the stored raw response is re-read and re-parsed under the
current strict contract instead of trusting a stored verdict.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.model.fake import FakeModelEndpoint
from novel_agent.domain.ids import ArtifactId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.model_calls import (
    ModelCallLedgerStatus,
    ModelCallPurpose,
    ModelRole,
    RawModelResponseArtifact,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.model_call_ledger import InMemoryModelCallLedger
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint

VERSION = SchemaVersion("1.0.0")


class _Answer(BaseModel):
    status: str


class _CountingEndpoint(FakeModelEndpoint):
    def __init__(self, text: str = '{"status": "ok"}') -> None:
        super().__init__(text)
        self.calls = 0

    async def generate(self, request):
        self.calls += 1
        return await super().generate(request)


def _request(
    *,
    request_id: str = "model.request.replay",
    prompt: str = "deterministic fixture",
    role: ModelRole = ModelRole.IMPLEMENTATION,
    purpose: ModelCallPurpose = ModelCallPurpose.DEVELOPMENT,
):
    from novel_agent.domain.model_calls import ModelRequest

    return ModelRequest(
        request_id=StableId(request_id),
        run_id=RunId("run.replay"),
        task_id=TaskId("task.replay"),
        model_role=role,
        purpose=purpose,
        trace_id="trace-replay",
        prompt=prompt,
    )


def _gateway(
    tmp_path: Path,
    adapter: FakeModelEndpoint,
    *,
    endpoint_name: str = "replay-endpoint",
    model_name: str = "fake-model",
) -> tuple[ModelGateway, ArtifactRepository, InMemoryModelCallLedger]:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    ledger = InMemoryModelCallLedger()
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.IMPLEMENTATION,
                endpoint_name=endpoint_name,
                model_name=model_name,
                adapter=adapter,
            ),
        ),
        call_ledger=ledger,
        raw_artifacts=artifacts,
        raw_artifact_schema_version=VERSION,
    )
    return gateway, artifacts, ledger


def test_a_completed_call_is_replayed_without_a_second_provider_call(tmp_path: Path) -> None:
    adapter = _CountingEndpoint()
    gateway, _artifacts, _ledger = _gateway(tmp_path, adapter)
    request = _request()

    first, first_call = asyncio.run(gateway.generate_structured(request, _Answer))
    outcome = gateway.replay_completed_structured(request, _Answer)

    assert first.status == "ok"
    assert outcome.replayed is True
    assert outcome.reason is None
    assert isinstance(outcome.output, _Answer)
    assert outcome.output.status == "ok"
    assert outcome.call_record is not None
    assert outcome.call_record.request_id == first_call.request_id
    assert adapter.calls == 1, "replay must not reach the provider"


def test_no_ledger_entry_is_reported_as_not_replayable(tmp_path: Path) -> None:
    adapter = _CountingEndpoint()
    gateway, _artifacts, _ledger = _gateway(tmp_path, adapter)

    outcome = gateway.replay_completed_structured(_request(), _Answer)

    assert outcome.replayed is False
    assert outcome.reason == "no_ledger_entry"
    assert adapter.calls == 0


def test_a_different_prompt_is_not_replayed(tmp_path: Path) -> None:
    adapter = _CountingEndpoint()
    gateway, _artifacts, _ledger = _gateway(tmp_path, adapter)
    asyncio.run(gateway.generate_structured(_request(), _Answer))

    outcome = gateway.replay_completed_structured(_request(prompt="a different chapter"), _Answer)

    assert outcome.replayed is False
    assert outcome.reason == "request_identity_drift"


def test_a_different_response_contract_is_not_replayed(tmp_path: Path) -> None:
    class _Other(BaseModel):
        value: str

    adapter = _CountingEndpoint('{"value": "ok"}')
    gateway, _artifacts, _ledger = _gateway(tmp_path, adapter)
    request = _request()
    asyncio.run(gateway.generate_structured(request, _Other, json_object_framing=True))

    outcome = gateway.replay_completed_structured(request, _Other, json_object_framing=True)
    assert outcome.replayed is True

    drifted = gateway.replay_completed_structured(request, _Answer)
    assert drifted.replayed is False
    assert drifted.reason == "request_identity_drift"


def test_provider_identity_drift_is_not_replayed(tmp_path: Path) -> None:
    """A stored response from a different model identity is never reused."""

    adapter = _CountingEndpoint()
    gateway, _artifacts, ledger = _gateway(tmp_path, adapter)
    request = _request()
    asyncio.run(gateway.generate_structured(request, _Answer))
    entry = ledger.load(request.request_id)
    assert entry is not None and entry.call_record is not None
    ledger.settle(
        entry.model_copy(
            update={"call_record": entry.call_record.model_copy(update={"model": "other-model"})}
        )
    )

    outcome = gateway.replay_completed_structured(request, _Answer)

    assert outcome.replayed is False
    assert outcome.reason == "model_identity_drift"


def test_an_uncertain_call_is_never_replayed_or_resent(tmp_path: Path) -> None:
    """A sent-but-unsettled call must be reconciled, not silently repeated."""

    class _UncertainEndpoint(FakeModelEndpoint):
        async def generate(self, request):
            raise TimeoutError("provider did not answer")

    gateway, _artifacts, ledger = _gateway(tmp_path, _UncertainEndpoint('{"status": "ok"}'))
    request = _request()
    with pytest.raises(TimeoutError):
        asyncio.run(gateway.generate_text(request))
    entry = ledger.load(request.request_id)
    assert entry is not None
    assert entry.status is ModelCallLedgerStatus.UNCERTAIN

    outcome = gateway.replay_completed_structured(request, _Answer)

    assert outcome.replayed is False
    assert outcome.reason == "uncertain_call_not_replayable"


def test_a_corrupted_raw_response_is_refused(tmp_path: Path) -> None:
    """The stored response is re-read and re-parsed, never trusted by verdict."""

    adapter = _CountingEndpoint()
    gateway, artifacts, ledger = _gateway(tmp_path, adapter)
    request = _request()
    asyncio.run(gateway.generate_structured(request, _Answer))
    entry = ledger.load(request.request_id)
    assert entry is not None and entry.raw_artifact_ref is not None

    original = RawModelResponseArtifact.model_validate_json(
        artifacts.read_verified(entry.raw_artifact_ref)
    )
    corrupted_text = "not json at all"
    corrupted = original.model_copy(update={"raw_response_text": corrupted_text})
    corrupted_ref = artifacts.put(
        canonical_json_bytes(corrupted.model_dump(mode="json")),
        entry.raw_artifact_ref.media_type,
        VERSION,
    )
    from novel_agent.services.artifacts import sha256_id

    ledger.settle(
        entry.model_copy(
            update={
                "raw_artifact_ref": corrupted_ref,
                # The stored hash is kept consistent with the corrupted text, so the
                # refusal must come from re-parsing, not from a hash shortcut.
                "raw_response_hash": sha256_id(corrupted_text.encode("utf-8")),
            }
        )
    )

    outcome = gateway.replay_completed_structured(request, _Answer)

    assert outcome.replayed is False
    assert outcome.reason == "raw_response_no_longer_parses"


def test_a_raw_response_hash_mismatch_is_refused(tmp_path: Path) -> None:
    adapter = _CountingEndpoint()
    gateway, artifacts, ledger = _gateway(tmp_path, adapter)
    request = _request()
    asyncio.run(gateway.generate_structured(request, _Answer))
    entry = ledger.load(request.request_id)
    assert entry is not None and entry.raw_artifact_ref is not None
    ledger.settle(entry.model_copy(update={"raw_response_hash": ArtifactId("sha256:" + "0" * 64)}))

    outcome = gateway.replay_completed_structured(request, _Answer)

    assert outcome.replayed is False
    assert outcome.reason == "raw_response_hash_mismatch"
    assert artifacts.read_verified(entry.raw_artifact_ref)


def test_validation_rejected_call_is_not_replayed(tmp_path: Path) -> None:
    adapter = _CountingEndpoint('{"wrong": true}')
    gateway, _artifacts, _ledger = _gateway(tmp_path, adapter)
    request = _request()
    with pytest.raises(ValidationError):
        asyncio.run(gateway.generate_structured(request, _Answer))

    outcome = gateway.replay_completed_structured(request, _Answer)

    assert outcome.replayed is False
    assert outcome.reason is not None and outcome.reason.startswith("terminal_status:")


def test_replay_does_not_consume_a_new_budget(tmp_path: Path) -> None:
    """A replayed response is not a new call, so usage stays with the original record."""

    adapter = _CountingEndpoint()
    gateway, _artifacts, ledger = _gateway(tmp_path, adapter)
    request = _request()
    _first, first_call = asyncio.run(gateway.generate_structured(request, _Answer))
    before = ledger.load(request.request_id)
    assert before is not None

    outcome = gateway.replay_completed_structured(request, _Answer)
    after = ledger.load(request.request_id)

    assert outcome.replayed is True
    assert outcome.call_record is not None
    assert outcome.call_record.usage.input_tokens == first_call.usage.input_tokens
    assert after is not None and after.completed_at == before.completed_at
    assert after.status is ModelCallLedgerStatus.COMPLETED


def test_repeating_a_completed_structured_request_reuses_the_answer(tmp_path: Path) -> None:
    """A retry of the same request must not reach the provider again.

    The ledger refuses to rebind a settled entry, so without this the second attempt
    surfaced a collision instead of the original answer.
    """

    adapter = _CountingEndpoint()
    gateway, _artifacts, _ledger = _gateway(tmp_path, adapter)
    request = _request()

    first, first_call = asyncio.run(gateway.generate_structured(request, _Answer))
    second, second_call = asyncio.run(gateway.generate_structured(request, _Answer))

    assert adapter.calls == 1
    assert first.status == second.status == "ok"
    assert first_call.request_id == second_call.request_id


def test_repeating_a_different_request_still_calls_the_provider(tmp_path: Path) -> None:
    adapter = _CountingEndpoint()
    gateway, _artifacts, _ledger = _gateway(tmp_path, adapter)

    asyncio.run(
        gateway.generate_structured(
            _request(request_id="model.request.replay.1", prompt="chapter one"), _Answer
        )
    )
    asyncio.run(
        gateway.generate_structured(
            _request(request_id="model.request.replay.2", prompt="chapter two"), _Answer
        )
    )

    assert adapter.calls == 2


def test_an_uncertain_retry_is_refused_rather_than_replayed(tmp_path: Path) -> None:
    """Reconcile first: an unresolved call is never answered from a stale response."""

    class _UncertainEndpoint(FakeModelEndpoint):
        async def generate(self, request):
            raise TimeoutError("provider did not answer")

    gateway, _artifacts, ledger = _gateway(tmp_path, _UncertainEndpoint('{"status": "ok"}'))
    request = _request()
    with pytest.raises(TimeoutError):
        asyncio.run(gateway.generate_structured(request, _Answer))

    entry = ledger.load(request.request_id)
    assert entry is not None and entry.status is ModelCallLedgerStatus.UNCERTAIN

    from novel_agent.services.model_gateway import ModelCallUncertainError

    with pytest.raises(ModelCallUncertainError):
        asyncio.run(gateway.generate_structured(request, _Answer))
