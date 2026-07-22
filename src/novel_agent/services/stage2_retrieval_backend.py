"""Commit-scoped real-hybrid retrieval backend assembly for Stage 2R."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from novel_agent.adapters.opensearch.search_index import OpenSearchIndex
from novel_agent.domain.ids import CommitId, ProjectId, StableId
from novel_agent.domain.memory import RetrievalChannel
from novel_agent.domain.retrieval_routing import (
    L2IndexKind,
    ProjectionAttestation,
    RetrievalBackendProfile,
)
from novel_agent.services.projection import DerivedSnapshotRepository, FullDerivedProjectionBuilder
from novel_agent.services.r1 import R1RetrievalBackend, R1WorldRepository
from novel_agent.services.retrieval import PassageReranker, RerankService, RetrievalBackend
from novel_agent.services.search_retrieval import (
    CompositeRetrievalBackend,
    EmbeddingProvider,
    Stage2ROpenSearchBackend,
)


class Stage2RetrievalBackendError(RuntimeError):
    """A real-hybrid backend cannot be assembled from an incomplete receipt."""


@dataclass(frozen=True, slots=True)
class Stage2RetrievalBackendBundle:
    backend: RetrievalBackend
    attestation: ProjectionAttestation
    allowed_channels: tuple[RetrievalChannel, ...]
    reranker: RerankService


class RealHybridProjectionGateway:
    """Ensure one commit's derived state is exact before exposing its backend.

    The gateway is deliberately a derived-data adapter: it may rebuild R1/L2
    projections and replace only the corresponding DerivedSnapshot metadata.
    It never changes Canonical Roots or a commit's identity.
    """

    def __init__(
        self,
        *,
        builder: FullDerivedProjectionBuilder,
        snapshots: DerivedSnapshotRepository,
        r1: R1WorldRepository,
        search_index: OpenSearchIndex,
        embedder: EmbeddingProvider,
        reranker: PassageReranker,
    ) -> None:
        self._builder = builder
        self._snapshots = snapshots
        self._r1 = r1
        self._search_index = search_index
        self._embedder = embedder
        self._reranker = reranker

    def backend_for(
        self,
        project_id: ProjectId,
        source_commit: CommitId,
    ) -> Stage2RetrievalBackendBundle:
        attestation = self._snapshots.get_attestation_for_commit(source_commit)
        if attestation is None or not attestation.quality_eligible:
            rebuilt = self._builder.build(project_id, source_commit)
            if rebuilt.projection_attestation is None:
                raise Stage2RetrievalBackendError("real-hybrid projection produced no attestation")
            self._snapshots.publish_rebuilt(project_id, rebuilt)
            attestation = _load_persisted_attestation(rebuilt.projection_attestation)
        return build_real_hybrid_backend(
            r1=self._r1,
            search_index=self._search_index,
            embedder=self._embedder,
            project_id=project_id,
            source_commit=source_commit,
            snapshot_id=attestation.snapshot_id,
            attestation=attestation,
            reranker=self._reranker,
        )


def build_real_hybrid_backend(
    *,
    r1: R1WorldRepository,
    search_index: OpenSearchIndex,
    embedder: EmbeddingProvider,
    project_id: ProjectId,
    source_commit: CommitId,
    snapshot_id: StableId,
    attestation: ProjectionAttestation,
    reranker: PassageReranker,
    graph_depth: int = 2,
) -> Stage2RetrievalBackendBundle:
    """Create only the channels certified for one exact, commit-scoped snapshot."""

    _validate_attestation(
        attestation,
        source_commit=source_commit,
        snapshot_id=snapshot_id,
        embedding_profile=embedder.profile,
    )
    available = set(attestation.capability.available_channels)
    access_scopes = ("writer_safe", "author_planning", "evaluator")
    r1_backend = R1RetrievalBackend(
        r1,
        snapshot_id=snapshot_id,
        graph_depth=graph_depth,
        access_scopes=access_scopes,
    )
    indexes = {item.index_kind: item for item in attestation.indexes}
    search_backend = Stage2ROpenSearchBackend(
        search_index,
        embedder,
        project_id=project_id,
        source_commit=source_commit,
        snapshot_id=snapshot_id,
        access_scopes=access_scopes,
        anchor_index_name=indexes[L2IndexKind.ANCHOR].physical_name,
        grounded_index_name=indexes[L2IndexKind.GROUNDED].physical_name,
    )
    r1_channels = {
        RetrievalChannel.R1_EXACT,
        RetrievalChannel.R1_TEMPORAL,
        RetrievalChannel.TYPED_GRAPH,
    }
    search_channels = {
        RetrievalChannel.ANCHOR_BM25,
        RetrievalChannel.ANCHOR_DENSE,
        RetrievalChannel.GROUNDED_BM25,
        RetrievalChannel.GROUNDED_DENSE,
        RetrievalChannel.HIERARCHY,
        RetrievalChannel.TYPED_GRAPH,
    }
    routes: dict[RetrievalChannel, RetrievalBackend] = {}
    routes.update({channel: r1_backend for channel in available & r1_channels})
    routes.update({channel: search_backend for channel in available & search_channels})
    return Stage2RetrievalBackendBundle(
        backend=CompositeRetrievalBackend(routes),
        attestation=attestation,
        allowed_channels=tuple(sorted(routes, key=lambda channel: channel.value)),
        reranker=RerankService(reranker),
    )


def _load_persisted_attestation(raw: Mapping[str, object]) -> ProjectionAttestation:
    """Restore strict domain types from a JSON-compatible snapshot payload."""

    return ProjectionAttestation.model_validate_json(json.dumps(raw))


def _validate_attestation(
    attestation: ProjectionAttestation,
    *,
    source_commit: CommitId,
    snapshot_id: StableId,
    embedding_profile: str,
) -> None:
    if attestation.retrieval_backend_profile is not RetrievalBackendProfile.REAL_HYBRID:
        raise Stage2RetrievalBackendError("real-hybrid backend requires a real-hybrid attestation")
    if not attestation.quality_eligible:
        raise Stage2RetrievalBackendError(
            "real-hybrid attestation is incomplete or has failure debt"
        )
    if attestation.source_commit != source_commit or attestation.snapshot_id != snapshot_id:
        raise Stage2RetrievalBackendError("attestation basis does not match requested backend")
    if attestation.embedding_dimension != 1024 or attestation.embedding_normalized is not True:
        raise Stage2RetrievalBackendError(
            "attestation does not certify normalized 1024d embeddings"
        )
    if attestation.capability.embedding_profile != embedding_profile:
        raise Stage2RetrievalBackendError(
            "runtime embedding profile differs from snapshot attestation"
        )
    expected = {
        RetrievalChannel.R1_EXACT,
        RetrievalChannel.R1_TEMPORAL,
        RetrievalChannel.ANCHOR_BM25,
        RetrievalChannel.ANCHOR_DENSE,
        RetrievalChannel.GROUNDED_BM25,
        RetrievalChannel.GROUNDED_DENSE,
        RetrievalChannel.HIERARCHY,
    }
    available = set(attestation.capability.available_channels)
    if not expected.issubset(available):
        missing = sorted(channel.value for channel in expected - available)
        raise Stage2RetrievalBackendError(
            f"attestation lacks required real-hybrid channels: {missing}"
        )
    kinds = {index.index_kind for index in attestation.indexes}
    if not {L2IndexKind.ANCHOR, L2IndexKind.GROUNDED}.issubset(kinds):
        raise Stage2RetrievalBackendError("attestation lacks Anchor or Grounded index receipt")
    if any("stage2r" not in index.alias for index in attestation.indexes):
        raise Stage2RetrievalBackendError(
            "attestation index aliases are not isolated Stage 2R aliases"
        )
