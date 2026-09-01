"""U3-E: reports rebuild from ledger, freeze receipts, and artifacts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.model.fake import FakeModelEndpoint
from novel_agent.domain.artifacts import DURABLE_EVIDENCE_REPORT_MEDIA_TYPE
from novel_agent.domain.ids import ArtifactId, ProjectId, RunId, StableId, TaskId
from novel_agent.domain.memory_benchmark import (
    ContextWriterConclusion,
    ContextWriterResponse,
    QaEvidenceItem,
    QaWriterResponse,
)
from novel_agent.domain.model_calls import (
    BudgetSource,
    EffectiveBudgetResult,
    ModelCallLedgerEntry,
    ModelCallLedgerStatus,
    ModelCallPurpose,
    ModelCostAvailability,
    ModelRequest,
    ModelRole,
    ProviderModelResult,
)
from novel_agent.domain.v05_readout import (
    BenchmarkFailureLayer,
    WriterJudgeAvailability,
    WriterJudgeKind,
    WriterJudgePair,
    WriterJudgeReceipt,
)
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    ContextAssemblyStatus,
    WriterContextEvidenceItem,
    WriterContextSection,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.durable_report_rebuild import (
    DurableReportRebuildError,
    persist_durable_evidence_report,
    rebuild_durable_evidence_report,
)
from novel_agent.services.gold_evidence_matching import GoldEvidenceMatch, GoldEvidenceMatcher
from novel_agent.services.memory_benchmark_contract import profile_namespace
from novel_agent.services.model_call_ledger import InMemoryModelCallLedger
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.writer_judge import WriterJudgeService
from tests.fixtures.stage2_memory_benchmark import frozen_evaluation_inputs
from tests.unit.test_production_writer_readout import _freeze, _package, _task
from tests.unit.test_writer_judge_receipts import _ref

PROJECT = ProjectId("project.rebuild")
EXPERIMENT = "v05-rebuild"


class _Output(BaseModel):
    answer: str


def _request() -> ModelRequest:
    return ModelRequest(
        request_id=StableId("model.rebuild.request"),
        run_id=RunId("run.rebuild"),
        task_id=TaskId("task.rebuild"),
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id="trace.rebuild",
        prompt="rebuild",
        scheduling_stage="benchmark.writer_qa_readout",
    )


def test_rebuild_locates_parse_failure_and_keeps_pending_gold_hits_unset(
    tmp_path: Path,
) -> None:
    class SequenceEndpoint(FakeModelEndpoint):
        def __init__(self) -> None:
            super().__init__("")
            self.responses = iter(("not-json", '{"answer":"ok"}'))

        async def generate(self, request: ModelRequest) -> ProviderModelResult:
            self.response_text = next(self.responses)
            return await super().generate(request)

    ledger = InMemoryModelCallLedger()
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="rebuild-endpoint",
                model_name="rebuild-model",
                adapter=SequenceEndpoint(),
            ),
        ),
        structured_max_retries=1,
        call_ledger=ledger,
    )
    request = _request()
    asyncio.run(gateway.generate_structured(request, _Output))
    freeze = _freeze()
    package = _package(
        _task(profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF),
        _ref(),
    )
    pair = WriterJudgeService(
        ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    ).pending_pair(
        run_id=request.run_id,
        task_id=StableId(request.task_id.root),
        freeze_receipt_id=freeze.receipt_id,
        response_ref=_ref(),
    )
    report = rebuild_durable_evidence_report(
        run_id=request.run_id,
        freeze_receipt=freeze,
        ledger_entries=ledger.list_for_run(request.run_id),
        writer_context=package,
        project_id=PROJECT,
        experiment_id=EXPERIMENT,
        judge_pair=pair,
    )
    stored = persist_durable_evidence_report(
        ArtifactRepository(FilesystemObjectStore(tmp_path / "report")),
        report,
    )
    assert stored.media_type == DURABLE_EVIDENCE_REPORT_MEDIA_TYPE
    assert report.first_failure_layer is BenchmarkFailureLayer.PARSE
    assert report.answer_judge_availability is WriterJudgeAvailability.PENDING
    assert report.gold_hit_count is None
    assert report.cost_availability is ModelCostAvailability.UNKNOWN
    assert report.phase_aggregates[0].schema_retry_count == 1
    statuses = report.phase_aggregates[0].status_counts
    assert statuses[ModelCallLedgerStatus.VALIDATION_REJECTED.value] == 1
    assert statuses[ModelCallLedgerStatus.COMPLETED.value] == 1


def test_history_only_and_apc_use_disjoint_profile_namespaces() -> None:
    freeze = _freeze()
    package = _package(
        _task(profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF),
        _ref(),
    )
    report = rebuild_durable_evidence_report(
        run_id=RunId("run.rebuild-ns"),
        freeze_receipt=freeze,
        ledger_entries=(),
        writer_context=package,
        project_id=PROJECT,
        experiment_id=EXPERIMENT,
        extra_profiles=(BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,),
    )
    visible = profile_namespace(PROJECT, BenchmarkInformationProfile.VISIBLE_AT_CUTOFF, EXPERIMENT)
    planned = profile_namespace(
        PROJECT, BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED, EXPERIMENT
    )
    assert visible != planned
    assert report.profile_namespaces == (visible, planned)
    assert report.first_failure_layer is BenchmarkFailureLayer.NONE
    assert report.cost_availability is ModelCostAvailability.NOT_APPLICABLE
    assert report.gold_hit_count is None


def test_package_failure_is_located_before_writer_answer() -> None:
    freeze = _freeze()
    package = _package(
        _task(profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF),
        _ref(),
    )
    package = package.model_copy(
        update={
            "budget_report": package.budget_report.model_copy(
                update={"final_status": ContextAssemblyStatus.EVIDENCE_INSUFFICIENT}
            )
        }
    )
    report = rebuild_durable_evidence_report(
        run_id=RunId("run.rebuild-package"),
        freeze_receipt=freeze,
        ledger_entries=(),
        writer_context=package,
        project_id=PROJECT,
        experiment_id=EXPERIMENT,
    )
    assert report.first_failure_layer is BenchmarkFailureLayer.PACKAGE


def test_lineage_counts_are_derived_from_frozen_artifacts(tmp_path: Path) -> None:
    gold, _package_v1, ledger, freeze = frozen_evaluation_inputs()
    refs = ledger.entries[0].evidence_refs
    task = _task(profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED, suffix="lineage")
    package = _package(task, _ref())
    item = WriterContextEvidenceItem(
        item_id=StableId("item.rebuild-lineage"),
        section=WriterContextSection.CURRENT_WORLD_STATE,
        need_ids=(StableId("need.rebuild-lineage"),),
        need_facet_ids=(StableId("facet.rebuild-lineage"),),
        purpose="frozen historical constraint",
        evidence_ledger_ids=(ledger.entries[0].ledger_id,),
        raw_preview="陈长生仍受经脉问题约束。",
    )
    package = package.model_copy(
        update={
            "items": (item,),
            "budget_report": package.budget_report.model_copy(
                update={"item_count": 1, "evidence_item_count": 1}
            ),
        }
    )
    response = ContextWriterResponse(
        response_version="context_writer_response.v1",
        task_contract=task,
        basis_commit_id=package.basis_commit_id,
        conclusions=(
            ContextWriterConclusion(
                conclusion_id=StableId("conclusion.rebuild-lineage"),
                text="陈长生仍受经脉问题约束。",
                evidence_refs=refs,
            ),
        ),
        frozen_before_gold_reveal=True,
    )
    match = GoldEvidenceMatcher().match(gold, ledger)
    pair = WriterJudgeService(
        ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    ).record_available_pair(
        run_id=RunId("run.rebuild-lineage"),
        task_id=task.task_id,
        freeze_receipt_id=freeze.receipt_id,
        response_ref=_ref(),
        answer_input_ref=_ref(),
        answer_output_ref=_ref(),
        answer_score=0.5,
        evidence_input_ref=_ref(),
        evidence_output_ref=_ref(),
        evidence_score=1.0 if match.matched else 0.0,
        answer_model_request_id=StableId("model.answer-judge.lineage"),
        evidence_model_request_id=StableId("model.evidence-judge.lineage"),
    )
    report = rebuild_durable_evidence_report(
        run_id=RunId("run.rebuild-lineage"),
        freeze_receipt=freeze,
        ledger_entries=(),
        writer_context=package,
        project_id=PROJECT,
        experiment_id=EXPERIMENT,
        writer_response=response,
        evidence_ledger=ledger,
        gold_match=match,
        judge_pair=pair,
    )
    assert report.writer_context_item_count == 1
    assert report.writer_used_item_count == 1
    assert report.cited_evidence_count == len(tuple(dict.fromkeys(refs)))
    assert report.gold_hit_count is not None
    assert report.gold_hit_count >= 1
    assert report.first_failure_layer is BenchmarkFailureLayer.NONE


def _status_entry(
    *,
    status: ModelCallLedgerStatus,
    run_id: RunId,
    phase: str = "unknown",
    request_id: str = "model.rebuild.layer",
) -> ModelCallLedgerEntry:
    now = datetime.now(UTC)
    kwargs: dict[str, object] = {
        "request_id": StableId(request_id),
        "run_id": run_id,
        "task_id": TaskId("task.rebuild"),
        "request_hash": ArtifactId("sha256:" + "e" * 64),
        "effective_budget": EffectiveBudgetResult(
            budget_source=BudgetSource.EXPLICIT_REQUEST,
            context_limit=100,
            estimated_input_tokens=10,
            body_output_budget=20,
            thinking_budget=5,
            total_output_budget=25,
            safety_allowance_tokens=5,
            reserved_sequence_tokens=40,
            available_input_tokens=70,
        ),
        "reasoning_included_in_completion_tokens": False,
        "status": status,
        "logical_phase": phase,
        "requested_at": now,
    }
    if status is ModelCallLedgerStatus.TRANSPORT_EXHAUSTED:
        kwargs["transport_error_type"] = "TimeoutError"
        kwargs["completed_at"] = now
    return ModelCallLedgerEntry.model_validate(kwargs)


def test_rebuild_rejects_unfrozen_receipt_and_foreign_run() -> None:
    freeze = _freeze()
    package = _package(_task(profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF), _ref())
    with pytest.raises(DurableReportRebuildError, match="freeze receipt"):
        rebuild_durable_evidence_report(
            run_id=RunId("run.rebuild-unfrozen"),
            freeze_receipt=freeze.model_copy(update={"frozen_before_reveal": False}),
            ledger_entries=(),
            writer_context=package,
            project_id=PROJECT,
            experiment_id=EXPERIMENT,
        )
    foreign = _status_entry(status=ModelCallLedgerStatus.REQUESTED, run_id=RunId("run.other"))
    with pytest.raises(DurableReportRebuildError, match="another run"):
        rebuild_durable_evidence_report(
            run_id=RunId("run.rebuild-foreign"),
            freeze_receipt=freeze,
            ledger_entries=(foreign,),
            writer_context=package,
            project_id=PROJECT,
            experiment_id=EXPERIMENT,
        )


def test_rebuild_locates_transport_raw_and_judge_layers(tmp_path: Path) -> None:
    freeze = _freeze()
    package = _package(_task(profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF), _ref())
    run_id = RunId("run.rebuild-layers")
    transport = rebuild_durable_evidence_report(
        run_id=run_id,
        freeze_receipt=freeze,
        ledger_entries=(
            _status_entry(status=ModelCallLedgerStatus.TRANSPORT_EXHAUSTED, run_id=run_id),
        ),
        writer_context=package,
        project_id=PROJECT,
        experiment_id=EXPERIMENT,
    )
    assert transport.first_failure_layer is BenchmarkFailureLayer.TRANSPORT
    raw = rebuild_durable_evidence_report(
        run_id=run_id,
        freeze_receipt=freeze,
        ledger_entries=(_status_entry(status=ModelCallLedgerStatus.UNCERTAIN, run_id=run_id),),
        writer_context=package,
        project_id=PROJECT,
        experiment_id=EXPERIMENT,
    )
    assert raw.first_failure_layer is BenchmarkFailureLayer.RAW
    qa = QaWriterResponse(
        answer="经脉堵塞",
        evidence=(QaEvidenceItem(chapter=0, span="陈长生被诊断出经脉堵塞"),),
    )
    qa_report = rebuild_durable_evidence_report(
        run_id=run_id,
        freeze_receipt=freeze,
        ledger_entries=(),
        writer_context=package,
        project_id=PROJECT,
        experiment_id=EXPERIMENT,
        writer_response=qa,
        gold_match=GoldEvidenceMatch(matched=True, reason="direct gold match"),
        judge_pair=WriterJudgeService(
            ArtifactRepository(FilesystemObjectStore(tmp_path))
        ).pending_pair(
            run_id=run_id,
            task_id=StableId("task.rebuild"),
            freeze_receipt_id=freeze.receipt_id,
            response_ref=_ref(),
            availability=WriterJudgeAvailability.UNAVAILABLE,
        ),
    )
    assert qa_report.cited_evidence_count == 1
    assert qa_report.gold_hit_count is None
    assert qa_report.first_failure_layer is BenchmarkFailureLayer.ANSWER_JUDGE


def test_rebuild_counts_unmatched_gold_and_skips_uncited_items(tmp_path: Path) -> None:
    freeze = _freeze()
    _gold, _package_v1, ledger, _freeze_unused = frozen_evaluation_inputs()
    task = _task(profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF, suffix="unused")
    base = _package(task, _ref())
    package = base.model_copy(
        update={
            "items": (
                WriterContextEvidenceItem(
                    item_id=StableId("item.rebuild-unused"),
                    section=WriterContextSection.CURRENT_WORLD_STATE,
                    need_ids=(StableId("need.rebuild-unused"),),
                    need_facet_ids=(StableId("facet.rebuild-unused"),),
                    purpose="unrelated frozen item",
                    evidence_ledger_ids=(StableId("ledger.absent"),),
                    raw_preview="unused",
                ),
            ),
            "budget_report": base.budget_report.model_copy(
                update={"item_count": 1, "evidence_item_count": 1}
            ),
        }
    )
    response = ContextWriterResponse(
        response_version="context_writer_response.v1",
        task_contract=task,
        basis_commit_id=package.basis_commit_id,
        conclusions=(
            ContextWriterConclusion(
                conclusion_id=StableId("conclusion.rebuild-unused"),
                text="unused",
                evidence_refs=ledger.entries[0].evidence_refs,
            ),
        ),
        frozen_before_gold_reveal=False,
    )
    report = rebuild_durable_evidence_report(
        run_id=RunId("run.rebuild-unused"),
        freeze_receipt=freeze,
        ledger_entries=(),
        writer_context=package,
        project_id=PROJECT,
        experiment_id=EXPERIMENT,
        writer_response=response,
        evidence_ledger=ledger,
        gold_match=GoldEvidenceMatch(matched=False, reason="no gold hit"),
        judge_pair=WriterJudgeService(
            ArtifactRepository(FilesystemObjectStore(tmp_path))
        ).record_available_pair(
            run_id=RunId("run.rebuild-unused"),
            task_id=task.task_id,
            freeze_receipt_id=freeze.receipt_id,
            response_ref=_ref(),
            answer_input_ref=_ref(),
            answer_output_ref=_ref(),
            answer_score=0.0,
            evidence_input_ref=_ref(),
            evidence_output_ref=_ref(),
            evidence_score=0.0,
            answer_model_request_id=StableId("model.answer-judge.unused"),
            evidence_model_request_id=StableId("model.evidence-judge.unused"),
        ),
    )
    assert report.writer_used_item_count == 0
    assert report.gold_hit_count == 0
    assert report.first_failure_layer is BenchmarkFailureLayer.WRITER_ANSWER


def test_rebuild_locates_evidence_judge_failure() -> None:
    freeze = _freeze()
    package = _package(_task(profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF), _ref())
    run_id = RunId("run.rebuild-evidence-judge")
    pair = WriterJudgePair(
        task_id=StableId("task.rebuild"),
        answer_judge=WriterJudgeReceipt(
            receipt_id=StableId("writer-judge.answer.evidence-layer"),
            kind=WriterJudgeKind.ANSWER,
            availability=WriterJudgeAvailability.PENDING,
            logical_phase="benchmark.answer_judge",
            run_id=run_id,
            task_id=StableId("task.rebuild"),
            freeze_receipt_id=freeze.receipt_id,
            response_ref=_ref(),
        ),
        evidence_support_judge=WriterJudgeReceipt(
            receipt_id=StableId("writer-judge.evidence.evidence-layer"),
            kind=WriterJudgeKind.EVIDENCE_SUPPORT,
            availability=WriterJudgeAvailability.UNAVAILABLE,
            logical_phase="benchmark.evidence_support_judge",
            run_id=run_id,
            task_id=StableId("task.rebuild"),
            freeze_receipt_id=freeze.receipt_id,
            response_ref=_ref(),
        ),
    )
    report = rebuild_durable_evidence_report(
        run_id=run_id,
        freeze_receipt=freeze,
        ledger_entries=(),
        writer_context=package,
        project_id=PROJECT,
        experiment_id=EXPERIMENT,
        judge_pair=pair,
    )
    assert report.first_failure_layer is BenchmarkFailureLayer.EVIDENCE_JUDGE
