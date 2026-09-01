#!/usr/bin/env python3
"""Exercise raw-before-parse recovery across two real Python processes."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.model.fake import FakeModelEndpoint
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.model_call_ledger import SqlModelCallLedger
from novel_agent.domain.ids import RunId, StableId, TaskId
from novel_agent.domain.model_calls import (
    ModelCallLedgerStatus,
    ModelCallPurpose,
    ModelRequest,
    ModelRole,
    ProviderModelResult,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint


class _Output(BaseModel):
    model_config = ConfigDict(strict=True)
    answer: str


class _CountingFakeEndpoint(FakeModelEndpoint):
    def __init__(self, count_path: Path) -> None:
        super().__init__('{"answer":"durable"}')
        self._count_path = count_path

    async def generate(self, request: ModelRequest) -> ProviderModelResult:
        count = (
            int(self._count_path.read_text(encoding="utf-8")) if self._count_path.exists() else 0
        )
        self._count_path.write_text(f"{count + 1}\n", encoding="utf-8")
        return await super().generate(request)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("send", "reparse"), required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--objects", type=Path, required=True)
    parser.add_argument("--provider-count", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def _request() -> ModelRequest:
    return ModelRequest(
        request_id=StableId("request.cross-process.raw-before-parse"),
        run_id=RunId("run.cross-process.raw-before-parse"),
        task_id=TaskId("task.cross-process.raw-before-parse"),
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id="trace.cross-process.raw-before-parse",
        prompt='{"answer":"durable"}',
        max_output_tokens=2_048,
        enable_thinking=True,
        thinking_token_budget=128,
        scheduling_stage="benchmark.cross_process_reparse",
    )


def _gateway(args: argparse.Namespace) -> tuple[ModelGateway, SqlModelCallLedger]:
    engine = create_engine(f"sqlite+pysqlite:///{args.database}")
    Base.metadata.create_all(engine)
    session_factory = build_session_factory(engine)
    ledger = SqlModelCallLedger(session_factory)
    endpoint = RegisteredModelEndpoint(
        role=ModelRole.BATCH_TEST,
        endpoint_name="cross-process-fake",
        model_name="cross-process-fake-v1",
        adapter=_CountingFakeEndpoint(args.provider_count),
        sequence_limit=8_192,
        output_limit=2_048,
        safety_allowance_tokens=64,
        estimated_reasoning_reserve=128,
        reasoning_included_in_completion_tokens=False,
        global_output_cap=4_096,
    )
    return (
        ModelGateway(
            (endpoint,),
            call_ledger=ledger,
            raw_artifacts=ArtifactRepository(FilesystemObjectStore(args.objects)),
        ),
        ledger,
    )


def _send(args: argparse.Namespace) -> int:
    gateway, _ledger = _gateway(args)
    asyncio.run(gateway.generate_text(_request()))
    # The provider raw envelope and COMPLETED ledger row are durable here.  Exit
    # before any structured parse/checkpoint work to model a worker crash.
    return 37


def _reparse(args: argparse.Namespace) -> int:
    gateway, ledger = _gateway(args)
    request = _request()
    entry = ledger.load(request.request_id)
    if entry is None:
        raise RuntimeError("reparse process cannot find the durable request")
    if entry.status is not ModelCallLedgerStatus.COMPLETED:
        raise RuntimeError(f"raw-before-parse request is not completed: {entry.status.value}")
    parsed, record = gateway.reparse_structured_from_raw(request, _Output)
    if not args.output:
        raise RuntimeError("reparse phase requires an evidence output")
    entries = ledger.list_for_run(request.run_id)
    payload = {
        "status": "reparsed",
        "provider_call_count": int(args.provider_count.read_text(encoding="utf-8")),
        "ledger_request_count": len(entries),
        "request_id": request.request_id.root,
        "raw_artifact_ref": entry.raw_artifact_ref.model_dump(mode="json")
        if entry.raw_artifact_ref is not None
        else None,
        "effective_budget": entry.effective_budget.model_dump(mode="json"),
        "reasoning_included_in_completion_tokens": (entry.reasoning_included_in_completion_tokens),
        "parsed": parsed.model_dump(mode="json"),
        "record_request_id": record.request_id.root,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


def main() -> int:
    args = _parser().parse_args()
    args.database = args.database.resolve()
    args.objects = args.objects.resolve()
    args.provider_count = args.provider_count.resolve()
    if args.phase == "send":
        args.database.parent.mkdir(parents=True, exist_ok=True)
        args.objects.mkdir(parents=True, exist_ok=True)
        args.provider_count.parent.mkdir(parents=True, exist_ok=True)
        return _send(args)
    return _reparse(args)


if __name__ == "__main__":
    raise SystemExit(main())
