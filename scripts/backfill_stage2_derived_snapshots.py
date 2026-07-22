#!/usr/bin/env python3
"""Rebuild exact Stage 2R derived snapshots from an existing Canonical commit chain."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import ParseResult, urlparse
from uuid import uuid4

from opensearchpy import OpenSearch

try:
    from scripts.native_models import assert_model_service, load_model_lock
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from native_models import (  # type: ignore[import-not-found,no-redef]
        assert_model_service,
        load_model_lock,
    )
from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import HttpEmbeddingProvider, RetrievalModelRoute
from novel_agent.adapters.opensearch import OpenSearchIndex
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.adapters.postgres.models import CommitRow, ProjectRow
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, RunId, TaskId
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRole
from novel_agent.domain.retrieval_routing import RetrievalBackendProfile
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.embedding_cache import SqlEmbeddingCache
from novel_agent.services.projection import (
    ArtifactProjectionSourceLoader,
    DerivedSnapshotRepository,
    FullDerivedProjectionBuilder,
)
from novel_agent.services.r1 import R1WorldRepository
from novel_agent.services.search_retrieval import Stage2RSearchIndexer


class Stage2RBackfillError(RuntimeError):
    """The existing project cannot safely be rebuilt as real hybrid retrieval."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-directory", required=True, type=Path)
    parser.add_argument("--project-id")
    parser.add_argument("--database-url")
    parser.add_argument(
        "--opensearch-url",
        default=f"http://127.0.0.1:{os.getenv('OPENSEARCH_PORT', '9200')}",
    )
    parser.add_argument(
        "--embedding-url",
        default=(
            "http://127.0.0.1:"
            f"{os.getenv('NOVEL_AGENT_EMBEDDING_MODEL_PORT', '8081')}/v1/embeddings"
        ),
    )
    parser.add_argument(
        "--retrieval-backend",
        choices=(RetrievalBackendProfile.REAL_HYBRID.value,),
        default=RetrievalBackendProfile.REAL_HYBRID.value,
    )
    parser.add_argument("--build-profile", default="stage2r-hybrid-v0.1")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-commits", type=int)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.max_commits is not None and args.max_commits < 1:
        raise ValueError("--max-commits must be positive")
    project_directory = args.project_directory.resolve()
    object_directory = project_directory / "objects"
    database_url = (
        args.database_url or f"sqlite:///{(project_directory / 'project.sqlite3').resolve()}"
    )
    if not object_directory.is_dir():
        raise Stage2RBackfillError(f"missing project artifact directory: {object_directory}")
    search_target = _loopback_url(args.opensearch_url, "OpenSearch")
    embedding_target = _loopback_url(args.embedding_url, "embedding")
    engine = build_engine(database_url)
    search_client: OpenSearch | None = None
    try:
        _assert_stage2r_schema(engine)
        factory = build_session_factory(engine)
        project_id = _resolve_project(factory, args.project_id)
        lock = load_model_lock()
        embedding_model = lock.models["embedding"]
        reranker_model = lock.models["reranker"]
        assert_model_service(embedding_model)
        assert_model_service(reranker_model)
        run_id = RunId(f"run.stage2r-backfill.{uuid4().hex}")
        embedder = HttpEmbeddingProvider(
            RetrievalModelRoute(
                endpoint=embedding_target.geturl(),
                model=embedding_model.model_id,
                revision=embedding_model.revision,
                runtime_fingerprint=embedding_model.runtime_fingerprint,
                run_id=run_id,
                task_id=TaskId(f"task.stage2r-backfill.{project_id.root}"),
                trace_id=f"trace.{run_id.root}",
                span_id=None,
                model_role=ModelRole.BATCH_TEST,
                purpose=ModelCallPurpose.EVALUATION,
                timeout_seconds=300,
            ),
            dimension=embedding_model.dimension or 0,
            batch_size=32,
        )
        search_client = OpenSearch(
            hosts=[
                {
                    "host": search_target.hostname,
                    "port": search_target.port,
                }
            ],
            use_ssl=search_target.scheme == "https",
            verify_certs=search_target.scheme == "https",
        )
        if not search_client.ping():
            raise Stage2RBackfillError("OpenSearch is unavailable")
        artifacts = ArtifactRepository(FilesystemObjectStore(object_directory))
        builder = FullDerivedProjectionBuilder(
            ArtifactProjectionSourceLoader(CommitService(factory), artifacts),
            R1WorldRepository(factory),
            Stage2RSearchIndexer(
                OpenSearchIndex(search_client),
                embedder,
                embedding_cache=SqlEmbeddingCache(factory),
            ),
            retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
            build_profile=args.build_profile,
            embedding_model=embedding_model.model_id,
            embedding_revision=embedding_model.revision,
            embedding_runtime_fingerprint=ArtifactId(
                f"sha256:{embedding_model.runtime_fingerprint}"
            ),
            reranker_model=reranker_model.model_id,
            reranker_revision=reranker_model.revision,
        )
        snapshots = DerivedSnapshotRepository(factory)
        commits = _project_commits(factory, project_id, args.max_commits)
        rebuilt: list[str] = []
        skipped: list[str] = []
        superseded: list[str] = []
        for source_commit in commits:
            current = snapshots.get_attestation_for_commit(source_commit)
            if args.resume and current is not None and current.quality_eligible:
                skipped.append(source_commit.root)
                continue
            snapshot = builder.build(project_id, source_commit)
            attestation = snapshot.projection_attestation
            if attestation is None:
                raise Stage2RBackfillError("real-hybrid projection produced no attestation")
            if snapshots.publish_rebuilt(project_id, snapshot):
                superseded.append(source_commit.root)
            rebuilt.append(source_commit.root)
        report = {
            "status": "stage2r_backfill_completed",
            "project_id": project_id.root,
            "retrieval_backend_profile": RetrievalBackendProfile.REAL_HYBRID.value,
            "build_profile": args.build_profile,
            "database_url": _safe_database_descriptor(database_url),
            "opensearch_url": search_target.geturl(),
            "completed_at": datetime.now(UTC).isoformat(),
            "rebuilt_commits": rebuilt,
            "superseded_metadata_only_commits": superseded,
            "skipped_exact_commits": skipped,
        }
        _write_report(project_directory / "stage2r_backfill_report.json", report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        if search_client is not None:
            search_client.close()
        engine.dispose()


def _assert_stage2r_schema(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    required_tables = {
        "project",
        "project_commit",
        "derived_snapshot",
        "r1_record",
        "embedding_cache",
    }
    missing = sorted(required_tables - tables)
    if missing:
        raise Stage2RBackfillError(f"database migration is incomplete; missing tables: {missing}")
    r1_columns = {column["name"] for column in inspector.get_columns("r1_record")}
    required_columns = {"worldline", "narrative_start", "narrative_end", "access_scope"}
    missing_columns = sorted(required_columns - r1_columns)
    if missing_columns:
        raise Stage2RBackfillError(
            f"database migration is incomplete; missing r1_record columns: {missing_columns}"
        )


def _safe_database_descriptor(database_url: str) -> str:
    parsed = urlparse(database_url)
    if parsed.scheme.startswith("postgresql+"):
        return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}{parsed.path}"
    return database_url


def _resolve_project(factory: object, requested: str | None) -> ProjectId:
    with factory() as session:  # type: ignore[operator]
        project_ids = tuple(
            session.scalars(select(ProjectRow.project_id).order_by(ProjectRow.project_id))
        )
    if requested is not None:
        project_id = ProjectId(requested)
        if project_id.root not in project_ids:
            raise Stage2RBackfillError(f"project is not present in database: {project_id.root}")
        return project_id
    if len(project_ids) != 1:
        raise Stage2RBackfillError(
            "--project-id is required when the database has multiple projects"
        )
    return ProjectId(project_ids[0])


def _project_commits(
    factory: object,
    project_id: ProjectId,
    maximum: int | None,
) -> tuple[CommitId, ...]:
    with factory() as session:  # type: ignore[operator]
        statement = (
            select(CommitRow.commit_id)
            .where(CommitRow.project_id == project_id.root)
            .order_by(CommitRow.created_at, CommitRow.commit_id)
        )
        if maximum is not None:
            statement = statement.limit(maximum)
        values = tuple(session.scalars(statement))
    if not values:
        raise Stage2RBackfillError("project has no commits to backfill")
    return tuple(CommitId(value) for value in values)


def _loopback_url(value: str, label: str) -> ParseResult:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
        or parsed.username is not None
        or parsed.query
        or parsed.fragment
    ):
        raise Stage2RBackfillError(f"{label} endpoint must be a loopback HTTP(S) URL")
    return parsed


def _write_report(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", "utf-8"
    )


if __name__ == "__main__":
    raise SystemExit(main())
