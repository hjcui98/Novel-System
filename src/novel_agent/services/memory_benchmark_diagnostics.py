"""Evaluator-only evidence stage-loss diagnostics for frozen Stage 2M artifacts."""

from __future__ import annotations

from collections.abc import Iterable

from novel_agent.domain.benchmark import GoldItem
from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import (
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1ContextPackage,
)
from novel_agent.domain.memory_benchmark import (
    BenchmarkInformationProfile,
    EvidenceLedger,
    EvidenceLedgerEntry,
    EvidenceStageFailure,
    PerGoldStageLossDiagnostic,
)
from novel_agent.services.gold_evidence_matching import GoldEvidenceMatcher


class StageLossDiagnosticBuilder:
    """Compare accepted evidence only after freeze/reveal; never mutate runtime artifacts."""

    version = "stage_loss_diagnostic.v1"

    def __init__(self, matcher: GoldEvidenceMatcher | None = None) -> None:
        self._matcher = matcher or GoldEvidenceMatcher()

    def build(
        self,
        *,
        gold_items: tuple[GoldItem, ...],
        profile: BenchmarkInformationProfile,
        stage1_context: Stage1ContextPackage,
        writer_ledger: EvidenceLedger,
    ) -> tuple[PerGoldStageLossDiagnostic, ...]:
        candidate_units = self._unique_units(
            candidate.unit
            for trace in stage1_context.retrieval_traces
            for candidate in trace.candidates
        )
        rank_selected_units = self._unique_units(
            candidate.unit
            for trace in stage1_context.retrieval_traces
            for candidate in trace.candidates
            if candidate.selected
        )
        stage1_selected_units = self._unique_units(self._context_units(stage1_context))
        candidate_ledger = self._ledger(candidate_units, stage1_context)
        rank_selected_ledger = self._ledger(rank_selected_units, stage1_context)
        stage1_selected_ledger = self._ledger(stage1_selected_units, stage1_context)

        diagnostics: list[PerGoldStageLossDiagnostic] = []
        for gold in gold_items:
            if profile not in gold.applicable_profiles:
                continue
            candidate = self._matcher.coverage(gold, candidate_ledger)
            rank_selected = self._matcher.coverage(gold, rank_selected_ledger)
            stage1_selected = self._matcher.coverage(gold, stage1_selected_ledger)
            writer = self._matcher.coverage(gold, writer_ledger)
            diagnostics.append(
                PerGoldStageLossDiagnostic(
                    gold_id=gold.gold_id,
                    candidate=candidate,
                    rank_selected=rank_selected,
                    stage1_selected=stage1_selected,
                    writer_ledger=writer,
                    primary_failure=self._failure(
                        candidate_complete=bool(candidate.complete_alternative_ids),
                        rank_complete=bool(rank_selected.complete_alternative_ids),
                        stage1_complete=bool(stage1_selected.complete_alternative_ids),
                        writer_complete=bool(writer.complete_alternative_ids),
                    ),
                )
            )
        return tuple(diagnostics)

    @staticmethod
    def _failure(
        *,
        candidate_complete: bool,
        rank_complete: bool,
        stage1_complete: bool,
        writer_complete: bool,
    ) -> EvidenceStageFailure:
        if writer_complete:
            return EvidenceStageFailure.COMPLETE
        if stage1_complete or rank_complete:
            return EvidenceStageFailure.F_ASSEMBLY
        if candidate_complete:
            return EvidenceStageFailure.F_RANK
        return EvidenceStageFailure.F_NEED_ROUTE_RETRIEVE

    @staticmethod
    def _unique_units(units: Iterable[RetrievalUnit]) -> tuple[RetrievalUnit, ...]:
        by_id: dict[StableId, RetrievalUnit] = {}
        for unit in units:
            by_id.setdefault(unit.unit_id, unit)
        return tuple(by_id.values())

    @staticmethod
    def _context_units(context: Stage1ContextPackage) -> tuple[RetrievalUnit, ...]:
        return (
            *context.mandatory_constraints,
            *context.current_world_state,
            *context.active_plan_obligations,
            *context.relevant_historical_events,
            *context.truth_and_knowledge_boundaries,
            *context.raw_evidence_spans,
            *context.style_or_reference_optional,
        )

    @staticmethod
    def _ledger(
        units: tuple[RetrievalUnit, ...],
        context: Stage1ContextPackage,
    ) -> EvidenceLedger:
        entries: list[EvidenceLedgerEntry] = []
        for index, unit in enumerate(units):
            plan_node_ids = (
                (StableId(unit.unit_id.root.removeprefix("anchor.")),)
                if unit.unit_kind in {RetrievalUnitKind.PLAN_ANCHOR, RetrievalUnitKind.ARC_ANCHOR}
                and not unit.evidence_refs
                else ()
            )
            if not unit.evidence_refs and not plan_node_ids:
                continue
            entries.append(
                EvidenceLedgerEntry(
                    ledger_id=StableId(f"ledger.diagnostic.{index}.{unit.unit_id.root}"[:128]),
                    evidence_refs=unit.evidence_refs,
                    plan_node_ids=plan_node_ids,
                    claim_excerpt=unit.text[:240] or unit.unit_id.root,
                    source_commit=context.base_commit,
                    information_scope=unit.access_scope,
                    retrieval_unit_ids=(unit.unit_id,),
                )
            )
        return EvidenceLedger(
            contract_version="evidence_ledger.diagnostic.v1",
            entries=tuple(entries),
            rendered_tokens=0,
        )


__all__ = ["StageLossDiagnosticBuilder"]
