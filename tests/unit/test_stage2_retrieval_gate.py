from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from scripts.run_stage2_retrieval_gate import _gate_entry

from novel_agent.domain.ids import ArtifactId, CommitId, StableId
from novel_agent.domain.memory import RetrievalChannel
from novel_agent.domain.retrieval_routing import (
    L2IndexKind,
    L2IndexManifest,
    ProjectionAttestation,
    RetrievalBackendProfile,
    SnapshotCapability,
    SnapshotCapabilityStatus,
)

COMMIT = CommitId("sha256:" + "a" * 64)
SNAPSHOT = StableId("snapshot.retrieval-gate")
HASH = ArtifactId("sha256:" + "b" * 64)
CHANNELS = (
    RetrievalChannel.R1_EXACT,
    RetrievalChannel.R1_TEMPORAL,
    RetrievalChannel.ANCHOR_BM25,
    RetrievalChannel.ANCHOR_DENSE,
    RetrievalChannel.GROUNDED_BM25,
    RetrievalChannel.GROUNDED_DENSE,
    RetrievalChannel.HIERARCHY,
)


def _manifest(kind: L2IndexKind, count: int) -> L2IndexManifest:
    return L2IndexManifest(
        index_id=StableId(f"index.retrieval-gate.{kind.value}"),
        index_kind=kind,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        physical_name=f"physical-{kind.value}",
        alias=f"mutable-{kind.value}",
        document_count=count,
        mapping_hash=HASH,
        analyzer_profile="standard",
        embedding_profile="narrative-bge-m3-v0.1",
    )


def _attestation() -> ProjectionAttestation:
    return ProjectionAttestation(
        attestation_id=StableId("attestation.retrieval-gate"),
        retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        capability=SnapshotCapability(
            source_commit=COMMIT,
            snapshot_id=SNAPSHOT,
            status=SnapshotCapabilityStatus.EXACT,
            available_channels=CHANNELS,
            embedding_profile="narrative-bge-m3-v0.1",
        ),
        r1_record_count=3,
        r1_entity_association_count=4,
        graph_node_count=3,
        graph_edge_count=5,
        indexes=(
            _manifest(L2IndexKind.ANCHOR, 7),
            _manifest(L2IndexKind.GROUNDED, 2),
        ),
        embedding_model="BAAI/bge-m3",
        embedding_revision="locked",
        embedding_dimension=1024,
        embedding_normalized=True,
        embedding_runtime_fingerprint=HASH,
        reranker_model="BAAI/bge-reranker-v2-m3",
        reranker_revision="locked",
    )


def test_historical_retrieval_gate_queries_attested_physical_indexes() -> None:
    targets: list[str] = []
    totals = {"physical-anchor": 7, "physical-grounded": 2}

    def search_with_total(
        target: str,
        _query: dict[str, object],
        *,
        size: int,
    ) -> tuple[list[object], int]:
        assert size == 1
        targets.append(target)
        return [], totals[target]

    snapshots = SimpleNamespace(
        get_for_commit=lambda _commit: SimpleNamespace(
            snapshot_id=SNAPSHOT,
            retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
        ),
        get_attestation_for_commit=lambda _commit: _attestation(),
    )
    r1 = SimpleNamespace(counts=lambda _commit: (3, 4, 5))
    search = SimpleNamespace(search_with_total=search_with_total)

    result = _gate_entry(
        60,
        COMMIT,
        cast(Any, snapshots),
        cast(Any, r1),
        cast(Any, search),
    )

    assert result.passed is True
    assert result.index_targets == {
        L2IndexKind.ANCHOR: "physical-anchor",
        L2IndexKind.GROUNDED: "physical-grounded",
    }
    assert targets == ["physical-anchor", "physical-grounded"]
    assert not any("mutable-" in target for target in targets)
