"""U3-D: discarding evaluation artifacts must not change Memory identity."""

from __future__ import annotations

from pathlib import Path

import pytest

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, SchemaVersion
from novel_agent.domain.memory_write import CanonicalWriteBasis, SourceProvenance
from novel_agent.domain.v05_readout import MemoryIdentitySnapshot
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.evaluation_namespace import (
    EvaluationNamespaceError,
    discard_evaluation_namespace,
)
from novel_agent.services.information_boundary import (
    InformationBoundaryPort,
    InformationBoundaryViolation,
)
from tests.contract.test_memory_write_workflow_contract import PROJECT, _manifest, _request
from tests.unit.test_information_boundary import TRUSTED_POLICY, _visibility

COMMIT = CommitId("sha256:" + "1" * 64)
ROOT = ArtifactId("sha256:" + "2" * 64)


def _eval_ref() -> ArtifactRef:
    return ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "3" * 64),
        media_type="application/vnd.novel-agent.evaluation.qa-writer-response+json",
        byte_length=4,
        schema_version=SchemaVersion("1.0.0"),
    )


def _identity() -> MemoryIdentitySnapshot:
    return MemoryIdentitySnapshot(
        commit_id=COMMIT,
        text_root=ROOT,
        world_root=ROOT,
        plan_root=ROOT,
        profile_root=ROOT,
    )


def test_discard_keeps_memory_identity_and_rejects_writeback(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    before = _identity()
    ref = _eval_ref()
    receipt = discard_evaluation_namespace(
        artifacts,
        run_id=RunId("run.eval-discard"),
        discarded_refs=(ref,),
        memory_before=before,
        memory_after=before,
    )
    assert receipt.memory_identity_before == receipt.memory_identity_after == before
    request = _request()
    visibility = _visibility(ref, boundary_id=request.information_boundary.boundary_id)
    tainted = request.model_copy(
        update={
            "source_artifacts": (ref,),
            "source_visibility_receipts": (visibility,),
            "source_provenance": (SourceProvenance.REVEALED_TEXT,),
        }
    )
    basis = CanonicalWriteBasis(
        project_id=PROJECT,
        commit_id=COMMIT,
        root_manifest=_manifest(),
    )
    with pytest.raises(InformationBoundaryViolation, match="evaluation"):
        InformationBoundaryPort(
            trusted_policy_hashes=(TRUSTED_POLICY,)
        ).verify_request_and_derivation_graph(tainted, basis)


def test_discard_rejects_non_evaluation_artifacts(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    production = ArtifactRef(
        artifact_id=ROOT,
        media_type="application/json",
        byte_length=1,
        schema_version=SchemaVersion("1.0.0"),
    )
    with pytest.raises(EvaluationNamespaceError, match="evaluation-namespace"):
        discard_evaluation_namespace(
            artifacts,
            run_id=RunId("run.eval-discard-bad"),
            discarded_refs=(production,),
            memory_before=_identity(),
            memory_after=_identity(),
        )


def test_discard_rejects_empty_artifact_list(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    with pytest.raises(EvaluationNamespaceError, match="requires evaluation artifacts"):
        discard_evaluation_namespace(
            artifacts,
            run_id=RunId("run.eval-discard-empty"),
            discarded_refs=(),
            memory_before=_identity(),
            memory_after=_identity(),
        )


def test_discard_rejects_changed_memory_identity(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    after = MemoryIdentitySnapshot(
        commit_id=CommitId("sha256:" + "9" * 64),
        text_root=ROOT,
        world_root=ROOT,
        plan_root=ROOT,
        profile_root=ROOT,
    )
    with pytest.raises(EvaluationNamespaceError, match="Memory identity"):
        discard_evaluation_namespace(
            artifacts,
            run_id=RunId("run.eval-discard-identity"),
            discarded_refs=(_eval_ref(),),
            memory_before=_identity(),
            memory_after=after,
        )
