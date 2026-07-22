#!/usr/bin/env python3
"""Run Stage 2 Genesis plus teacher-forced chapter replay on the human benchmark."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from opensearchpy import OpenSearch

try:
    from scripts.native_models import assert_model_service, load_model_lock
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from native_models import assert_model_service, load_model_lock  # type: ignore[no-redef]

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import (
    HttpEmbeddingProvider,
    HttpPassageReranker,
    OpenAICompatibleChatEndpoint,
    RetrievalModelRoute,
)
from novel_agent.adapters.opensearch import OpenSearchIndex
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.domain.ids import ArtifactId, RunId, TaskId
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRole
from novel_agent.domain.retrieval_routing import RetrievalBackendProfile
from novel_agent.domain.stage2 import BenchmarkInformationProfile
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.embedding_cache import SqlEmbeddingCache
from novel_agent.services.human_benchmark_compiler import HumanBenchmarkCompiler
from novel_agent.services.projection import (
    ArtifactProjectionSourceLoader,
    DerivedSnapshotRepository,
    FullDerivedProjectionBuilder,
)
from novel_agent.services.r1 import R1WorldRepository
from novel_agent.services.search_retrieval import Stage2RSearchIndexer
from novel_agent.services.stage2_retrieval_backend import RealHybridProjectionGateway
from novel_agent.services.teacher_forced_benchmark_e2e import (
    TeacherForcedBenchmarkE2ERunner,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--source", type=Path, required=True)
    value.add_argument("--output-directory", type=Path, required=True)
    value.add_argument(
        "--resume-project",
        type=Path,
        help=(
            "existing Canonical project directory whose objects, commit chain, "
            "and snapshots are reused"
        ),
    )
    value.add_argument(
        "--database-url",
        help="required loopback PostgreSQL URL for formal real_hybrid retrieval",
    )
    value.add_argument(
        "--opensearch-url",
        default=f"http://127.0.0.1:{os.getenv('OPENSEARCH_PORT', '9200')}",
    )
    value.add_argument(
        "--embedding-url",
        default=(
            "http://127.0.0.1:"
            f"{os.getenv('NOVEL_AGENT_EMBEDDING_MODEL_PORT', '8081')}/v1/embeddings"
        ),
    )
    value.add_argument(
        "--reranker-url",
        default=(f"http://127.0.0.1:{os.getenv('NOVEL_AGENT_RERANKER_MODEL_PORT', '8082')}/rerank"),
    )
    value.add_argument(
        "--information-profile",
        choices=tuple(item.value for item in BenchmarkInformationProfile),
        default=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED.value,
    )
    value.add_argument("--token-budget", type=int, default=4000)
    value.add_argument("--max-candidates", type=int, default=20)
    value.add_argument(
        "--semantic-backend",
        choices=("local_openai", "scripted"),
        default="local_openai",
    )
    value.add_argument("--model-base-url", default="http://127.0.0.1:8002/v1")
    value.add_argument("--model", default="qwen36-27b-nvfp4")
    value.add_argument("--model-max-output-tokens", type=int, default=8192)
    value.add_argument("--model-max-retries", type=int, default=0)
    value.add_argument(
        "--retrieval-backend",
        choices=tuple(item.value for item in RetrievalBackendProfile),
        default=RetrievalBackendProfile.REAL_HYBRID.value,
        help="real_hybrid is the only formal benchmark mode; scripted_smoke is contract-test only",
    )
    value.add_argument("--stop-after-genesis", action="store_true")
    value.add_argument("--max-chapter", type=int, default=None)
    value.add_argument("--resume", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    bundle = HumanBenchmarkCompiler().compile(args.source)
    project_directory = (args.resume_project or args.output_directory).resolve()
    retrieval_profile = RetrievalBackendProfile(args.retrieval_backend)
    endpoint = (
        OpenAICompatibleChatEndpoint(
            base_url=args.model_base_url,
            model=args.model,
            max_output_tokens=args.model_max_output_tokens,
            max_retries=args.model_max_retries,
        )
        if args.semantic_backend == "local_openai"
        else None
    )
    provider_engine = None
    search_client = None
    try:
        real_hybrid_provider = None
        if retrieval_profile is RetrievalBackendProfile.REAL_HYBRID:
            if args.database_url is None:
                raise ValueError("--database-url is required for real_hybrid execution")
            provider_engine, search_client, gateway = _real_hybrid_gateway(
                args,
                project_directory,
            )
            real_hybrid_provider = gateway.backend_for
        summary = TeacherForcedBenchmarkE2ERunner(
            token_budget=args.token_budget,
            max_candidates=args.max_candidates,
            semantic_endpoint=endpoint,
            retrieval_backend_profile=retrieval_profile,
            real_hybrid_backend_provider=real_hybrid_provider,
            database_url=args.database_url,
        ).run(
            args.source,
            args.output_directory,
            bundle,
            information_profile=BenchmarkInformationProfile(args.information_profile),
            stop_after_genesis=args.stop_after_genesis,
            max_chapter=args.max_chapter,
            resume=args.resume or args.resume_project is not None,
            project_directory=project_directory,
        )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        if search_client is not None:
            search_client.close()
        if provider_engine is not None:
            provider_engine.dispose()
        if endpoint is not None:
            import asyncio

            asyncio.run(endpoint.aclose())


def _real_hybrid_gateway(
    args: argparse.Namespace,
    project_directory: Path,
) -> tuple[object, OpenSearch, RealHybridProjectionGateway]:
    """Build the exact commit-scoped gateway used by both paired arms.

    This validates all native retrieval dependencies up front.  It does not
    call an embedding service or replay any benchmark chapter until the
    runner freezes a checkpoint and asks the gateway for that commit.
    """

    database_url = _loopback_postgres_url(args.database_url)
    search_target = _loopback_http_url(args.opensearch_url, "OpenSearch")
    embedding_target = _loopback_http_url(args.embedding_url, "embedding")
    reranker_target = _loopback_http_url(args.reranker_url, "reranker")
    if not (project_directory / "objects").is_dir():
        raise ValueError(f"missing project artifact directory: {project_directory / 'objects'}")
    model_lock = load_model_lock()
    embedding_model = model_lock.models["embedding"]
    reranker_model = model_lock.models["reranker"]
    assert_model_service(embedding_model)
    assert_model_service(reranker_model)
    run_id = RunId(f"run.stage2r-teacher-forced.{uuid4().hex}")
    embedder = HttpEmbeddingProvider(
        RetrievalModelRoute(
            endpoint=embedding_target.geturl(),
            model=embedding_model.model_id,
            revision=embedding_model.revision,
            runtime_fingerprint=embedding_model.runtime_fingerprint,
            run_id=run_id,
            task_id=TaskId("task.stage2r-teacher-forced.embedding"),
            trace_id=f"trace.{run_id.root}",
            span_id=None,
            model_role=ModelRole.BATCH_TEST,
            purpose=ModelCallPurpose.EVALUATION,
            timeout_seconds=300,
        ),
        dimension=embedding_model.dimension or 0,
        batch_size=32,
    )
    reranker = HttpPassageReranker(
        RetrievalModelRoute(
            endpoint=reranker_target.geturl(),
            model=reranker_model.model_id,
            revision=reranker_model.revision,
            runtime_fingerprint=reranker_model.runtime_fingerprint,
            run_id=run_id,
            task_id=TaskId("task.stage2r-teacher-forced.reranker"),
            trace_id=f"trace.{run_id.root}",
            span_id=None,
            model_role=ModelRole.BATCH_TEST,
            purpose=ModelCallPurpose.EVALUATION,
            timeout_seconds=300,
        )
    )
    engine = build_engine(database_url)
    client = OpenSearch(
        hosts=[{"host": search_target.hostname, "port": search_target.port}],
        use_ssl=search_target.scheme == "https",
        verify_certs=search_target.scheme == "https",
    )
    if not client.ping():
        client.close()
        engine.dispose()
        raise RuntimeError("OpenSearch is unavailable")
    factory = build_session_factory(engine)
    r1 = R1WorldRepository(factory)
    artifacts = ArtifactRepository(FilesystemObjectStore(project_directory / "objects"))
    builder = FullDerivedProjectionBuilder(
        ArtifactProjectionSourceLoader(CommitService(factory), artifacts),
        r1,
        Stage2RSearchIndexer(
            OpenSearchIndex(client),
            embedder,
            embedding_cache=SqlEmbeddingCache(factory),
        ),
        retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
        build_profile="stage2r-hybrid-v0.1",
        embedding_model=embedding_model.model_id,
        embedding_revision=embedding_model.revision,
        embedding_runtime_fingerprint=ArtifactId(f"sha256:{embedding_model.runtime_fingerprint}"),
        reranker_model=reranker_model.model_id,
        reranker_revision=reranker_model.revision,
    )
    return (
        engine,
        client,
        RealHybridProjectionGateway(
            builder=builder,
            snapshots=DerivedSnapshotRepository(factory),
            r1=r1,
            search_index=OpenSearchIndex(client),
            embedder=embedder,
            reranker=reranker,
        ),
    )


def _loopback_postgres_url(value: str | None) -> str:
    if value is None:
        raise ValueError("real_hybrid requires a PostgreSQL database URL")
    parsed = urlparse(value)
    if (
        not parsed.scheme.startswith("postgresql+")
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
    ):
        raise ValueError("real_hybrid database must use a loopback PostgreSQL URL")
    return value


def _loopback_http_url(value: str, label: str):
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
        or parsed.username is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{label} endpoint must be a loopback HTTP(S) URL")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
