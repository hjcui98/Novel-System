from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

import novel_agent.services.stage2_retrieval_backend as backend_module
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, StableId
from novel_agent.domain.memory import RetrievalChannel
from novel_agent.domain.retrieval_routing import (
    ChannelFailure,
    ChannelFailureCode,
    L2IndexKind,
    L2IndexManifest,
    ProjectionAttestation,
    RetrievalBackendProfile,
    SnapshotCapability,
    SnapshotCapabilityStatus,
)
from novel_agent.services.stage2_retrieval_backend import (
    RealHybridProjectionGateway,
    Stage2RetrievalBackendError,
    _validate_attestation,
)

COMMIT = CommitId("sha256:" + "a" * 64)
SNAPSHOT = StableId("snapshot.backend")
HASH = ArtifactId(COMMIT.root)
PROJECT = ProjectId("project.backend")
CHANNELS = (
    RetrievalChannel.R1_EXACT,
    RetrievalChannel.R1_TEMPORAL,
    RetrievalChannel.ANCHOR_BM25,
    RetrievalChannel.ANCHOR_DENSE,
    RetrievalChannel.GROUNDED_BM25,
    RetrievalChannel.GROUNDED_DENSE,
    RetrievalChannel.HIERARCHY,
)


def manifest(kind: L2IndexKind) -> L2IndexManifest:
    return L2IndexManifest(
        index_id=StableId(f"index.{kind.value}"),
        index_kind=kind,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        physical_name=f"physical-{kind.value}",
        alias=f"project-stage2r-{kind.value}",
        document_count=1,
        mapping_hash=HASH,
        analyzer_profile="standard",
        embedding_profile=(None if kind is L2IndexKind.HIERARCHY else "narrative-bge-m3-v0.1"),
    )


def attestation() -> ProjectionAttestation:
    capability = SnapshotCapability(
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        status=SnapshotCapabilityStatus.EXACT,
        available_channels=CHANNELS,
        embedding_profile="narrative-bge-m3-v0.1",
    )
    return ProjectionAttestation(
        attestation_id=StableId("attestation.backend"),
        retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        capability=capability,
        r1_record_count=1,
        r1_entity_association_count=1,
        graph_node_count=0,
        graph_edge_count=0,
        indexes=(manifest(L2IndexKind.ANCHOR), manifest(L2IndexKind.GROUNDED)),
        embedding_model="BAAI/bge-m3",
        embedding_revision="locked",
        embedding_dimension=1024,
        embedding_normalized=True,
        embedding_runtime_fingerprint=HASH,
        reranker_model="BAAI/bge-reranker-v2-m3",
        reranker_revision="locked",
    )


def validate(item: ProjectionAttestation) -> None:
    _validate_attestation(
        item,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        embedding_profile="narrative-bge-m3-v0.1",
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("profile", "requires a real-hybrid"),
        ("debt", "incomplete or has failure debt"),
        ("basis", "basis does not match"),
        ("dimension", "normalized 1024d"),
        ("embedding", "embedding profile differs"),
        ("channels", "lacks required"),
        ("indexes", "lacks Anchor or Grounded"),
        ("alias", "not isolated"),
    ),
)
def test_real_hybrid_attestation_rejects_every_untrusted_runtime_mismatch(
    mutation: str,
    message: str,
) -> None:
    item = attestation()
    if mutation == "profile":
        item = item.model_copy(
            update={"retrieval_backend_profile": RetrievalBackendProfile.SCRIPTED_SMOKE}
        )
    elif mutation == "debt":
        item = item.model_copy(
            update={
                "failures": (
                    ChannelFailure(
                        channel=RetrievalChannel.ANCHOR_DENSE,
                        code=ChannelFailureCode.TIMEOUT,
                        reason="timeout",
                    ),
                )
            }
        )
    elif mutation == "basis":
        item = item.model_copy(update={"source_commit": CommitId("sha256:" + "b" * 64)})
    elif mutation == "dimension":
        item = item.model_copy(update={"embedding_dimension": 768})
    elif mutation == "embedding":
        item = item.model_copy(
            update={"capability": item.capability.model_copy(update={"embedding_profile": "other"})}
        )
    elif mutation == "channels":
        item = item.model_copy(
            update={
                "capability": item.capability.model_copy(
                    update={"available_channels": CHANNELS[:-1]}
                )
            }
        )
    elif mutation == "indexes":
        item = item.model_copy(update={"indexes": (manifest(L2IndexKind.ANCHOR),)})
    elif mutation == "alias":
        item = item.model_copy(
            update={
                "indexes": (
                    manifest(L2IndexKind.ANCHOR).model_copy(update={"alias": "shared-anchor"}),
                    manifest(L2IndexKind.GROUNDED),
                )
            }
        )
    with pytest.raises(Stage2RetrievalBackendError, match=message):
        validate(item)


def test_projection_gateway_reuses_or_rebuilds_attested_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = attestation()
    validate(current)
    marker = object()
    calls: list[dict[str, Any]] = []

    def build_backend(**kwargs: Any) -> object:
        calls.append(kwargs)
        return marker

    monkeypatch.setattr(backend_module, "build_real_hybrid_backend", build_backend)
    snapshots = SimpleNamespace(
        get_attestation_for_commit=lambda _: current,
        publish_rebuilt=lambda *_: None,
    )
    builder = SimpleNamespace(build=lambda *_: None)
    gateway = RealHybridProjectionGateway(
        builder=cast(Any, builder),
        snapshots=cast(Any, snapshots),
        r1=cast(Any, object()),
        search_index=cast(Any, object()),
        embedder=cast(Any, object()),
        reranker=cast(Any, object()),
    )
    assert gateway.backend_for(PROJECT, COMMIT) is marker
    assert calls[-1]["snapshot_id"] == SNAPSHOT

    snapshots.get_attestation_for_commit = lambda _: None
    builder.build = lambda *_: SimpleNamespace(projection_attestation=None)
    with pytest.raises(Stage2RetrievalBackendError, match="produced no attestation"):
        gateway.backend_for(PROJECT, COMMIT)

    published: list[object] = []
    snapshots.publish_rebuilt = lambda *args: published.append(args)
    builder.build = lambda *_: SimpleNamespace(
        projection_attestation=current.model_dump(mode="json")
    )
    assert gateway.backend_for(PROJECT, COMMIT) is marker
    assert published
