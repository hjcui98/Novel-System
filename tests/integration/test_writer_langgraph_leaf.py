"""U7-A isolated LangGraph differential for the selected Writer leaf."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.runtime.stage3_writer import Stage3WritingLeafAdapter
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.editorial import EditorialVerdict
from novel_agent.domain.generation import WritingLoopRequest
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.writing_loop import WritingLoopResult, WritingLoopTerminalStatus
from novel_agent.runtime.writer_langgraph_leaf import (
    WRITER_LANGGRAPH_REQUEST_MEDIA_TYPE,
    WriterLangGraphLeafAdapter,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository
from novel_agent.services.writer_reactive_memory import ReactiveMemoryInputs
from tests.integration.test_writer_context_loop import _loop, _request


class _SequenceWriter:
    def __init__(self, results: tuple[WritingLoopResult, ...]) -> None:
        self._results = list(results)
        self.requests: list[WritingLoopRequest] = []

    async def run(self, request: WritingLoopRequest) -> WritingLoopResult:
        self.requests.append(request)
        return self._results.pop(0)


def _failed_result(request: WritingLoopRequest) -> WritingLoopResult:
    return WritingLoopResult(
        result_id=StableId(f"graph-result.{request.task_id.root}"),
        run_id=request.run_id,
        task_id=request.task_id,
        status=WritingLoopTerminalStatus.WRITER_FAILED,
        failure_detail="isolated graph fixture",
    )


def test_writer_langgraph_round_trips_public_result_and_keeps_state_ref_only(
    tmp_path: Path,
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    request = _request(artifacts, "u7a-failure")
    expected = _failed_result(request)
    delegate = _SequenceWriter((expected,))
    adapter = WriterLangGraphLeafAdapter(delegate, artifacts)

    result = asyncio.run(adapter.run(request))

    assert result == expected
    assert delegate.requests == [request]
    from novel_agent.runtime.writer_langgraph_leaf import WriterLangGraphState

    assert set(WriterLangGraphState.__annotations__) == {
        "request_artifact_ref",
        "result_artifact_ref",
        "checkpoint_ref",
        "terminal_status",
        "final_candidate_id",
        "phase",
    }
    assert WRITER_LANGGRAPH_REQUEST_MEDIA_TYPE.startswith("application/vnd.novel-agent")


def test_writer_langgraph_routes_typed_resumable_status_only_with_checkpoint(
    tmp_path: Path,
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    delegate = _SequenceWriter(())
    adapter = WriterLangGraphLeafAdapter(delegate, artifacts)
    checkpoint = ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "a" * 64),
        media_type="application/json",
        byte_length=1,
        schema_version=SchemaVersion("1.0.0"),
    )

    assert (
        adapter._route_typed_terminal(
            {
                "terminal_status": WritingLoopTerminalStatus.YIELDED.value,
                "checkpoint_ref": checkpoint.model_dump(mode="json"),
            }
        )
        == "resumable"
    )
    with pytest.raises(ValueError, match="checkpoint ref"):
        adapter._route_typed_terminal({"terminal_status": WritingLoopTerminalStatus.YIELDED.value})
    assert (
        adapter._route_typed_terminal(
            {"terminal_status": WritingLoopTerminalStatus.WRITER_FAILED.value}
        )
        == "terminal"
    )


def test_writer_langgraph_preserves_existing_checkpoint_lineage_on_recovery(
    tmp_path: Path,
) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    request = _request(artifacts, "u7a-recovery")
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    repositories = (RunEventLogRepository(factory), RunCheckpointRepository(factory))
    bounded = request.model_copy(
        update={"budgets": request.budgets.model_copy(update={"max_post_draft_model_calls": 0})}
    )
    loop, model_request, _ = _loop(
        tmp_path,
        repositories,
        bounded,
        route=EditorialVerdict.PASS,
        artifact_repository=artifacts,
    )
    first = asyncio.run(loop.execute(bounded, model_request, cast(Any, object())))
    assert first.status is WritingLoopTerminalStatus.YIELDED
    assert first.checkpoint_ref is not None
    resumed = bounded.model_copy(
        update={
            "resume_checkpoint_ref": first.checkpoint_ref,
            "budgets": bounded.budgets.model_copy(update={"max_post_draft_model_calls": 5}),
        }
    )
    second = asyncio.run(loop.execute(resumed, model_request, cast(Any, object())))
    assert second.status is WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY

    delegate = _SequenceWriter((first, second))
    adapter = WriterLangGraphLeafAdapter(delegate, artifacts)
    assert asyncio.run(adapter.run(bounded)).status is WritingLoopTerminalStatus.YIELDED
    assert (
        asyncio.run(adapter.run(resumed)).status is WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY
    )
    assert delegate.requests[1].resume_checkpoint_ref == first.checkpoint_ref
    engine.dispose()


def test_writer_langgraph_wraps_real_stage3_writer_for_a_small_sample(tmp_path: Path) -> None:
    def build_sample(
        suffix: str,
    ) -> tuple[WritingLoopRequest, Stage3WritingLeafAdapter, ArtifactRepository, Engine]:
        artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / suffix / "objects"))
        request = _request(artifacts, "u7a-real-small")
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        factory = build_session_factory(engine)
        repositories = (RunEventLogRepository(factory), RunCheckpointRepository(factory))
        loop, model_request, _ = _loop(
            tmp_path / suffix,
            repositories,
            request,
            route=EditorialVerdict.PASS,
            artifact_repository=artifacts,
        )
        direct = Stage3WritingLeafAdapter(
            loop,
            lambda _request: model_request,
            lambda _request: cast(ReactiveMemoryInputs, object()),
        )
        return request, direct, artifacts, engine

    direct_request, direct_leaf, _direct_artifacts, direct_engine = build_sample("direct")
    direct_result = asyncio.run(direct_leaf.run(direct_request))

    graph_request, graph_delegate, graph_artifacts, graph_engine = build_sample("graph")
    graph_result = asyncio.run(
        WriterLangGraphLeafAdapter(graph_delegate, graph_artifacts).run(graph_request)
    )

    assert graph_result.status is WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY
    assert graph_result.run_id == direct_result.run_id
    assert graph_result.task_id == direct_result.task_id
    assert graph_result.final_candidate_id == direct_result.final_candidate_id
    assert graph_result.final_text_artifact == direct_result.final_text_artifact
    assert graph_result.initial_draft is not None
    assert direct_result.initial_draft is not None
    assert graph_result.initial_draft.text_artifact == direct_result.initial_draft.text_artifact
    assert (
        graph_result.initial_draft.sidecar_artifact == direct_result.initial_draft.sidecar_artifact
    )
    assert (
        graph_result.initial_draft.raw_output_artifact
        == direct_result.initial_draft.raw_output_artifact
    )
    assert graph_result.initial_draft.writer_receipt.model_dump(
        exclude={"started_at", "completed_at"}
    ) == direct_result.initial_draft.writer_receipt.model_dump(
        exclude={"started_at", "completed_at"}
    )
    assert graph_result.final_text_artifact is not None
    assert graph_result.observation_artifact is not None
    assert graph_result.reconciliation is not None
    assert len(graph_result.model_call_records) == len(direct_result.model_call_records)
    assert [
        (record.request_id, record.model_role, record.purpose, record.usage)
        for record in graph_result.model_call_records
    ] == [
        (record.request_id, record.model_role, record.purpose, record.usage)
        for record in direct_result.model_call_records
    ]
    direct_engine.dispose()
    graph_engine.dispose()
