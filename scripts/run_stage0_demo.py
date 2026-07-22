#!/usr/bin/env python3
"""Run the deterministic Stage 0 vertical slice against local PostgreSQL and MinIO."""

from __future__ import annotations

import json
import os
import uuid
from contextlib import suppress
from urllib.parse import quote

from langgraph.checkpoint.postgres import PostgresSaver
from minio import Minio

from novel_agent.adapters.minio import MinioObjectStore
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.adapters.telemetry import build_otlp_telemetry
from novel_agent.domain.artifacts import (
    PlanRootRef,
    ProjectProfileRootRef,
    ReferenceRootRef,
    RootManifest,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.ids import ProjectId, RunId, SchemaVersion, TaskId
from novel_agent.runtime import StageZeroWorkflow
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService, ProjectAlreadyExistsError
from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository


def environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


def main() -> None:
    database_user = environment("POSTGRES_USER")
    database_password = environment("POSTGRES_PASSWORD")
    database_name = environment("POSTGRES_DB")
    database_port = environment("POSTGRES_PORT")
    connection_url = (
        f"postgresql://{quote(database_user, safe='')}:{quote(database_password, safe='')}"
        f"@127.0.0.1:{database_port}/{quote(database_name, safe='')}"
    )
    sqlalchemy_url = connection_url.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = build_engine(sqlalchemy_url)
    factory = build_session_factory(engine)

    minio_client = Minio(
        f"127.0.0.1:{environment('MINIO_API_PORT')}",
        access_key=environment("MINIO_ROOT_USER"),
        secret_key=environment("MINIO_ROOT_PASSWORD"),
        secure=False,
    )
    object_store = MinioObjectStore(minio_client, "novel-agent-artifacts")
    object_store.ensure_bucket()
    artifacts = ArtifactRepository(object_store)
    commits = CommitService(factory)
    events = RunEventLogRepository(factory)
    checkpoints = RunCheckpointRepository(factory)
    version = SchemaVersion("0.1.0")
    project_id = ProjectId("project.stage0.demo")

    roots = [
        artifacts.put(
            json.dumps({"root": name}, sort_keys=True).encode(),
            "application/json",
            version,
        )
        for name in ("text", "plan", "world", "reference", "project_profile")
    ]
    manifest = RootManifest(
        project_id=project_id,
        schema_version=version,
        text_root=TextRootRef(**roots[0].model_dump()),
        plan_root=PlanRootRef(**roots[1].model_dump()),
        world_root=WorldRootRef(**roots[2].model_dump()),
        reference_root=ReferenceRootRef(**roots[3].model_dump()),
        project_profile_root=ProjectProfileRootRef(**roots[4].model_dump()),
    )
    with suppress(ProjectAlreadyExistsError):
        commits.initialize_project(manifest)

    run_suffix = uuid.uuid4().hex
    run_id = RunId(f"run.stage0.{run_suffix}")
    thread_id = f"thread.stage0.{run_suffix}"
    chapter = artifacts.put(b"Stage 0 fixed chapter", "text/plain", version)
    telemetry, telemetry_provider = build_otlp_telemetry(
        f"http://127.0.0.1:{environment('OTEL_GRPC_PORT')}"
    )
    with PostgresSaver.from_conn_string(connection_url) as saver:
        saver.setup()
        workflow = StageZeroWorkflow(artifacts, commits, events, checkpoints, saver, telemetry)
        initial = workflow.initial_state(
            project_id,
            run_id,
            TaskId(f"task.stage0.{run_suffix}"),
            chapter.model_dump(mode="json"),
            trace_id=f"trace-{run_suffix}",
        )
        interrupted = workflow.graph.invoke(initial, {"configurable": {"thread_id": thread_id}})
        if "__interrupt__" not in interrupted:
            raise RuntimeError("workflow did not stop at the required checkpoint")
        completed = workflow.resume(thread_id)

    checkpoint = checkpoints.latest(run_id)
    if checkpoint is None:
        raise RuntimeError("workflow completed without a RunCheckpoint")
    result = {
        "run_id": run_id.root,
        "status": completed["status"],
        "commit_id": completed["commit_result"]["commit_id"],
        "event_count": len(events.replay(run_id)),
        "checkpoint_id": checkpoint.checkpoint_id.root,
        "model_calls": 0,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    telemetry_provider.shutdown()
    engine.dispose()


if __name__ == "__main__":
    main()
