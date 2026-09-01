"""U3-B durable ledger aggregation and report reconstruction tests."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import create_engine

from novel_agent.adapters.model.fake import FakeModelEndpoint
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.domain.ids import RunId, StableId, TaskId
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelCostAvailability,
    ModelRequest,
    ModelRole,
    ModelUsage,
    ProviderModelResult,
)
from novel_agent.services.event_log import RunEventLogRepository
from novel_agent.services.model_call_ledger import (
    InMemoryModelCallLedger,
    aggregate_model_calls,
)
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.runtime_reporting import RuntimeReportService


def _request(
    *,
    request_id: str = "model.aggregate.request",
    run_id: str = "run.aggregate",
    prompt: str = "aggregate",
) -> ModelRequest:
    return ModelRequest(
        request_id=StableId(request_id),
        run_id=RunId(run_id),
        task_id=TaskId("task.aggregate"),
        attempt_id=StableId("attempt.aggregate"),
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id="trace.aggregate",
        prompt=prompt,
        scheduling_stage="writer",
    )


def _endpoint(adapter: FakeModelEndpoint) -> RegisteredModelEndpoint:
    return RegisteredModelEndpoint(
        role=ModelRole.BATCH_TEST,
        endpoint_name="aggregate-endpoint",
        model_name="aggregate-model",
        adapter=adapter,
    )


def test_ledger_aggregation_distinguishes_schema_retry_and_unknown_cost() -> None:
    class SequenceEndpoint(FakeModelEndpoint):
        def __init__(self) -> None:
            super().__init__("")
            self.responses = iter(("not-json", '{"answer":"ok"}'))

        async def generate(self, request: ModelRequest) -> ProviderModelResult:
            self.response_text = next(self.responses)
            return await super().generate(request)

    class OutputModel(BaseModel):
        answer: str

    ledger = InMemoryModelCallLedger()
    gateway = ModelGateway(
        (_endpoint(SequenceEndpoint()),),
        structured_max_retries=1,
        call_ledger=ledger,
    )
    request = _request()

    asyncio.run(gateway.generate_structured(request, OutputModel))

    entries = ledger.list_for_run(request.run_id)
    aggregate = aggregate_model_calls(entries)
    assert len(aggregate) == 1
    result = aggregate[0]
    assert result.request_count == 2
    assert result.schema_retry_count == 1
    assert result.logical_phase == "writer"
    assert result.attempt_id == request.attempt_id
    assert result.status_counts == {"validation_rejected": 1, "completed": 1}
    assert result.cost_usd is None
    assert result.cost_availability is ModelCostAvailability.UNKNOWN


def test_ledger_aggregation_reports_known_cost_without_filling_unknown_as_zero() -> None:
    class KnownCostEndpoint(FakeModelEndpoint):
        async def generate(self, request: ModelRequest) -> ProviderModelResult:
            return ProviderModelResult(
                text="known",
                model_version="known-v1",
                usage=ModelUsage(
                    input_tokens=3,
                    output_tokens=4,
                    cost_usd=Decimal("0.12"),
                    cost_availability=ModelCostAvailability.KNOWN,
                ),
            )

    ledger = InMemoryModelCallLedger()
    gateway = ModelGateway((_endpoint(KnownCostEndpoint("ignored")),), call_ledger=ledger)
    request = _request(request_id="model.known.request")
    asyncio.run(gateway.generate_text(request))

    aggregate = aggregate_model_calls(ledger.list_for_run(request.run_id))[0]
    assert aggregate.cost_availability is ModelCostAvailability.KNOWN
    assert aggregate.cost_usd == Decimal("0.12")
    assert aggregate.input_tokens == 3
    assert aggregate.output_tokens == 4


def test_runtime_report_rebuilds_model_usage_from_ledger(tmp_path: Path) -> None:
    database = create_engine(f"sqlite+pysqlite:///{tmp_path / 'report.db'}")
    Base.metadata.create_all(database)
    factory = build_session_factory(database)
    ledger = InMemoryModelCallLedger()
    gateway = ModelGateway(
        (_endpoint(FakeModelEndpoint("response")),),
        call_ledger=ledger,
    )
    request = _request(request_id="model.report.request", run_id="run.report-ledger")
    asyncio.run(gateway.generate_text(request))

    manifest = (
        Path(__file__).parents[2]
        / "src"
        / "novel_agent"
        / "runtime"
        / "stage5_development_manifest.json"
    )
    report = RuntimeReportService(
        factory,
        RunEventLogRepository(factory),
        ledger,
    ).export(
        request.run_id,
        manifest_path=manifest,
        executable_commit="a" * 40,
    )

    assert report.model_request_count == 1
    assert report.model_cost_usd is None
    assert report.model_cost_availability is ModelCostAvailability.UNKNOWN
    assert len(report.model_call_aggregates) == 1
    assert report.model_call_aggregates[0].logical_phase == "writer"
