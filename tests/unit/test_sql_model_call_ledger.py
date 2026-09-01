"""Durable ModelGateway sent/raw ledger recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.model.fake import FakeModelEndpoint
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.model_call_ledger import SqlModelCallLedger
from novel_agent.domain.ids import RunId, StableId, TaskId
from novel_agent.domain.model_calls import (
    EffectiveBudgetResult,
    ModelCallLedgerStatus,
    ModelCallPurpose,
    ModelRequest,
    ModelRole,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.model_call_ledger import ModelCallLedgerCollision
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint


def _request() -> ModelRequest:
    return ModelRequest(
        request_id=StableId("model.sql-ledger.request"),
        run_id=RunId("run.sql-ledger"),
        task_id=TaskId("task.sql-ledger"),
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id="trace.sql-ledger",
        prompt='{"answer":"durable"}',
    )


def _endpoint(fake: FakeModelEndpoint) -> RegisteredModelEndpoint:
    return RegisteredModelEndpoint(
        role=ModelRole.BATCH_TEST,
        endpoint_name="sql-ledger-endpoint",
        model_name="sql-ledger-model",
        adapter=fake,
    )


def _budget(request: ModelRequest) -> EffectiveBudgetResult:
    return ModelGateway((_endpoint(FakeModelEndpoint("budget")),)).resolve_effective_budget(request)


class _Output(BaseModel):
    model_config = ConfigDict(strict=True)
    answer: str


def test_sql_ledger_survives_gateway_reconstruction_and_raw_reparse(tmp_path: Path) -> None:
    database = create_engine(f"sqlite+pysqlite:///{tmp_path / 'ledger.db'}")
    Base.metadata.create_all(database)
    session_factory = build_session_factory(database)
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    fake = FakeModelEndpoint('{"answer":"durable"}')
    first_gateway = ModelGateway(
        (_endpoint(fake),),
        call_ledger=SqlModelCallLedger(session_factory),
        raw_artifacts=artifacts,
    )
    request = _request()

    asyncio.run(first_gateway.generate_text(request))

    reloaded_ledger = SqlModelCallLedger(session_factory)
    entry = reloaded_ledger.load(request.request_id)
    assert entry is not None
    assert entry.status is ModelCallLedgerStatus.COMPLETED
    assert entry.effective_budget == _budget(request)
    assert entry.reasoning_included_in_completion_tokens is False
    assert entry.provider_sent_at is not None
    assert entry.raw_artifact_ref is not None

    second_gateway = ModelGateway(
        (_endpoint(fake),),
        call_ledger=reloaded_ledger,
        raw_artifacts=artifacts,
    )
    parsed, record = second_gateway.reparse_structured_from_raw(request, _Output)
    assert parsed.answer == "durable"
    assert record.request_id == request.request_id
    assert fake.requests[0].request_id == request.request_id
    assert fake.requests[0].prompt == request.prompt


def test_sql_ledger_reconstructs_attempt_and_logical_phase(tmp_path: Path) -> None:
    database = create_engine(f"sqlite+pysqlite:///{tmp_path / 'ledger-identity.db'}")
    Base.metadata.create_all(database)
    session_factory = build_session_factory(database)
    ledger = SqlModelCallLedger(session_factory)
    request = _request().model_copy(
        update={
            "attempt_id": StableId("attempt.sql-ledger.1"),
            "scheduling_stage": "writer.draft",
        }
    )

    created = ledger.create_requested(
        request,
        effective_budget=_budget(request),
        reasoning_included_in_completion_tokens=False,
    )
    assert created.attempt_id == request.attempt_id
    assert created.logical_phase == "writer.draft"

    reconstructed = SqlModelCallLedger(session_factory).load(request.request_id)
    assert reconstructed is not None
    assert reconstructed.attempt_id == request.attempt_id
    assert reconstructed.logical_phase == "writer.draft"
    assert reconstructed.attempt_id is not None
    assert reconstructed.logical_phase not in {"", "unknown"}


def test_sql_ledger_collision_and_missing_settlement_fail_closed(tmp_path: Path) -> None:
    database = create_engine(f"sqlite+pysqlite:///{tmp_path / 'ledger.db'}")
    Base.metadata.create_all(database)
    ledger = SqlModelCallLedger(build_session_factory(database))
    request = _request()
    budget = _budget(request)
    first = ledger.create_requested(
        request, effective_budget=budget, reasoning_included_in_completion_tokens=False
    )
    assert (
        ledger.create_requested(
            request, effective_budget=budget, reasoning_included_in_completion_tokens=False
        )
        == first
    )
    collided = request.model_copy(update={"prompt": '{"answer":"other"}'})
    with pytest.raises(ModelCallLedgerCollision, match="identity collision"):
        ledger.create_requested(
            collided, effective_budget=budget, reasoning_included_in_completion_tokens=False
        )
    missing = first.model_copy(update={"request_id": StableId("model.sql-ledger.absent")})
    with pytest.raises(KeyError, match="not reserved"):
        ledger.settle(missing)
    identity = first.model_copy(update={"run_id": RunId("run.other")})
    with pytest.raises(ModelCallLedgerCollision, match="settlement identity"):
        ledger.settle(identity)
    assert ledger.load(StableId("model.sql-ledger.absent")) is None
    assert ledger.list_for_prefix("model.sql-ledger.request")
    assert ledger.list_for_run(request.run_id)


def test_sql_ledger_rejects_terminal_status_overwrite(tmp_path: Path) -> None:
    database = create_engine(f"sqlite+pysqlite:///{tmp_path / 'ledger-terminal.db'}")
    Base.metadata.create_all(database)
    session_factory = build_session_factory(database)
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    fake = FakeModelEndpoint('{"answer":"durable"}')
    ledger = SqlModelCallLedger(session_factory)
    gateway = ModelGateway(
        (_endpoint(fake),),
        call_ledger=ledger,
        raw_artifacts=artifacts,
    )
    request = _request()
    asyncio.run(gateway.generate_text(request))
    completed = ledger.load(request.request_id)
    assert completed is not None
    with pytest.raises(ModelCallLedgerCollision, match="cannot be overwritten"):
        ledger.settle(
            completed.model_copy(
                update={
                    "status": ModelCallLedgerStatus.TRANSPORT_EXHAUSTED,
                    "transport_error_type": "late",
                }
            )
        )


def test_sql_ledger_preserves_requested_sent_state_before_provider_completion(
    tmp_path: Path,
) -> None:
    database = create_engine(f"sqlite+pysqlite:///{tmp_path / 'ledger.db'}")
    Base.metadata.create_all(database)
    session_factory = build_session_factory(database)
    ledger = SqlModelCallLedger(session_factory)
    request = _request()

    requested = ledger.create_requested(
        request,
        effective_budget=_budget(request),
        reasoning_included_in_completion_tokens=False,
    )
    sent = ledger.settle(requested.model_copy(update={"provider_sent_at": requested.requested_at}))

    reloaded = SqlModelCallLedger(session_factory).load(request.request_id)
    assert reloaded == sent
    assert reloaded is not None
    assert reloaded.status is ModelCallLedgerStatus.REQUESTED
    assert reloaded.provider_sent_at is not None
