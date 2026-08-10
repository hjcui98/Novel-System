from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from alembic.config import Config
from langgraph.checkpoint.postgres import PostgresSaver
from minio import Minio
from opensearchpy import OpenSearch
from opentelemetry.sdk.trace import TracerProvider
from scripts.native_infra import LOOPBACK, NativeInfra
from sqlalchemy import Engine, func, inspect, select
from sqlalchemy.exc import OperationalError
from testcontainers.core.container import DockerContainer
from testcontainers.postgres import PostgresContainer
from urllib3 import PoolManager, Retry, Timeout
from urllib3.exceptions import MaxRetryError

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.minio import MinioObjectStore
from novel_agent.adapters.opensearch import OpenSearchIndex
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.adapters.postgres.models import (
    CommitReceiptRow,
    CommitRow,
    DerivedSnapshotRow,
    ProjectionOutboxRow,
    R1RecordRow,
    RunEventRow,
)
from novel_agent.adapters.telemetry import OpenTelemetryAdapter
from novel_agent.domain.artifacts import PlanRootRef, TextRootRef, WorldRootRef
from novel_agent.domain.ids import ProjectId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.memory import RetrievalChannel
from novel_agent.domain.runtime import RunEvent, RunEventType
from novel_agent.runtime import StageZeroWorkflow
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.benchmark_importer import canonical_json_bytes
from novel_agent.services.commits import CommitService
from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository
from novel_agent.services.memory_pipeline import AnchorBuilder
from novel_agent.services.projection import (
    ArtifactProjectionSourceLoader,
    DerivedProjectionService,
    DerivedSnapshotRepository,
    FullDerivedProjectionBuilder,
    ProjectionOutboxRepository,
)
from novel_agent.services.r1 import R1WorldRepository
from novel_agent.services.replay import ExactReplayProjectionBuilder
from novel_agent.services.search_retrieval import (
    DeterministicHashEmbedder,
    Stage1OpenSearchBackend,
    Stage1SearchIndexer,
)
from novel_agent.services.stage1_benchmark import Stage1NeedGenerator
from tests.factories import SCHEMA_VERSION, make_commit_request, make_manifest
from tests.fixtures.stage1_synthetic import make_synthetic_bundle

pytestmark = pytest.mark.integration
REPOSITORY_ROOT = Path(__file__).parents[2]


def wait_until_ready(probe: Callable[[], object], *, timeout_seconds: int = 90) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if probe():
                return
        except Exception:
            pass
        time.sleep(1)
    raise TimeoutError("infrastructure did not become ready")


def database_is_ready(engine: Engine) -> bool:
    with engine.connect() as connection:
        return cast(int, connection.exec_driver_sql("SELECT 1").scalar_one()) == 1


@pytest.fixture(scope="module")
def native_infra(request: pytest.FixtureRequest) -> Iterator[NativeInfra | None]:
    backend = os.environ.get("INFRA_BACKEND", "native")
    if backend == "docker":
        yield None
        return
    if backend != "native":
        pytest.fail(f"unsupported INFRA_BACKEND={backend!r}")
    failures_before = request.session.testsfailed
    infra = NativeInfra.integration()
    infra.up()
    try:
        yield infra
    finally:
        infra.down(clean=request.session.testsfailed == failures_before)


@contextmanager
def postgres_service(
    infra: NativeInfra | None,
) -> Iterator[tuple[str, Callable[[], None], Callable[[], None]]]:
    if infra is not None:
        yield (
            infra.database_url,
            lambda: infra.stop_service("postgres"),
            lambda: infra.start_service("postgres"),
        )
        return
    with PostgresContainer("postgres:17.10-bookworm", driver="psycopg") as postgres:
        wrapped = postgres.get_wrapped_container()
        yield (
            postgres.get_connection_url(),
            lambda: wrapped.stop(timeout=10),
            wrapped.start,
        )


def test_postgres_migration_and_durable_workflow_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_infra: NativeInfra | None,
) -> None:
    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    with postgres_service(native_infra) as (database_url, stop, start):
        alembic = Config(str(REPOSITORY_ROOT / "alembic.ini"))
        alembic.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(alembic, "head")
        engine = build_engine(database_url)
        try:
            tables = set(inspect(engine).get_table_names())
            assert {
                "project",
                "project_commit",
                "commit_receipt",
                "run_stream",
                "run_event",
                "run_checkpoint",
                "evaluation_entry",
                "projection_outbox",
                "derived_snapshot",
                "r1_record",
                "r1_record_entity",
            } <= tables

            stop()
            engine.dispose()
            try:
                with pytest.raises(OperationalError):
                    database_is_ready(engine)
            finally:
                start()
            wait_until_ready(lambda: database_is_ready(engine))

            factory = build_session_factory(engine)
            artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
            commits = CommitService(factory)
            events = RunEventLogRepository(factory)
            checkpoints = RunCheckpointRepository(factory)
            concurrent_event = RunEvent(
                event_id=StableId("event.integration.concurrent.1"),
                run_id=RunId("run.integration.concurrent"),
                task_id=TaskId("task.integration.concurrent"),
                sequence_no=1,
                event_type=RunEventType.TASK_STARTED,
                occurred_at=datetime(2026, 7, 20, tzinfo=UTC),
                idempotency_identity=StableId("effect.integration.concurrent.1"),
                payload_schema_version=SchemaVersion("0.1.0"),
                trace_id="trace-integration-concurrent",
                payload={"node": "concurrent"},
            )
            with ThreadPoolExecutor(max_workers=2) as executor:
                concurrent_events = tuple(
                    executor.map(events.append, (concurrent_event, concurrent_event))
                )
            assert concurrent_events == (concurrent_event, concurrent_event)
            with factory() as session:
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(RunEventRow)
                        .where(RunEventRow.run_id == concurrent_event.run_id.root)
                    )
                    == 1
                )

            concurrent_project = ProjectId("project.integration.concurrent")
            concurrent_genesis = commits.initialize_project(make_manifest(concurrent_project))
            concurrent_request = make_commit_request(
                concurrent_genesis,
                project_id=concurrent_project,
                idempotency_key="commit.integration.concurrent",
            )
            with ThreadPoolExecutor(max_workers=2) as executor:
                concurrent_results = tuple(
                    executor.map(commits.commit, (concurrent_request, concurrent_request))
                )
            assert concurrent_results[0] == concurrent_results[1]
            with factory() as session:
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(CommitReceiptRow)
                        .where(CommitReceiptRow.project_id == concurrent_project.root)
                    )
                    == 1
                )
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(CommitRow)
                        .where(CommitRow.project_id == concurrent_project.root)
                    )
                    == 2
                )
            synthetic_world = make_synthetic_bundle().world_roots[0]
            accepted_commit = concurrent_results[0].commit_id
            assert accepted_commit is not None
            assert (
                R1WorldRepository(factory).materialize(
                    concurrent_project, accepted_commit, synthetic_world
                )
                == 4
            )
            with factory() as session:
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(R1RecordRow)
                        .where(R1RecordRow.source_commit == accepted_commit.root)
                    )
                    == 4
                )
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(ProjectionOutboxRow)
                        .where(ProjectionOutboxRow.project_id == concurrent_project.root)
                    )
                    == 2
                )
            projection_service = DerivedProjectionService(
                ProjectionOutboxRepository(factory), ExactReplayProjectionBuilder()
            )
            assert projection_service.process_all() == 2
            with factory() as session:
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(DerivedSnapshotRow)
                        .where(DerivedSnapshotRow.project_id == concurrent_project.root)
                    )
                    == 2
                )

            project_id = ProjectId("project.integration")
            commits.initialize_project(make_manifest(project_id))
            chapter = artifacts.put(b"integration chapter", "text/plain", SCHEMA_VERSION)
            checkpoint_url = database_url.replace("postgresql+psycopg://", "postgresql://")
            telemetry = OpenTelemetryAdapter(TracerProvider().get_tracer("stage0-integration"))

            with PostgresSaver.from_conn_string(checkpoint_url) as first_saver:
                first_saver.setup()
                workflow = StageZeroWorkflow(
                    artifacts, commits, events, checkpoints, first_saver, telemetry
                )
                initial = workflow.initial_state(
                    project_id,
                    RunId("run.integration"),
                    TaskId("task.integration"),
                    chapter.model_dump(mode="json"),
                    trace_id="trace-integration",
                )
                interrupted = workflow.graph.invoke(
                    initial, {"configurable": {"thread_id": "thread-integration"}}
                )
                assert "__interrupt__" in interrupted

            with PostgresSaver.from_conn_string(checkpoint_url) as restarted_saver:
                restarted = StageZeroWorkflow(
                    artifacts, commits, events, checkpoints, restarted_saver, telemetry
                )
                completed = restarted.resume("thread-integration")
                assert completed["status"] == "completed"
                assert len(events.replay(RunId("run.integration"))) == 14
        finally:
            engine.dispose()


@contextmanager
def minio_service(
    infra: NativeInfra | None,
) -> Iterator[tuple[Minio, Callable[[], None], Callable[[], None]]]:
    if infra is not None:
        client = Minio(
            f"{LOOPBACK}:{infra.settings.minio_api_port}",
            access_key=infra.settings.minio_user,
            secret_key=infra.settings.minio_password,
            secure=False,
            http_client=PoolManager(timeout=Timeout(connect=2, read=2), retries=Retry(total=0)),
        )
        yield (
            client,
            lambda: infra.stop_service("minio"),
            lambda: infra.start_service("minio"),
        )
        return
    with (
        DockerContainer("quay.io/minio/minio:RELEASE.2025-06-13T11-33-47Z")
        .with_env("MINIO_ROOT_USER", "integration-user")
        .with_env("MINIO_ROOT_PASSWORD", "integration-password-change-me")
        .with_command('server /data --console-address ":9001"')
        .with_exposed_ports(9000)
    ) as container:
        client = Minio(
            f"127.0.0.1:{container.get_exposed_port(9000)}",
            access_key="integration-user",
            secret_key="integration-password-change-me",
            secure=False,
            http_client=PoolManager(timeout=Timeout(connect=2, read=2), retries=Retry(total=0)),
        )
        wrapped = container.get_wrapped_container()
        yield client, lambda: wrapped.stop(timeout=10), wrapped.start


def test_minio_adapter_round_trips_real_object(native_infra: NativeInfra | None) -> None:
    with minio_service(native_infra) as (client, stop, start):
        wait_until_ready(lambda: client.list_buckets() is not None)
        bucket = f"artifacts-{native_infra.settings.run_id}" if native_infra else "artifacts"
        store = MinioObjectStore(client, bucket)
        store.ensure_bucket()
        stored = store.put_if_absent("sha256/aa/object", b"content", "text/plain")
        assert stored.byte_length == 7
        assert store.get("sha256/aa/object") == b"content"

        stop()
        try:
            with pytest.raises(MaxRetryError):
                store.get("sha256/aa/object")
        finally:
            start()
        wait_until_ready(lambda: client.list_buckets() is not None)
        assert store.get("sha256/aa/object") == b"content"


@contextmanager
def opensearch_service(
    infra: NativeInfra | None,
) -> Iterator[tuple[OpenSearch, Callable[[], None], Callable[[], None]]]:
    if infra is not None:
        client = OpenSearch(
            hosts=[{"host": LOOPBACK, "port": infra.settings.opensearch_port}],
            use_ssl=False,
        )
        yield (
            client,
            lambda: infra.stop_service("opensearch"),
            lambda: infra.start_service("opensearch"),
        )
        return
    with (
        DockerContainer("opensearchproject/opensearch:3.7.0")
        .with_env("discovery.type", "single-node")
        .with_env("DISABLE_INSTALL_DEMO_CONFIG", "true")
        .with_env("DISABLE_SECURITY_PLUGIN", "true")
        .with_env("OPENSEARCH_JAVA_OPTS", "-Xms512m -Xmx512m")
        .with_exposed_ports(9200)
    ) as container:
        client = OpenSearch(
            hosts=[{"host": "127.0.0.1", "port": int(container.get_exposed_port(9200))}],
            use_ssl=False,
        )
        wrapped = container.get_wrapped_container()
        yield client, lambda: wrapped.stop(timeout=10), wrapped.start


def test_opensearch_adapter_indexes_and_searches_real_document(
    native_infra: NativeInfra | None,
) -> None:
    with opensearch_service(native_infra) as (client, stop, start):
        wait_until_ready(client.ping)
        adapter = OpenSearchIndex(client)
        index = f"evidence-{native_infra.settings.run_id}" if native_infra else "evidence"
        adapter.ensure_index(index, {"mappings": {"properties": {"text": {"type": "text"}}}})
        adapter.index_document(index, "doc-1", {"text": "long range evidence"})
        hits = adapter.search(index, {"match": {"text": "evidence"}}, size=10)
        assert hits[0]["_id"] == "doc-1"

        bundle = make_synthetic_bundle()
        case = bundle.case_manifests[0]
        world = bundle.world_roots[0]
        history = next(root for root in bundle.text_roots if len(root.chapters) == 20)
        plan = bundle.plan_roots[0]
        run_suffix = native_infra.settings.run_id if native_infra else "docker"
        snapshot_id = StableId(f"snapshot.integration.{run_suffix}")
        units = AnchorBuilder().build(world, history, plan, snapshot_id=snapshot_id)
        embedder = DeterministicHashEmbedder(dimension=8)
        stage1_indexer = Stage1SearchIndexer(adapter, embedder)
        anchor_index, grounded_index = stage1_indexer.build_and_publish(
            case.project_id, world.source_commit, snapshot_id, units
        )
        assert anchor_index != grounded_index
        backend = Stage1OpenSearchBackend(
            adapter,
            embedder,
            project_id=case.project_id,
            source_commit=world.source_commit,
            snapshot_id=snapshot_id,
        )
        semantic_need = next(
            need
            for need in Stage1NeedGenerator().generate(world, case)
            if need.query_intent.value == "related_event"
        )
        assert backend.search(semantic_need, RetrievalChannel.ANCHOR_BM25, 5)
        assert backend.search(semantic_need, RetrievalChannel.ANCHOR_DENSE, 5)

        stop()
        try:
            assert client.ping() is False
        finally:
            start()
        wait_until_ready(client.ping)
        recovered_hits = adapter.search(index, {"match": {"text": "evidence"}}, size=10)
        assert recovered_hits[0]["_id"] == "doc-1"


def test_full_outbox_projection_crosses_minio_postgres_and_opensearch(
    native_infra: NativeInfra | None,
) -> None:
    with (
        postgres_service(native_infra) as (database_url, _, _),
        minio_service(native_infra) as (minio_client, _, _),
        opensearch_service(native_infra) as (search_client, _, _),
    ):
        alembic = Config(str(REPOSITORY_ROOT / "alembic.ini"))
        alembic.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(alembic, "head")
        engine = build_engine(database_url)
        try:
            factory = build_session_factory(engine)
            suffix = native_infra.settings.run_id if native_infra else "docker"
            project_id = ProjectId(f"project.full-projection.{suffix}")
            bucket = f"projection-{suffix}"
            object_store = MinioObjectStore(minio_client, bucket)
            object_store.ensure_bucket()
            artifacts = ArtifactRepository(object_store)
            bundle = make_synthetic_bundle()
            history = next(root for root in bundle.text_roots if len(root.chapters) == 20)
            plan = bundle.plan_roots[0]
            world = bundle.world_roots[0]
            text_artifact = artifacts.put(
                canonical_json_bytes(history.model_dump(mode="json")),
                "application/vnd.novel-agent.text-root+json",
                history.schema_version,
            )
            plan_artifact = artifacts.put(
                canonical_json_bytes(plan.model_dump(mode="json")),
                "application/vnd.novel-agent.plan-root+json",
                plan.schema_version,
            )
            world_artifact = artifacts.put(
                canonical_json_bytes(world.model_dump(mode="json")),
                "application/vnd.novel-agent.world-root+json",
                world.schema_version,
            )
            manifest = make_manifest(project_id).model_copy(
                update={
                    "text_root": TextRootRef(
                        artifact_id=text_artifact.artifact_id,
                        media_type=text_artifact.media_type,
                        byte_length=text_artifact.byte_length,
                        schema_version=text_artifact.schema_version,
                    ),
                    "plan_root": PlanRootRef(
                        artifact_id=plan_artifact.artifact_id,
                        media_type=plan_artifact.media_type,
                        byte_length=plan_artifact.byte_length,
                        schema_version=plan_artifact.schema_version,
                    ),
                    "world_root": WorldRootRef(
                        artifact_id=world_artifact.artifact_id,
                        media_type=world_artifact.media_type,
                        byte_length=world_artifact.byte_length,
                        schema_version=world_artifact.schema_version,
                    ),
                }
            )
            commits = CommitService(factory)
            commit_id = commits.initialize_project(manifest)
            search_index = OpenSearchIndex(search_client)
            search_indexer = Stage1SearchIndexer(
                search_index, DeterministicHashEmbedder(dimension=8)
            )
            full_builder = FullDerivedProjectionBuilder(
                ArtifactProjectionSourceLoader(commits, artifacts),
                R1WorldRepository(factory),
                search_indexer,
            )
            worker = DerivedProjectionService(
                ProjectionOutboxRepository(factory),
                full_builder,
                project_id=project_id,
                worker_id="integration.full-projection",
            )

            assert worker.process_all() == 1
            snapshot = DerivedSnapshotRepository(factory).get_for_commit(commit_id)
            assert snapshot is not None and snapshot.source_commit == commit_id
            assert snapshot.embedding_profile == "deterministic-hash-test-only-8d"
            with factory() as session:
                record_counts: dict[str, int] = {
                    row[0]: row[1]
                    for row in session.execute(
                        select(R1RecordRow.access_scope, func.count())
                        .where(R1RecordRow.source_commit == commit_id.root)
                        .group_by(R1RecordRow.access_scope)
                    ).all()
                }
                assert record_counts == {"author_planning": 4, "writer_safe": 4}
            backend = Stage1OpenSearchBackend(
                search_index,
                DeterministicHashEmbedder(dimension=8),
                project_id=project_id,
                source_commit=commit_id,
                snapshot_id=snapshot.snapshot_id,
            )
            semantic_need = next(
                need
                for need in Stage1NeedGenerator().generate(world, bundle.case_manifests[0])
                if need.query_intent.value == "related_event"
            ).model_copy(update={"base_commit": commit_id})
            assert backend.search(semantic_need, RetrievalChannel.ANCHOR_BM25, 5)
        finally:
            engine.dispose()
