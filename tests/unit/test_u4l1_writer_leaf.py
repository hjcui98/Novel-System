from datetime import UTC, datetime

import pytest

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.production_assembly import ResolvedProductionAssemblyAttestation
from novel_agent.domain.retrieval_routing import (
    ProjectionAttestation,
    RetrievalBackendProfile,
    SnapshotCapability,
    SnapshotCapabilityStatus,
)
from novel_agent.domain.u4l1_writer_leaf import (
    U4L1BoundaryCheck,
    U4L1GateStatus,
    U4L1RubricItem,
    U4L1RubricStatus,
    U4L1WriterLeafReport,
)
from novel_agent.domain.writing_loop import (
    WritingLoopResult,
    WritingLoopTerminalStatus,
)

REF = ArtifactRef(
    artifact_id=ArtifactId("sha256:" + "1" * 64),
    media_type="application/json",
    byte_length=1,
    schema_version=SchemaVersion("1.0.0"),
)
COMMIT = CommitId("sha256:" + "2" * 64)
PROJECT = ProjectId("project.u4l1.test")
RUN = RunId("run.u4l1.test")
TASK = TaskId("task.u4l1.test")
SNAPSHOT = StableId("snapshot.u4l1.test")


def _result(*, run_id: RunId = RUN, task_id: TaskId = TASK) -> WritingLoopResult:
    return WritingLoopResult.model_construct(
        result_id=StableId("result.u4l1.test"),
        run_id=run_id,
        task_id=task_id,
        status=WritingLoopTerminalStatus.WRITER_FAILED,
        candidate_only=True,
        canon_mutated=False,
        memory_patch_generated=False,
        commit_called=False,
        failure_detail="synthetic test failure",
    )


def _rubric() -> tuple[U4L1RubricItem, ...]:
    return tuple(
        U4L1RubricItem(
            dimension=dimension,  # type: ignore[arg-type]
            status=U4L1RubricStatus.NOT_SCORED,
            detail="not independently scored",
        )
        for dimension in (
            "plan_obedience",
            "evidence_use",
            "knowledge_boundary",
            "readability",
            "repair_convergence",
            "cost",
        )
    )


def _projection_attestation() -> ProjectionAttestation:
    capability = SnapshotCapability(
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        status=SnapshotCapabilityStatus.EXACT,
    )
    return ProjectionAttestation(
        attestation_id=StableId("attestation.u4l1.test"),
        retrieval_backend_profile=RetrievalBackendProfile.REAL_HYBRID,
        source_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        capability=capability,
        r1_record_count=1,
        r1_entity_association_count=0,
        graph_node_count=0,
        graph_edge_count=0,
        embedding_model="embedding",
        embedding_revision="revision",
        embedding_dimension=1024,
        embedding_normalized=True,
        embedding_runtime_fingerprint=ArtifactId("sha256:" + "3" * 64),
        reranker_model="reranker",
        reranker_revision="revision",
    )


def _report(*, status: U4L1GateStatus = U4L1GateStatus.FAILED) -> U4L1WriterLeafReport:
    return U4L1WriterLeafReport(
        report_id=StableId("report.u4l1.test"),
        generated_at=datetime.now(UTC),
        gate_status=status,
        gate_blockers=("writer_status:WRITER_FAILED",) if status is not U4L1GateStatus.PASS else (),
        project_id=PROJECT,
        run_id=RUN,
        task_id=TASK,
        basis_commit=COMMIT,
        snapshot_id=SNAPSHOT,
        model_identity={},
        endpoint_url="http://127.0.0.1:8005/v1",
        # This failed-report fixture intentionally bypasses attestation validation.
        production_attestation=ResolvedProductionAssemblyAttestation.model_construct(  # type: ignore[call-arg]
        ),
        projection_attestation=_projection_attestation(),
        request_artifacts=(REF,),
        writing_task_artifact=REF,
        accepted_plan_artifact=REF,
        project_profile_artifact=REF,
        writer_context_package_artifact=REF,
        evidence_ledger_artifact=REF,
        recent_prose_artifact=REF,
        api_budget_consistent=False,
        ledger_report_reconstructed=False,
        boundary_checks=(U4L1BoundaryCheck(name="candidate", passed=False, detail="failed"),),
        rubric=_rubric(),
        result=_result(),
    )


def test_failed_writer_result_is_a_valid_failed_report() -> None:
    report = _report()

    assert report.gate_status is U4L1GateStatus.FAILED
    assert report.result.status is WritingLoopTerminalStatus.WRITER_FAILED


def test_pass_report_rejects_non_ready_result() -> None:
    with pytest.raises(ValueError, match="requires a ready Draft candidate"):
        _report(status=U4L1GateStatus.PASS)


def test_not_scored_rubric_rejects_a_score() -> None:
    with pytest.raises(ValueError, match="cannot carry a score"):
        U4L1RubricItem(
            dimension="readability",
            status=U4L1RubricStatus.NOT_SCORED,
            score=0.5,
            detail="not independently scored",
        )


def test_report_rejects_cross_task_result() -> None:
    report = _report()
    payload = report.model_dump(mode="python")
    payload["production_attestation"] = report.production_attestation
    payload["projection_attestation"] = report.projection_attestation
    payload["result"] = _result(task_id=TaskId("task.u4l1.other"))
    with pytest.raises(ValueError, match="result lineage"):
        U4L1WriterLeafReport(**payload)
