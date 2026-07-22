#!/usr/bin/env python3
"""Run a validated Stage 1 BenchmarkBundle without exposing private future text."""

from __future__ import annotations

import argparse
import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import ParseResult, quote_plus, urlparse
from uuid import uuid4

from opensearchpy import OpenSearch
from pydantic import JsonValue
from scripts.native_models import assert_model_service, load_model_lock
from sqlalchemy import inspect

from novel_agent.adapters.model import (
    HttpEmbeddingProvider,
    HttpPassageReranker,
    RetrievalModelRoute,
)
from novel_agent.adapters.opensearch import OpenSearchIndex
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.domain.artifacts import (
    PlanRootRef,
    ProjectProfileRootRef,
    ReferenceRootRef,
    RootManifest,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.benchmark import (
    BenchmarkBundle,
    BenchmarkTrack,
    PlanRootDocument,
    Stage1BenchmarkResult,
    TextRootDocument,
)
from novel_agent.domain.ids import ArtifactId, ProjectId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.memory import RetrievalChannel, WorldRootDocument
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelRole,
    RetrievalInferenceCallRecord,
    RetrievalInferenceStatus,
)
from novel_agent.domain.runtime import RunEvent, RunEventType
from novel_agent.services.benchmark_importer import BenchmarkBundleImporter
from novel_agent.services.benchmark_workspace import BenchmarkWorkspaceRepository
from novel_agent.services.evaluation import EvaluationLedgerRepository
from novel_agent.services.event_log import RunEventLogRepository
from novel_agent.services.memory_pipeline import AnchorBuilder
from novel_agent.services.r1 import R1RetrievalBackend, R1WorldRepository
from novel_agent.services.retrieval import RetrievalBackend
from novel_agent.services.search_retrieval import (
    CompositeRetrievalBackend,
    Stage1OpenSearchBackend,
    Stage1SearchIndexer,
)
from novel_agent.services.stage1_benchmark import Stage1BenchmarkRunner
from novel_agent.services.stage1_evaluation import build_stage1_evaluation_records

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
REQUIRED_R1_TABLES = frozenset(
    {"r1_record", "r1_record_entity", "run_stream", "run_event", "evaluation_entry"}
)


class _BenchmarkRunEventSink:
    def __init__(
        self,
        run_id: RunId,
        trace_id: str,
        schema_version: SchemaVersion,
        started_at: datetime,
    ) -> None:
        self.run_id = run_id
        self.trace_id = trace_id
        self._schema_version = schema_version
        self._started_at = started_at
        self._repository: RunEventLogRepository | None = None
        self._sequence = 0

    def bind(self, repository: RunEventLogRepository) -> None:
        if self._repository is not None:
            raise RuntimeError("benchmark event sink is already bound")
        self._repository = repository
        self._append(
            event_type=RunEventType.RUN_CREATED,
            occurred_at=self._started_at,
            task_id=None,
            identity=StableId(f"effect.{self.run_id.root}.created"),
            payload={"runner": "stage1-native-bge", "model_role": ModelRole.BATCH_TEST.value},
        )

    def __call__(self, record: RetrievalInferenceCallRecord) -> None:
        if record.run_id != self.run_id or record.trace_id != self.trace_id:
            raise RuntimeError("retrieval call does not belong to the benchmark event sink")
        event_type = (
            RunEventType.MODEL_COMPLETED
            if record.status is RetrievalInferenceStatus.SUCCEEDED
            else RunEventType.MODEL_FAILED
        )
        self._append(
            event_type=event_type,
            occurred_at=record.completed_at,
            task_id=record.task_id,
            identity=record.call_id,
            payload={
                "operation": record.operation.value,
                "status": record.status.value,
                "usage": record.usage.model_dump(mode="json"),
                "error_type": record.error_type,
            },
            span_id=record.span_id,
            model_call_record=record,
        )

    def complete(self, result: Stage1BenchmarkResult) -> None:
        self._append(
            event_type=RunEventType.RUN_COMPLETED,
            occurred_at=datetime.now(UTC),
            task_id=None,
            identity=StableId(f"effect.{self.run_id.root}.completed"),
            payload={
                "case_id": result.case_id.root,
                "snapshot_id": result.snapshot_id.root,
                "profile_count": len(result.profile_results),
                "retrieval_model_call_count": len(result.retrieval_model_calls),
            },
        )

    def fail(self, error: Exception) -> None:
        if self._repository is None:
            return
        self._append(
            event_type=RunEventType.RUN_FAILED,
            occurred_at=datetime.now(UTC),
            task_id=None,
            identity=StableId(f"effect.{self.run_id.root}.failed"),
            payload={"error_type": type(error).__name__},
        )

    def _append(
        self,
        *,
        event_type: RunEventType,
        occurred_at: datetime,
        task_id: TaskId | None,
        identity: StableId,
        payload: dict[str, JsonValue],
        span_id: str | None = None,
        model_call_record: RetrievalInferenceCallRecord | None = None,
    ) -> None:
        if self._repository is None:
            raise RuntimeError("benchmark event sink is not bound")
        self._sequence += 1
        event = RunEvent(
            event_id=StableId(f"event.{self.run_id.root}.{self._sequence}"),
            run_id=self.run_id,
            task_id=task_id,
            sequence_no=self._sequence,
            event_type=event_type,
            occurred_at=occurred_at,
            idempotency_identity=identity,
            payload_schema_version=self._schema_version,
            trace_id=self.trace_id,
            span_id=span_id,
            payload=payload,
            model_call_record=model_call_record,
        )
        self._repository.append(event)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--case-id", required=True)
    parser.add_argument(
        "--track",
        choices=(BenchmarkTrack.ORACLE.value,),
        default=BenchmarkTrack.ORACLE.value,
        help="End-to-end runs require an application-supplied memory constructor.",
    )
    parser.add_argument("--token-budget", type=int, default=4000)
    parser.add_argument(
        "--retrieval-backend",
        choices=("in-memory", "native-bge"),
        default="in-memory",
    )
    parser.add_argument("--database-url")
    parser.add_argument(
        "--opensearch-url",
        default=f"http://127.0.0.1:{os.getenv('OPENSEARCH_PORT', '9200')}",
    )
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    bundle = BenchmarkBundleImporter().load(args.bundle)
    case_id = StableId(args.case_id)
    track = BenchmarkTrack(args.track)
    if args.retrieval_backend == "native-bge":
        result = run_native_bge(bundle, case_id, track, args)
    else:
        result = Stage1BenchmarkRunner(token_budget=args.token_budget).run(
            bundle,
            case_id,
            track,
        )
    payload = result.model_dump_json(indent=2) + "\n"
    if args.output is None:
        print(payload, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    return 0


def run_native_bge(
    bundle: BenchmarkBundle,
    case_id: StableId,
    track: BenchmarkTrack,
    args: argparse.Namespace,
) -> Stage1BenchmarkResult:
    if track is not BenchmarkTrack.ORACLE:
        raise ValueError("native BGE runner currently requires the Oracle track")
    case = next((item for item in bundle.case_manifests if item.case_id == case_id), None)
    if case is None:
        raise ValueError(f"benchmark case does not exist: {case_id.root}")
    if case.input_world_root_verified is None:
        raise ValueError("native BGE Oracle run requires a verified WorldRoot")
    history = next(root for root in bundle.text_roots if root.root_hash == case.input_text_root)
    world = next(
        root for root in bundle.world_roots if root.root_hash == case.input_world_root_verified
    )
    plan = next(
        (root for root in bundle.plan_roots if root.root_hash == case.input_plan_root), None
    )

    model_lock = load_model_lock()
    embedding_model = model_lock.models["embedding"]
    reranker_model = model_lock.models["reranker"]
    assert_model_service(embedding_model)
    assert_model_service(reranker_model)
    embedding_port = int(os.getenv("NOVEL_AGENT_EMBEDDING_MODEL_PORT", "8081"))
    reranker_port = int(os.getenv("NOVEL_AGENT_RERANKER_MODEL_PORT", "8082"))
    run_started_at = datetime.now(UTC)
    run_id = RunId(f"run.benchmark.{uuid4().hex}")
    trace_id = f"trace.{run_id.root}"
    event_sink = _BenchmarkRunEventSink(
        run_id,
        trace_id,
        bundle.bundle_schema_version,
        run_started_at,
    )
    embedder = HttpEmbeddingProvider(
        RetrievalModelRoute(
            endpoint=f"http://127.0.0.1:{embedding_port}/v1/embeddings",
            model=embedding_model.model_id,
            revision=embedding_model.revision,
            runtime_fingerprint=embedding_model.runtime_fingerprint,
            run_id=run_id,
            task_id=TaskId(f"task.benchmark.{case.case_id.root}.embedding"),
            trace_id=trace_id,
            span_id=None,
            model_role=ModelRole.BATCH_TEST,
            purpose=ModelCallPurpose.EVALUATION,
            timeout_seconds=300,
        ),
        dimension=embedding_model.dimension or 0,
        batch_size=32,
        audit_sink=event_sink,
    )
    reranker = HttpPassageReranker(
        RetrievalModelRoute(
            endpoint=f"http://127.0.0.1:{reranker_port}/rerank",
            model=reranker_model.model_id,
            revision=reranker_model.revision,
            runtime_fingerprint=reranker_model.runtime_fingerprint,
            run_id=run_id,
            task_id=TaskId(f"task.benchmark.{case.case_id.root}.reranker"),
            trace_id=trace_id,
            span_id=None,
            model_role=ModelRole.BATCH_TEST,
            purpose=ModelCallPurpose.EVALUATION,
            timeout_seconds=300,
        ),
        audit_sink=event_sink,
    )

    database_url = args.database_url or _database_url_from_environment()
    _validate_database_url(database_url)
    search_target = _validate_opensearch_url(args.opensearch_url)
    engine = build_engine(database_url)
    search_client = OpenSearch(
        hosts=[
            {
                "host": cast(str, search_target.hostname),
                "port": cast(int, search_target.port),
            }
        ],
        use_ssl=search_target.scheme == "https",
        verify_certs=search_target.scheme == "https",
    )
    try:
        missing_tables = REQUIRED_R1_TABLES - set(inspect(engine).get_table_names())
        if missing_tables:
            raise RuntimeError(
                f"database migration is incomplete; missing tables: {sorted(missing_tables)}"
            )
        session_factory = build_session_factory(engine)
        event_sink.bind(RunEventLogRepository(session_factory))
        if not search_client.ping():
            raise RuntimeError("OpenSearch is unavailable")
        fingerprint = hashlib.sha256(
            (
                f"{bundle.content_hash.root}\0{case.case_id.root}\0{world.source_commit.root}\0"
                f"{embedder.profile}\0{reranker.profile}"
            ).encode()
        ).hexdigest()
        snapshot_id = StableId(f"snapshot.bge.{fingerprint}")
        units = AnchorBuilder().build(world, history, plan, snapshot_id=snapshot_id)
        workspace_project_id = _benchmark_workspace_project_id(bundle, case_id, world.root_hash)
        relational_project_id = BenchmarkWorkspaceRepository(session_factory).ensure_imported_basis(
            workspace_project_id,
            world.source_commit,
            _benchmark_root_manifest(
                workspace_project_id,
                bundle.bundle_schema_version,
                history,
                world,
                plan,
            ),
        )
        r1_repository = R1WorldRepository(session_factory)
        r1_repository.materialize(relational_project_id, world.source_commit, world)
        search_index = OpenSearchIndex(search_client)
        Stage1SearchIndexer(search_index, embedder).build_and_publish(
            case.project_id,
            world.source_commit,
            snapshot_id,
            units,
        )
        r1_backend = R1RetrievalBackend(r1_repository, snapshot_id=snapshot_id)
        search_backend = Stage1OpenSearchBackend(
            search_index,
            embedder,
            project_id=case.project_id,
            source_commit=world.source_commit,
            snapshot_id=snapshot_id,
        )
        routes: dict[RetrievalChannel, RetrievalBackend] = {
            RetrievalChannel.R1_EXACT: r1_backend,
            RetrievalChannel.R1_TEMPORAL: r1_backend,
            RetrievalChannel.TYPED_GRAPH: r1_backend,
            RetrievalChannel.ANCHOR_BM25: search_backend,
            RetrievalChannel.ANCHOR_DENSE: search_backend,
            RetrievalChannel.GROUNDED_BM25: search_backend,
            RetrievalChannel.GROUNDED_DENSE: search_backend,
            RetrievalChannel.HIERARCHY: search_backend,
        }
        result = Stage1BenchmarkRunner(token_budget=args.token_budget).run(
            bundle,
            case_id,
            track,
            retrieval_backend=CompositeRetrievalBackend(routes),
            retrieval_snapshot_id=snapshot_id,
            embedding_profile=embedder.profile,
            reranker=reranker,
        )
        audited_result = result.model_copy(
            update={
                "run_id": run_id,
                "retrieval_model_calls": (*embedder.call_records, *reranker.call_records),
            }
        )
        evaluation_config, evaluation_entries = build_stage1_evaluation_records(
            audited_result,
            bundle.content_hash,
            created_at=datetime.now(UTC),
        )
        evaluation_ledger = EvaluationLedgerRepository(session_factory)
        for entry in evaluation_entries:
            evaluation_ledger.append(evaluation_config, entry)
        event_sink.complete(audited_result)
        return audited_result
    except Exception as error:
        try:
            event_sink.fail(error)
        except Exception as audit_error:
            raise RuntimeError(
                "benchmark failure could not be persisted to RunEventLog"
            ) from audit_error
        raise
    finally:
        search_client.close()
        engine.dispose()


def _database_url_from_environment() -> str:
    required = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_PORT", "POSTGRES_DB")
    missing = tuple(name for name in required if not os.getenv(name))
    if missing:
        raise ValueError(f"native benchmark database environment is incomplete: {missing}")
    user = quote_plus(os.environ["POSTGRES_USER"])
    password = quote_plus(os.environ["POSTGRES_PASSWORD"])
    port = int(os.environ["POSTGRES_PORT"])
    database = quote_plus(os.environ["POSTGRES_DB"])
    return f"postgresql+psycopg://{user}:{password}@127.0.0.1:{port}/{database}"


def _validate_database_url(value: str) -> None:
    parsed = urlparse(value)
    if not parsed.scheme.startswith("postgresql+") or parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError("native benchmark database must use a loopback PostgreSQL URL")


def _validate_opensearch_url(value: str) -> ParseResult:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in LOOPBACK_HOSTS
        or parsed.port is None
        or parsed.path not in {"", "/"}
        or parsed.username is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("native benchmark OpenSearch URL must be a bare loopback HTTP(S) origin")
    return parsed


def _benchmark_workspace_project_id(
    bundle: BenchmarkBundle,
    case_id: StableId,
    world_root: ArtifactId,
) -> ProjectId:
    digest = hashlib.sha256(
        f"{bundle.bundle_id.root}\0{case_id.root}\0{world_root.root}".encode()
    ).hexdigest()
    return ProjectId(f"project.benchmark.{digest[:48]}")


def _placeholder_artifact(label: str, *identities: str) -> ArtifactId:
    payload = "\0".join(("benchmark-placeholder", label, *identities)).encode()
    return ArtifactId(f"sha256:{hashlib.sha256(payload).hexdigest()}")


def _document_size(document: TextRootDocument | WorldRootDocument | PlanRootDocument) -> int:
    return len(document.model_dump_json().encode("utf-8"))


def _benchmark_root_manifest(
    project_id: ProjectId,
    schema_version: SchemaVersion,
    history: TextRootDocument,
    world: WorldRootDocument,
    plan: PlanRootDocument | None,
) -> RootManifest:
    plan_hash = (
        plan.root_hash
        if plan is not None
        else _placeholder_artifact("absent-plan", history.root_hash.root, world.root_hash.root)
    )
    return RootManifest(
        project_id=project_id,
        schema_version=schema_version,
        text_root=TextRootRef(
            artifact_id=history.root_hash,
            media_type="application/json",
            byte_length=_document_size(history),
            schema_version=schema_version,
        ),
        plan_root=PlanRootRef(
            artifact_id=plan_hash,
            media_type="application/json",
            byte_length=_document_size(plan) if plan is not None else 0,
            schema_version=schema_version,
        ),
        world_root=WorldRootRef(
            artifact_id=world.root_hash,
            media_type="application/json",
            byte_length=_document_size(world),
            schema_version=schema_version,
        ),
        reference_root=ReferenceRootRef(
            artifact_id=_placeholder_artifact(
                "absent-reference", history.root_hash.root, world.root_hash.root
            ),
            media_type="application/json",
            byte_length=0,
            schema_version=schema_version,
        ),
        project_profile_root=ProjectProfileRootRef(
            artifact_id=_placeholder_artifact(
                "absent-project-profile", history.root_hash.root, world.root_hash.root
            ),
            media_type="application/json",
            byte_length=0,
            schema_version=schema_version,
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
