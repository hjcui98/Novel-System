from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from scripts.export_stage2_retrieval_gate_evaluation import _experiment_identity

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.gates import (
    Stage2RetrievalCheckpointEvidence,
    Stage2RetrievalGateR1Counts,
    Stage2RetrievalGateReport,
)
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    SchemaVersion,
    StableId,
)
from novel_agent.domain.retrieval_routing import L2IndexKind, RetrievalBackendProfile
from novel_agent.domain.runtime import EvaluationDecision
from novel_agent.services.stage2_retrieval_gate_evaluation import (
    Stage2RetrievalGateEvaluationBuilder,
)

NOW = datetime(2026, 7, 29, tzinfo=UTC)
DATASET = ArtifactId("sha256:" + "d" * 64)
ARTIFACT = ArtifactRef(
    artifact_id=ArtifactId("sha256:" + "e" * 64),
    media_type="application/vnd.novel-agent.stage2-retrieval-gate+json",
    byte_length=100,
    schema_version=SchemaVersion("2.0.0"),
)


def _checkpoint(chapter: int, *, passed: bool = True) -> Stage2RetrievalCheckpointEvidence:
    return Stage2RetrievalCheckpointEvidence(
        checkpoint=chapter,
        source_commit=CommitId("sha256:" + f"{chapter:064x}"),
        snapshot_id=StableId(f"snapshot.retrieval-gate.{chapter}"),
        r1_counts=Stage2RetrievalGateR1Counts(
            records=chapter,
            entity_associations=chapter + 1,
            relation_edges=chapter + 2,
        ),
        index_targets={
            L2IndexKind.ANCHOR: f"physical-anchor-{chapter}",
            L2IndexKind.GROUNDED: f"physical-grounded-{chapter}",
        },
        index_totals={
            L2IndexKind.ANCHOR: chapter + 3,
            L2IndexKind.GROUNDED: chapter + 4,
        },
        failures=() if passed else ("index_count_mismatch",),
        passed=passed,
    )


def _report() -> Stage2RetrievalGateReport:
    return Stage2RetrievalGateReport(
        status="passed",
        project_id=ProjectId("project.retrieval-gate"),
        retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
        checkpoints=tuple(_checkpoint(chapter) for chapter in (20, 40, 60, 80, 95)),
    )


def test_retrieval_gate_builder_creates_five_independent_ledger_entries() -> None:
    config, entries = Stage2RetrievalGateEvaluationBuilder().build(
        _report(),
        ARTIFACT,
        dataset_hash=DATASET,
        code_version="ca9c78e",
        created_at=NOW,
    )

    assert config.dataset_hash == DATASET
    assert config.model_required is False
    assert config.parameters[1].value == [20, 40, 60, 80, 95]
    assert len(entries) == 5
    assert len({entry.evaluation_id for entry in entries}) == 5
    assert entries[-1].commit_id == _report().checkpoints[-1].source_commit
    assert entries[-1].decision is EvaluationDecision.INFORMATIONAL
    assert entries[-1].failure_codes == ()
    assert entries[-1].evidence_artifacts == (ARTIFACT,)
    metrics = {item.name: item.value for item in entries[0].metrics}
    assert metrics["checkpoint_chapter"] == 20.0
    assert metrics["passed"] == 1.0
    assert metrics["immutable_physical_index_target"] == 1.0
    assert metrics["failure_count"] == 0.0


def test_retrieval_gate_contract_and_builder_fail_closed() -> None:
    with pytest.raises(ValidationError, match="pass status"):
        _checkpoint(20).model_copy(update={"passed": False}).model_validate(
            {
                **_checkpoint(20).model_dump(),
                "passed": False,
            }
        )
    with pytest.raises(ValidationError, match="unique and ascending"):
        Stage2RetrievalGateReport(
            status="passed",
            project_id=ProjectId("project.retrieval-gate"),
            retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
            checkpoints=(_checkpoint(40), _checkpoint(20)),
        )
    failed = Stage2RetrievalGateReport(
        status="failed",
        project_id=ProjectId("project.retrieval-gate"),
        retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
        checkpoints=(_checkpoint(20, passed=False),),
    )
    with pytest.raises(ValueError, match="only a passed"):
        Stage2RetrievalGateEvaluationBuilder().build(
            failed,
            ARTIFACT,
            dataset_hash=DATASET,
            code_version="ca9c78e",
            created_at=NOW,
        )


def test_experiment_identity_requires_pinned_dataset_and_code(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "experiment_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "benchmark_content_hash": DATASET.root,
                "code_commit": "ca9c78e",
            }
        ),
        encoding="utf-8",
    )
    assert _experiment_identity(manifest) == (DATASET, "ca9c78e")

    manifest.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        _experiment_identity(manifest)
    manifest.write_text(json.dumps({"code_commit": "ca9c78e"}), encoding="utf-8")
    with pytest.raises(ValueError, match="benchmark_content_hash"):
        _experiment_identity(manifest)
    manifest.write_text(
        json.dumps({"benchmark_content_hash": DATASET.root}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="code_commit"):
        _experiment_identity(manifest)
