"""Rebuild benchmark usage reports from ledger, freeze receipts, and artifacts.

Counts are derived from durable evidence. Callers do not pass request, item,
citation, or Gold-hit totals.
"""

from __future__ import annotations

from novel_agent.domain.artifacts import DURABLE_EVIDENCE_REPORT_MEDIA_TYPE, ArtifactRef
from novel_agent.domain.ids import ProjectId, RunId, SchemaVersion, StableId
from novel_agent.domain.memory_benchmark import (
    ContextWriterResponse,
    EvidenceLedger,
    QaWriterResponse,
)
from novel_agent.domain.model_calls import (
    ModelCallLedgerEntry,
    ModelCallLedgerStatus,
    ModelCostAvailability,
)
from novel_agent.domain.v05_readout import (
    BenchmarkFailureLayer,
    DurableEvidenceReport,
    WriterJudgeAvailability,
    WriterJudgePair,
)
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    ContextAssemblyStatus,
    FreezeReceipt,
    WriterContextPackageV2,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.gold_evidence_matching import GoldEvidenceMatch
from novel_agent.services.memory_benchmark_contract import profile_namespace
from novel_agent.services.model_call_ledger import aggregate_model_calls, summarize_model_cost

REPORT_SCHEMA_VERSION = SchemaVersion("1.0.0")
_WRITER_PHASES = frozenset({"benchmark.writer_qa_readout", "benchmark.writer_context_readout"})


class DurableReportRebuildError(ValueError):
    """The durable evidence is incomplete or internally inconsistent."""


def rebuild_durable_evidence_report(
    *,
    run_id: RunId,
    freeze_receipt: FreezeReceipt,
    ledger_entries: tuple[ModelCallLedgerEntry, ...],
    writer_context: WriterContextPackageV2,
    project_id: ProjectId,
    experiment_id: str,
    writer_response: ContextWriterResponse | QaWriterResponse | None = None,
    evidence_ledger: EvidenceLedger | None = None,
    gold_match: GoldEvidenceMatch | None = None,
    judge_pair: WriterJudgePair | None = None,
    extra_profiles: tuple[BenchmarkInformationProfile, ...] = (),
) -> DurableEvidenceReport:
    if not freeze_receipt.frozen_before_reveal:
        raise DurableReportRebuildError("report rebuild requires a freeze receipt")
    if any(entry.run_id != run_id for entry in ledger_entries):
        raise DurableReportRebuildError("ledger entries belong to another run")
    aggregates = aggregate_model_calls(ledger_entries)
    records = tuple(entry.call_record for entry in ledger_entries if entry.call_record is not None)
    _cost, cost_availability = summarize_model_cost(records)
    if not records:
        cost_availability = ModelCostAvailability.NOT_APPLICABLE
    answer_availability = (
        WriterJudgeAvailability.PENDING
        if judge_pair is None
        else judge_pair.answer_judge.availability
    )
    evidence_availability = (
        WriterJudgeAvailability.PENDING
        if judge_pair is None
        else judge_pair.evidence_support_judge.availability
    )
    profiles = (
        writer_context.task_contract.information_profile,
        *extra_profiles,
    )
    namespaces = tuple(
        dict.fromkeys(profile_namespace(project_id, profile, experiment_id) for profile in profiles)
    )
    cited = _cited_evidence_count(writer_response)
    gold_hits = _gold_hit_count(evidence_availability, gold_match)
    return DurableEvidenceReport(
        run_id=run_id,
        freeze_receipt_id=freeze_receipt.receipt_id,
        phase_aggregates=aggregates,
        profile_namespaces=namespaces,
        writer_context_item_count=len(writer_context.items),
        writer_used_item_count=_used_item_count(
            writer_context,
            writer_response,
            evidence_ledger,
        ),
        cited_evidence_count=cited,
        gold_hit_count=gold_hits,
        first_failure_layer=_first_failure_layer(
            ledger_entries,
            writer_context=writer_context,
            writer_response=writer_response,
            answer_availability=answer_availability,
            evidence_availability=evidence_availability,
        ),
        answer_judge_availability=answer_availability,
        evidence_judge_availability=evidence_availability,
        cost_availability=cost_availability,
    )


def persist_durable_evidence_report(
    artifacts: ArtifactRepository,
    report: DurableEvidenceReport,
) -> ArtifactRef:
    return artifacts.put(
        canonical_json_bytes(report.model_dump(mode="json")),
        DURABLE_EVIDENCE_REPORT_MEDIA_TYPE,
        REPORT_SCHEMA_VERSION,
    )


def _cited_evidence_count(
    response: ContextWriterResponse | QaWriterResponse | None,
) -> int:
    if isinstance(response, ContextWriterResponse):
        refs = tuple(ref for conclusion in response.conclusions for ref in conclusion.evidence_refs)
        return len(dict.fromkeys(refs))
    if isinstance(response, QaWriterResponse):
        return len(response.evidence)
    return 0


def _used_item_count(
    package: WriterContextPackageV2,
    response: ContextWriterResponse | QaWriterResponse | None,
    evidence_ledger: EvidenceLedger | None,
) -> int:
    if not isinstance(response, ContextWriterResponse) or evidence_ledger is None:
        return 0
    cited = {ref for conclusion in response.conclusions for ref in conclusion.evidence_refs}
    used: list[StableId] = []
    for item in package.items:
        for entry in evidence_ledger.entries:
            if entry.ledger_id not in item.evidence_ledger_ids:
                continue
            if any(ref in cited for ref in entry.evidence_refs):
                used.append(item.item_id)
                break
    return len(set(used))


def _gold_hit_count(
    availability: WriterJudgeAvailability,
    gold_match: GoldEvidenceMatch | None,
) -> int | None:
    if availability in {
        WriterJudgeAvailability.PENDING,
        WriterJudgeAvailability.UNAVAILABLE,
    }:
        return None
    if gold_match is None:
        return 0
    if gold_match.matched_evidence_set_ids:
        return len(gold_match.matched_evidence_set_ids)
    return 1 if gold_match.matched else 0


def _valid_writer_answer(
    response: ContextWriterResponse | QaWriterResponse | None,
) -> bool:
    if isinstance(response, ContextWriterResponse):
        return response.frozen_before_gold_reveal
    return isinstance(response, QaWriterResponse)


def _first_failure_layer(
    entries: tuple[ModelCallLedgerEntry, ...],
    *,
    writer_context: WriterContextPackageV2,
    writer_response: ContextWriterResponse | QaWriterResponse | None,
    answer_availability: WriterJudgeAvailability,
    evidence_availability: WriterJudgeAvailability,
) -> BenchmarkFailureLayer:
    statuses = {entry.status for entry in entries}
    if ModelCallLedgerStatus.TRANSPORT_EXHAUSTED in statuses:
        return BenchmarkFailureLayer.TRANSPORT
    if ModelCallLedgerStatus.UNCERTAIN in statuses:
        return BenchmarkFailureLayer.RAW
    if ModelCallLedgerStatus.VALIDATION_REJECTED in statuses:
        return BenchmarkFailureLayer.PARSE
    if writer_context.budget_report.final_status is not ContextAssemblyStatus.READY:
        return BenchmarkFailureLayer.PACKAGE
    writer_completed = any(
        entry.status is ModelCallLedgerStatus.COMPLETED and entry.logical_phase in _WRITER_PHASES
        for entry in entries
    )
    if writer_completed and not _valid_writer_answer(writer_response):
        return BenchmarkFailureLayer.WRITER_ANSWER
    if writer_response is not None and not _valid_writer_answer(writer_response):
        return BenchmarkFailureLayer.WRITER_ANSWER
    if answer_availability is WriterJudgeAvailability.UNAVAILABLE:
        return BenchmarkFailureLayer.ANSWER_JUDGE
    if evidence_availability is WriterJudgeAvailability.UNAVAILABLE:
        return BenchmarkFailureLayer.EVIDENCE_JUDGE
    return BenchmarkFailureLayer.NONE
