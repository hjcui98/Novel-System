"""WriterContextReadoutProbe contract tests."""

from __future__ import annotations

import pytest

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.domain.memory_benchmark import ContextWriterConclusion, ContextWriterResponse
from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, QuoteHash, TextSpanRef
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
    ContextAssemblyStatus,
    EvidenceFirstLineage,
    FreezeReceipt,
    WriterContextBudgetReportV2,
    WriterContextPackageV2,
)
from novel_agent.services.memory_benchmark_contract import build_safe_task_contract
from novel_agent.services.writer_context_readout import (
    WriterContextReadoutError,
    WriterContextReadoutProbe,
    WriterContextReadoutRequest,
)

COMMIT = CommitId("sha256:" + "a" * 64)
EVIDENCE = EvidenceRef(
    evidence_id=StableId("evidence.readout"),
    root_hash=ArtifactId("sha256:" + "b" * 64),
    object_hash=ArtifactId("sha256:" + "c" * 64),
    chapter_id=StableId("chapter.100"),
    scene_id=StableId("scene.100"),
    span=TextSpanRef(block_id=StableId("block.100"), start=0, end=1),
    quote_hash=QuoteHash("sha256:" + "d" * 64),
    support_status=EvidenceSupportStatus.CURRENT,
    resolved_at_commit=COMMIT,
)
LEDGER = ArtifactRef(
    artifact_id=ArtifactId("sha256:" + "e" * 64),
    media_type="application/json",
    byte_length=2,
    schema_version=SchemaVersion("1.0.0"),
)


def _task(*, suffix: str = "readout") -> BenchmarkTaskContract:
    return build_safe_task_contract(
        case_id=StableId(f"case.{suffix}"),
        checkpoint_chapter=100,
        target_range=(101, 120),
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        task_intent="test",
    )


def _package(task: BenchmarkTaskContract) -> WriterContextPackageV2:
    return WriterContextPackageV2(
        task_contract=task,
        basis_commit_id=COMMIT,
        basis_snapshot_id=StableId("snapshot.readout"),
        arm="A",
        items=(),
        gaps=(),
        budget_report=WriterContextBudgetReportV2(
            tokenizer="t",
            tokenizer_version="v",
            configured_writer_token_budget=4000,
            actual_rendered_writer_tokens=0,
            configured_ledger_token_budget=12000,
            actual_rendered_ledger_tokens=0,
            item_count=0,
            evidence_item_count=0,
            gap_item_count=0,
            ledger_entry_count=0,
            final_status=ContextAssemblyStatus.READY,
        ),
        evidence_ledger_ref=LEDGER,
        lineage=EvidenceFirstLineage(assembler_version="test"),
        rendered_context="",
    )


def _response(
    task: BenchmarkTaskContract,
    *,
    frozen: bool = True,
    rendered: str = "",
) -> ContextWriterResponse:
    return ContextWriterResponse(
        response_version="context_writer_response.v1",
        task_contract=task,
        basis_commit_id=COMMIT,
        conclusions=(
            ContextWriterConclusion(
                conclusion_id=StableId("conclusion.readout"),
                text="陈长生仍在研究修行困境。",
                evidence_refs=(EVIDENCE,),
            ),
        ),
        gaps=(),
        rendered_response=rendered,
        frozen_before_gold_reveal=frozen,
    )


def test_readout_probe_freezes_writer_answer_before_gold_reveal() -> None:
    task = _task()
    probe = WriterContextReadoutProbe(lambda request: _response(request.task_contract))
    response = probe.run(
        WriterContextReadoutRequest(
            task_contract=task,
            writer_context=_package(task),
        )
    )
    assert isinstance(response, ContextWriterResponse)
    assert response.frozen_before_gold_reveal is True
    assert response.task_contract.task_id == task.task_id


def test_readout_probe_accepts_non_target_rendered_notes() -> None:
    task = _task()
    probe = WriterContextReadoutProbe(
        lambda request: _response(request.task_contract, rendered="历史结论摘要")
    )
    response = probe.run(
        WriterContextReadoutRequest(task_contract=task, writer_context=_package(task))
    )
    assert isinstance(response, ContextWriterResponse)
    assert response.rendered_response == "历史结论摘要"


def test_readout_probe_rejects_gold_reveal() -> None:
    task = _task()
    probe = WriterContextReadoutProbe(lambda request: _response(request.task_contract))
    with pytest.raises(WriterContextReadoutError, match="before Gold reveal"):
        probe.run(
            WriterContextReadoutRequest(
                task_contract=task,
                writer_context=_package(task),
                gold_revealed=True,
            )
        )


def test_readout_probe_rejects_wcp_task_mismatch() -> None:
    task = _task()
    other = _task(suffix="other")
    probe = WriterContextReadoutProbe(lambda request: _response(request.task_contract))
    with pytest.raises(WriterContextReadoutError, match="WCP task_id"):
        probe.run(
            WriterContextReadoutRequest(
                task_contract=task,
                writer_context=_package(other),
            )
        )


def test_readout_probe_rejects_unfrozen_answer() -> None:
    task = _task()
    probe = WriterContextReadoutProbe(
        lambda request: _response(request.task_contract, frozen=False)
    )
    with pytest.raises(WriterContextReadoutError, match="not frozen"):
        probe.run(WriterContextReadoutRequest(task_contract=task, writer_context=_package(task)))


def test_readout_probe_rejects_answer_task_mismatch() -> None:
    task = _task()
    other = _task(suffix="other")
    probe = WriterContextReadoutProbe(lambda _request: _response(other))
    with pytest.raises(WriterContextReadoutError, match="Writer answer task_id"):
        probe.run(WriterContextReadoutRequest(task_contract=task, writer_context=_package(task)))


def test_readout_probe_rejects_target_window_end_prose() -> None:
    task = _task()
    probe = WriterContextReadoutProbe(
        lambda request: _response(request.task_contract, rendered="第120章收束")
    )
    with pytest.raises(WriterContextReadoutError, match="target-window prose"):
        probe.run(WriterContextReadoutRequest(task_contract=task, writer_context=_package(task)))


def test_readout_probe_rejects_target_window_prose() -> None:
    task = _task()
    probe = WriterContextReadoutProbe(
        lambda request: _response(request.task_contract, rendered="第101章正文草稿")
    )
    with pytest.raises(WriterContextReadoutError, match="target-window prose"):
        probe.run(WriterContextReadoutRequest(task_contract=task, writer_context=_package(task)))


PLAN_HASH = ArtifactId("sha256:" + "1" * 64)


def _freeze() -> FreezeReceipt:
    digest = ArtifactId("sha256:" + "f" * 64)
    return FreezeReceipt(
        receipt_id=StableId("freeze.readout"),
        public_input_hash=digest,
        code_version="test",
        run_config_hash=digest,
        arm_artifact_hashes={"A": digest, "B": digest, "C": digest},
        frozen_before_reveal=True,
    )


def _production_probe() -> WriterContextReadoutProbe:
    return WriterContextReadoutProbe(
        lambda request: _response(request.task_contract),
        require_production_contract=True,
    )


def test_production_probe_rejects_missing_freeze_receipt() -> None:
    task = _task()
    with pytest.raises(WriterContextReadoutError, match="freeze receipt"):
        _production_probe().run(
            WriterContextReadoutRequest(task_contract=task, writer_context=_package(task))
        )


def test_production_probe_rejects_author_plan_without_planning_context() -> None:
    task = _task()
    with pytest.raises(WriterContextReadoutError, match="frozen planning context"):
        _production_probe().run(
            WriterContextReadoutRequest(
                task_contract=task,
                writer_context=_package(task),
                freeze_receipt=_freeze(),
                case_id=StableId("case.readout"),
            )
        )


def test_production_probe_rejects_history_only_with_planning_context() -> None:
    task = build_safe_task_contract(
        case_id=StableId("case.blind-plan"),
        checkpoint_chapter=100,
        target_range=(101, 120),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        planning_context_ref=PLAN_HASH,
        planning_context_hash=PLAN_HASH,
    )
    probe = WriterContextReadoutProbe(
        lambda request: _response(request.task_contract),
        require_production_contract=True,
    )
    with pytest.raises(WriterContextReadoutError, match="must not carry planning context"):
        probe.run(
            WriterContextReadoutRequest(
                task_contract=task,
                writer_context=_package(task),
                freeze_receipt=_freeze(),
                case_id=StableId("case.blind-plan"),
            )
        )
