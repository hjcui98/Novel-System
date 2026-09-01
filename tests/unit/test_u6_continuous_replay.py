"""Contract edges for the U6-A continuous replay domain."""

from __future__ import annotations

import pytest

from novel_agent.domain.artifacts import (
    ArtifactRef,
    PlanRootRef,
    ProjectProfileRootRef,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, RunId, SchemaVersion, StableId
from novel_agent.domain.u6_continuous_replay import (
    U6BasisKind,
    U6BasisStatus,
    U6CheckpointBasis,
    U6CheckpointBasisManifest,
    U6CheckpointLineage,
    U6ContinuousReplayReport,
)

VERSION = SchemaVersion("1.0.0")
HASH = ArtifactId("sha256:" + "a" * 64)
COMMIT = CommitId("sha256:" + "b" * 64)


def _ref(root_type: type[ArtifactRef]) -> ArtifactRef:
    return root_type(
        artifact_id=HASH,
        media_type="application/vnd.novel-agent.test-root+json",
        byte_length=1,
        schema_version=VERSION,
    )


def _frozen_basis(chapter: int = 20) -> U6CheckpointBasis:
    return U6CheckpointBasis(
        basis_id=StableId(f"basis.{chapter}"),
        checkpoint_chapter=chapter,
        kind=U6BasisKind.PUBLIC_DECLARED,
        status=U6BasisStatus.FROZEN,
        commit_id=COMMIT,
        snapshot_id=StableId(f"snapshot.u6.{chapter}"),
        plan_root_ref=_ref(PlanRootRef),
        text_root_ref=_ref(TextRootRef),
        world_root_ref=_ref(WorldRootRef),
        profile_root_ref=_ref(ProjectProfileRootRef),
    )


def test_pending_basis_cannot_smuggle_root_identity() -> None:
    with pytest.raises(ValueError, match="pending basis cannot carry replay roots"):
        U6CheckpointBasis(
            basis_id=StableId("basis.20"),
            checkpoint_chapter=20,
            kind=U6BasisKind.PUBLIC_DECLARED,
            status=U6BasisStatus.PENDING_REPLAY,
            commit_id=COMMIT,
        )


def test_frozen_basis_requires_every_root_and_deduplicates_jobs() -> None:
    with pytest.raises(ValueError, match="frozen basis requires"):
        U6CheckpointBasis(
            basis_id=StableId("basis.20"),
            checkpoint_chapter=20,
            kind=U6BasisKind.PUBLIC_DECLARED,
            status=U6BasisStatus.FROZEN,
            commit_id=COMMIT,
            snapshot_id=StableId("snapshot.u6.20"),
        )

    with pytest.raises(ValueError, match="basis jobs must be unique"):
        payload = _frozen_basis(20).model_dump(mode="python")
        payload["jobs"] = (StableId("dshort-101"), StableId("dshort-101"))
        U6CheckpointBasis(**payload)


def test_basis_manifest_rejects_duplicate_checkpoint_declarations() -> None:
    with pytest.raises(ValueError, match="unique by chapter"):
        U6CheckpointBasisManifest(
            benchmark_id="novelmem-eval-ztj",
            version="0.5-test",
            frozen_build_id="test",
            status=U6BasisStatus.FROZEN,
            replay_scope="0..300 sequential_once",
            status_note="test",
            basis_nodes=(_frozen_basis(), _frozen_basis()),
        )


def test_checkpoint_lineage_requires_discard_identity_to_be_stable() -> None:
    kwargs = {
        "basis_id": StableId("basis.20"),
        "checkpoint_chapter": 20,
        "commit_id": COMMIT,
        "snapshot_id": StableId("snapshot.u6.20"),
        "plan_root_ref": _ref(PlanRootRef),
        "text_root_ref": _ref(TextRootRef),
        "world_root_ref": _ref(WorldRootRef),
        "profile_root_ref": _ref(ProjectProfileRootRef),
        "index_lineage_ref": _ref(ArtifactRef),
        "memory_identity_before": HASH,
        "memory_identity_after": ArtifactId("sha256:" + "c" * 64),
        "control_replay_identity": HASH,
        "evaluation_namespace": "PENDING_READOUT",
        "identity_match": True,
    }
    with pytest.raises(ValueError, match="durable memory identity"):
        U6CheckpointLineage(**kwargs)


def test_completed_report_requires_all_readout_tasks_and_a_report_ref() -> None:
    common = {
        "campaign_id": StableId("campaign.u6a.test"),
        "run_id": RunId("run.u6a.test"),
        "project_id": ProjectId("project.u6a.test"),
        "benchmark_id": "novelmem-eval-ztj",
        "benchmark_version": "0.5-test",
        "basis_manifest_ref": _ref(ArtifactRef),
        "chapters_declared": 300,
        "chapters_ingested": 300,
        "ingest_passes": 1,
        "public_basis_count": 0,
        "internal_basis_count": 0,
        "basis_count": 0,
        "canary_job_count": 0,
        "expected_readout_task_count": 1,
        "completed_readout_task_count": 0,
        "evaluation_discard_count": 0,
        "future_leakage_count": 0,
        "duplicate_checkpoint_declarations": 0,
        "control_replay_identity": HASH,
        "lineage": (),
    }
    with pytest.raises(ValueError, match="requires a readout report"):
        U6ContinuousReplayReport(**common, status="COMPLETED")
