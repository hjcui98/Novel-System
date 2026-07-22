from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import create_engine

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.telemetry import OpenTelemetryAdapter
from novel_agent.domain.changes import CommitResult, CommitStatus
from novel_agent.domain.ids import ProjectId, RunId, TaskId
from novel_agent.domain.runtime import RunEventType
from novel_agent.runtime import StageZeroWorkflow
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository
from tests.factories import SCHEMA_VERSION, make_manifest


@pytest.fixture
def workflow_runtime(
    tmp_path: Path,
) -> Iterator[
    tuple[
        StageZeroWorkflow,
        ArtifactRepository,
        CommitService,
        RunEventLogRepository,
        RunCheckpointRepository,
        InMemorySaver,
        OpenTelemetryAdapter,
        InMemorySpanExporter,
    ]
]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    commits = CommitService(factory)
    events = RunEventLogRepository(factory)
    checkpoints = RunCheckpointRepository(factory)
    checkpointer = InMemorySaver()
    span_exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    telemetry = OpenTelemetryAdapter(provider.get_tracer("stage0-test"))
    workflow = StageZeroWorkflow(artifacts, commits, events, checkpoints, checkpointer, telemetry)
    yield (
        workflow,
        artifacts,
        commits,
        events,
        checkpoints,
        checkpointer,
        telemetry,
        span_exporter,
    )
    provider.shutdown()
    engine.dispose()


def test_workflow_interrupts_resumes_and_commits_without_model_calls(
    workflow_runtime: tuple[
        StageZeroWorkflow,
        ArtifactRepository,
        CommitService,
        RunEventLogRepository,
        RunCheckpointRepository,
        InMemorySaver,
        OpenTelemetryAdapter,
        InMemorySpanExporter,
    ],
    tmp_path: Path,
) -> None:
    (
        workflow,
        artifacts,
        commits,
        events,
        checkpoints,
        checkpointer,
        telemetry,
        span_exporter,
    ) = workflow_runtime
    project_id = ProjectId("project.workflow")
    commits.initialize_project(make_manifest(project_id))
    chapter = artifacts.put("固定测试章节".encode(), "text/plain", SCHEMA_VERSION)
    initial = workflow.initial_state(
        project_id,
        RunId("run.workflow"),
        TaskId("task.workflow"),
        chapter.model_dump(mode="json"),
        trace_id="trace-workflow",
    )
    config: RunnableConfig = {"configurable": {"thread_id": "thread-workflow"}}

    interrupted = cast(dict[str, Any], workflow.graph.invoke(initial, config))

    assert "__interrupt__" in interrupted
    assert interrupted["sequence_no"] == 5
    serializable_state = {
        key: value for key, value in interrupted.items() if key != "__interrupt__"
    }
    assert "固定测试章节" not in json.dumps(serializable_state, ensure_ascii=False)
    checkpoint = checkpoints.latest(RunId("run.workflow"))
    assert checkpoint is not None
    assert checkpoint.event_position == 7
    assert len(events.replay(RunId("run.workflow"))) == 7

    restarted = StageZeroWorkflow(artifacts, commits, events, checkpoints, checkpointer, telemetry)
    completed = restarted.resume("thread-workflow")

    assert completed["status"] == "completed"
    assert completed["resumed"] is True
    assert completed["sequence_no"] == 14
    result = CommitResult.model_validate_json(json.dumps(completed["commit_result"]))
    assert result.status is CommitStatus.ACCEPTED
    assert result.commit_id == commits.current_commit(project_id)
    evaluation = artifacts.read_verified(
        restarted._artifact_from_state(completed["evaluation_artifact"])
    )
    assert b"workflow_completed" in evaluation

    persisted_events = events.replay(RunId("run.workflow"))
    event_types = tuple(event.event_type for event in persisted_events)
    assert event_types == (
        RunEventType.RUN_CREATED,
        RunEventType.ARTIFACT_PRODUCED,
        RunEventType.ARTIFACT_PRODUCED,
        RunEventType.TASK_STARTED,
        RunEventType.TASK_COMPLETED,
        RunEventType.TASK_SUSPENDED,
        RunEventType.CHECKPOINT_CREATED,
        RunEventType.RUN_RESUMED,
        RunEventType.TASK_COMPLETED,
        RunEventType.TASK_COMPLETED,
        RunEventType.COMMIT_REQUESTED,
        RunEventType.COMMIT_ACCEPTED,
        RunEventType.ARTIFACT_PRODUCED,
        RunEventType.RUN_COMPLETED,
    )

    assert restarted.resume("thread-workflow")["status"] == "completed"
    assert len(events.replay(RunId("run.workflow"))) == 14
    assert len({event.trace_id for event in persisted_events}) == 1
    assert all(event.span_id for event in persisted_events)
    finished_spans = span_exporter.get_finished_spans()
    assert {format(span.context.trace_id, "032x") for span in finished_spans} == {
        persisted_events[0].trace_id
    }
    exported_span_ids = {format(span.context.span_id, "016x") for span in finished_spans}
    assert {event.span_id for event in persisted_events} <= exported_span_ids

    baseline_engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(baseline_engine)
    baseline_factory = build_session_factory(baseline_engine)
    baseline_artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "baseline-objects"))
    baseline_commits = CommitService(baseline_factory)
    baseline_events = RunEventLogRepository(baseline_factory)
    baseline_checkpoints = RunCheckpointRepository(baseline_factory)
    baseline_commits.initialize_project(make_manifest(project_id))
    baseline_chapter = baseline_artifacts.put("固定测试章节".encode(), "text/plain", SCHEMA_VERSION)
    baseline_workflow = StageZeroWorkflow(
        baseline_artifacts,
        baseline_commits,
        baseline_events,
        baseline_checkpoints,
        InMemorySaver(),
        telemetry,
    )
    baseline_initial = baseline_workflow.initial_state(
        project_id,
        RunId("run.workflow.baseline"),
        TaskId("task.workflow.baseline"),
        baseline_chapter.model_dump(mode="json"),
        trace_id="trace-workflow-baseline",
    )
    baseline_config: RunnableConfig = {"configurable": {"thread_id": "thread-workflow-baseline"}}
    baseline_interrupted = baseline_workflow.graph.invoke(baseline_initial, baseline_config)
    assert "__interrupt__" in baseline_interrupted
    baseline_completed = baseline_workflow.resume("thread-workflow-baseline")
    baseline_result = CommitResult.model_validate_json(
        json.dumps(baseline_completed["commit_result"])
    )

    assert baseline_result.commit_id == result.commit_id
    baseline_engine.dispose()
