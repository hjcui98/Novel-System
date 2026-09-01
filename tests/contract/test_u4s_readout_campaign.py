from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, SchemaVersion, StableId
from novel_agent.domain.v05_readout import (
    BenchmarkFailureLayer,
    U4SSeedReadoutReport,
    U4SSeedTaskReport,
    V05ReadoutTrack,
)
from novel_agent.domain.writer_context import BenchmarkInformationProfile

HASH_A = ArtifactId("sha256:" + "a" * 64)
HASH_B = ArtifactId("sha256:" + "b" * 64)
REF_A = ArtifactRef(
    artifact_id=HASH_A,
    media_type="application/vnd.novel-agent.writer-context-package-v2+json",
    byte_length=10,
    schema_version=SchemaVersion("1.0.0"),
)
REF_B = ArtifactRef(
    artifact_id=HASH_B,
    media_type="application/vnd.novel-agent.evidence-ledger-v2+json",
    byte_length=11,
    schema_version=SchemaVersion("1.0.0"),
)


def _task(task_id: str, track: V05ReadoutTrack) -> U4SSeedTaskReport:
    return U4SSeedTaskReport(
        task_id=StableId(task_id),
        track=track,
        checkpoint_chapter=20,
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        basis_commit_id=CommitId("sha256:" + "c" * 64),
        snapshot_id=StableId("snapshot.u4s.contract"),
        package_ref=REF_A,
        evidence_ledger_ref=REF_B,
        freeze_receipt_id=StableId(f"freeze.{task_id}"),
        writer_status="SCHEMA_VALID",
        first_failure_layer=BenchmarkFailureLayer.NONE,
        package_status="READY",
        semantic_status="UNASSESSED",
        future_leakage_count=0,
        response_ref=REF_A,
        readout_record_ref=REF_B,
        raw_response_ref=REF_A,
        judge_receipt_refs=(REF_A, REF_B),
    )


def test_u4s_report_is_schema_valid_without_answer_or_gold_payloads() -> None:
    report = U4SSeedReadoutReport(
        campaign_id=StableId("campaign.u4s.contract"),
        mode="representative",
        run_id=RunId("run.u4s.contract"),
        task_count=2,
        qa_count=1,
        context_count=1,
        chapters_ingested_once=20,
        checkpoint_count=1,
        tasks=(
            _task("task.contract.qa", V05ReadoutTrack.QA),
            _task("task.contract.context", V05ReadoutTrack.CONTEXT),
        ),
        first_failure_layer=BenchmarkFailureLayer.NONE,
        status="COMPLETED",
    )

    payload = report.model_dump(mode="json", by_alias=True)
    assert payload["schema"] == "u4s-seed-readout-report.v1"
    assert "answer" not in payload
    assert "gold" not in payload
    assert "question" not in payload


def test_u4s_report_keeps_typed_failure_distinct_from_success() -> None:
    failed_task = _task("task.contract.failed", V05ReadoutTrack.QA).model_copy(
        update={
            "writer_status": "TYPED_FAILURE",
            "first_failure_layer": BenchmarkFailureLayer.PARSE,
            "response_ref": None,
            "readout_record_ref": None,
        }
    )
    report = U4SSeedReadoutReport(
        campaign_id=StableId("campaign.u4s.contract-failure"),
        mode="representative",
        run_id=RunId("run.u4s.contract-failure"),
        task_count=1,
        qa_count=1,
        context_count=0,
        chapters_ingested_once=20,
        checkpoint_count=1,
        tasks=(failed_task,),
        first_failure_layer=BenchmarkFailureLayer.PARSE,
        status="REVIEW_REQUIRED",
    )

    assert report.status == "REVIEW_REQUIRED"
    assert report.tasks[0].writer_status == "TYPED_FAILURE"
    assert report.tasks[0].first_failure_layer is BenchmarkFailureLayer.PARSE
