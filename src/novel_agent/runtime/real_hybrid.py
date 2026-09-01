"""Commit-scoped real-hybrid retrieval for the production runtime."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any
from urllib.parse import urlparse

from opensearchpy import OpenSearch
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.model import (
    HttpEmbeddingProvider,
    HttpPassageReranker,
    RetrievalModelRoute,
)
from novel_agent.adapters.opensearch.search_index import OpenSearchIndex
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, RunId, TaskId
from novel_agent.domain.memory import ChannelHit, RetrievalChannel, Stage1MemoryNeed
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
from novel_agent.services.retrieval import PassageReranker, RerankService, RetrievalBackend
from novel_agent.services.search_retrieval import Stage2RSearchIndexer
from novel_agent.services.stage2_retrieval_backend import (
    RealHybridProjectionGateway,
    Stage2RetrievalBackendError,
)


class ProductionRealHybridError(RuntimeError):
    """Real-hybrid production retrieval cannot start from an incomplete receipt."""


@dataclass
class CommitScopedRealHybridBackend:
    """Route each Memory Need to the exact real-hybrid basis it names."""

    def __init__(
        self,
        *,
        project_id: ProjectId,
        gateway: RealHybridProjectionGateway,
        initial_commit: CommitId | None = None,
        initial_backend: RetrievalBackend | None = None,
    ) -> None:
        self._project_id = project_id
        self._gateway = gateway
        self._bundles: dict[CommitId, RetrievalBackend] = {}
        if initial_commit is not None and initial_backend is not None:
            self._bundles[initial_commit] = initial_backend

    def backend_for(self, source_commit: CommitId) -> RetrievalBackend:
        backend = self._bundles.get(source_commit)
        if backend is None:
            backend = self._gateway.backend_for(self._project_id, source_commit).backend
            self._bundles[source_commit] = backend
        return backend

    def search(
        self,
        need: Stage1MemoryNeed,
        channel: RetrievalChannel,
        limit: int,
    ) -> tuple[ChannelHit, ...]:
        return self.backend_for(need.base_commit).search(need, channel, limit)


@dataclass(frozen=True, slots=True)
class ProductionRealHybridAssembly:
    backend: CommitScopedRealHybridBackend
    reranker: RerankService
    projection_builder: FullDerivedProjectionBuilder
    gateway: RealHybridProjectionGateway


def _native_models_module() -> Any:
    try:
        return import_module("native_models")
    except ModuleNotFoundError as error:
        if error.name != "native_models":
            raise
        return import_module("scripts.native_models")


def assemble_production_real_hybrid(
    *,
    session_factory: sessionmaker[Session],
    commits: CommitService,
    artifacts: ArtifactRepository,
    project_id: ProjectId,
    run_id: RunId,
    opensearch_url: str,
    embedding_url: str,
    reranker_url: str,
) -> ProductionRealHybridAssembly:
    """Build the existing real-hybrid owners and fail closed before the first search."""

    native_models = _native_models_module()
    lock = native_models.load_model_lock()
    embedding_model = lock.models["embedding"]
    reranker_model = lock.models["reranker"]
    native_models.assert_model_service(embedding_model)
    native_models.assert_model_service(reranker_model)
    parsed = urlparse(opensearch_url)
    if parsed.hostname is None or parsed.port is None:
        raise ProductionRealHybridError("OpenSearch URL must include a host and port")
    search_client = OpenSearch(
        hosts=[{"host": parsed.hostname, "port": parsed.port}],
        use_ssl=parsed.scheme == "https",
        verify_certs=parsed.scheme == "https",
    )
    if not search_client.ping():
        search_client.close()
        raise ProductionRealHybridError("OpenSearch is unavailable")
    search_index = OpenSearchIndex(search_client)
    embedding_run = RunId(f"run.stage2r.runtime.{run_id.root}"[:128])
    embedder = HttpEmbeddingProvider(
        RetrievalModelRoute(
            endpoint=embedding_url,
            model=embedding_model.model_id,
            revision=embedding_model.revision,
            runtime_fingerprint=embedding_model.runtime_fingerprint,
            run_id=embedding_run,
            task_id=TaskId("task.stage2r.runtime.embedding"),
            trace_id=f"trace.{embedding_run.root}.embedding",
            span_id=None,
            model_role=ModelRole.BATCH_TEST,
            purpose=ModelCallPurpose.EVALUATION,
            timeout_seconds=300,
        ),
        dimension=embedding_model.dimension or 0,
        batch_size=32,
    )
    passage_reranker: PassageReranker = HttpPassageReranker(
        RetrievalModelRoute(
            endpoint=reranker_url,
            model=reranker_model.model_id,
            revision=reranker_model.revision,
            runtime_fingerprint=reranker_model.runtime_fingerprint,
            run_id=embedding_run,
            task_id=TaskId("task.stage2r.runtime.reranker"),
            trace_id=f"trace.{embedding_run.root}.reranker",
            span_id=None,
            model_role=ModelRole.BATCH_TEST,
            purpose=ModelCallPurpose.EVALUATION,
            timeout_seconds=300,
        )
    )
    projection_builder = FullDerivedProjectionBuilder(
        ArtifactProjectionSourceLoader(commits, artifacts),
        R1WorldRepository(session_factory),
        Stage2RSearchIndexer(
            search_index,
            embedder,
            embedding_cache=SqlEmbeddingCache(session_factory),
            index_namespace=run_id.root,
        ),
        retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
        build_profile="stage2r-hybrid-v0.1",
        embedding_model=embedding_model.model_id,
        embedding_revision=embedding_model.revision,
        embedding_runtime_fingerprint=ArtifactId(f"sha256:{embedding_model.runtime_fingerprint}"),
        reranker_model=reranker_model.model_id,
        reranker_revision=reranker_model.revision,
    )
    gateway = RealHybridProjectionGateway(
        builder=projection_builder,
        snapshots=DerivedSnapshotRepository(session_factory),
        r1=R1WorldRepository(session_factory),
        search_index=search_index,
        embedder=embedder,
        reranker=passage_reranker,
    )
    try:
        current = commits.current_commit(project_id)
    except KeyError as error:
        raise ProductionRealHybridError(
            "real-hybrid production retrieval requires an initialized project"
        ) from error
    try:
        bundle = gateway.backend_for(project_id, current)
    except Stage2RetrievalBackendError as error:
        raise ProductionRealHybridError(str(error)) from error
    if bundle.attestation.retrieval_backend_profile is not RetrievalBackendProfile.REAL_HYBRID:
        raise ProductionRealHybridError("real-hybrid preflight attestation is not real_hybrid")
    if not bundle.attestation.quality_eligible:
        raise ProductionRealHybridError("real-hybrid preflight attestation is not exact")
    missing_indexes = tuple(
        index.physical_name
        for index in bundle.attestation.indexes
        if not search_index.index_exists(index.physical_name)
    )
    if missing_indexes:
        raise ProductionRealHybridError(
            f"projection attestation indexes are unavailable: {missing_indexes}"
        )
    backend = CommitScopedRealHybridBackend(
        project_id=project_id,
        gateway=gateway,
        initial_commit=current,
        initial_backend=bundle.backend,
    )
    return ProductionRealHybridAssembly(
        backend=backend,
        reranker=RerankService(passage_reranker),
        projection_builder=projection_builder,
        gateway=gateway,
    )


__all__ = [
    "CommitScopedRealHybridBackend",
    "ProductionRealHybridAssembly",
    "ProductionRealHybridError",
    "assemble_production_real_hybrid",
]
