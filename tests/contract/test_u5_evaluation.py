import pytest

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, RunId, SchemaVersion, StableId
from novel_agent.domain.u5_evaluation import (
    U5C20EvaluationIsolationReport,
    U5EvaluationTaskEvidence,
)
from novel_agent.domain.v05_readout import MemoryIdentitySnapshot

HASH = ArtifactId("sha256:" + "1" * 64)
VERSION = SchemaVersion("1.0.0")


def _ref() -> ArtifactRef:
    return ArtifactRef(
        artifact_id=HASH,
        media_type="application/vnd.novel-agent.evaluation.answer+json",
        byte_length=1,
        schema_version=VERSION,
    )


def _memory() -> MemoryIdentitySnapshot:
    return MemoryIdentitySnapshot(
        commit_id=CommitId("sha256:" + "2" * 64),
        text_root=HASH,
        world_root=HASH,
        plan_root=HASH,
        profile_root=HASH,
    )


def _tasks() -> tuple[U5EvaluationTaskEvidence, ...]:
    return (
        U5EvaluationTaskEvidence(
            task_id=StableId("u5.qa"),
            track="novelmem_qa",
            information_profile="visible_at_cutoff",
            evaluation_task_identity=StableId("eval.u5.qa"),
            basis_commit_id=_memory().commit_id,
            freeze_receipt_id=StableId("freeze.qa"),
            writer_status="SCHEMA_VALID",
            response_ref=_ref(),
        ),
        U5EvaluationTaskEvidence(
            task_id=StableId("u5.context.history"),
            track="novelmem_context",
            information_profile="visible_at_cutoff",
            evaluation_task_identity=StableId("eval.u5.context.history"),
            basis_commit_id=_memory().commit_id,
            freeze_receipt_id=StableId("freeze.history"),
            writer_status="SCHEMA_VALID",
            response_ref=_ref(),
        ),
        U5EvaluationTaskEvidence(
            task_id=StableId("u5.context.apc"),
            track="novelmem_context",
            information_profile="author_plan_conditioned",
            evaluation_task_identity=StableId("eval.u5.context.apc"),
            basis_commit_id=_memory().commit_id,
            freeze_receipt_id=StableId("freeze.apc"),
            writer_status="SCHEMA_VALID",
            response_ref=_ref(),
        ),
    )


def _report(**overrides: object) -> U5C20EvaluationIsolationReport:
    memory = _memory()
    payload: dict[str, object] = {
        "run_id": RunId("run.u5.eval"),
        "project_id": ProjectId("project.u5"),
        "basis_commit": memory.commit_id,
        "memory_identity_before": memory,
        "memory_identity_after": memory,
        "tasks": _tasks(),
        "discard_receipt_ref": _ref(),
        "canonical_commit_count_before": 1,
        "canonical_commit_count_after": 1,
        "runtime_task_count_before": 0,
        "runtime_task_count_after": 0,
        "model_call_count": 3,
        "evaluation_artifact_count": 4,
        "c21_request_path": "/tmp/u5-request.json",
        "status": "COMPLETED",
    }
    payload.update(overrides)
    return U5C20EvaluationIsolationReport.model_validate(payload)


def test_u5_evaluation_contract_requires_three_non_production_tasks() -> None:
    report = _report()

    assert report.report_schema == "u5-c20-evaluation-isolation.v1"
    assert report.c21_private_fields_absent is True


def test_u5_evaluation_contract_rejects_canonical_change() -> None:
    with pytest.raises(ValueError, match="canonical Commit count"):
        _report(canonical_commit_count_after=2)
