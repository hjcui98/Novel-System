"""Thin Writer-response adapters over the existing Gold evidence matcher.

WCP evaluation already calls ``GoldEvidenceMatcher.match``. Track B Writer
answers reuse that kernel after freeze. Track A only freeze-gates the
``QaWriterResponse``; it does not add a second answer scorer.
"""

from __future__ import annotations

from novel_agent.domain.benchmark import GoldItem, TextRootDocument
from novel_agent.domain.ids import StableId
from novel_agent.domain.memory_benchmark import (
    ContextWriterResponse,
    EvidenceLedger,
    EvidenceLedgerEntry,
    FreezeReceipt,
    QaWriterResponse,
)
from novel_agent.domain.text import EvidenceRef
from novel_agent.domain.writer_context import WriterContextPackage
from novel_agent.services.benchmark_importer import BenchmarkImportError, validate_evidence_ref
from novel_agent.services.gold_evidence_matching import GoldEvidenceMatch, GoldEvidenceMatcher
from novel_agent.services.memory_benchmark_evaluation import MemoryBenchmarkEvaluator


class WriterResponseAdapterError(ValueError):
    """Writer readout is not frozen, or its evidence cannot enter evaluation."""


class WriterContextGoldAdapter:
    """WCP caller: frozen package ledger -> existing GoldEvidenceMatcher."""

    def __init__(self, matcher: GoldEvidenceMatcher | None = None) -> None:
        self._matcher = matcher or GoldEvidenceMatcher()

    def match(
        self,
        *,
        gold: GoldItem,
        package: WriterContextPackage,
        evidence_ledger: EvidenceLedger,
        freeze_receipt: FreezeReceipt,
    ) -> GoldEvidenceMatch:
        MemoryBenchmarkEvaluator._verify_frozen_artifacts(package, evidence_ledger, freeze_receipt)
        return self._matcher.match(gold, evidence_ledger)


class WriterResponseGoldAdapter:
    """Track B caller: frozen Writer conclusions -> the same GoldEvidenceMatcher."""

    def __init__(self, matcher: GoldEvidenceMatcher | None = None) -> None:
        self._matcher = matcher or GoldEvidenceMatcher()

    def match(
        self,
        *,
        response: ContextWriterResponse,
        frozen_ledger: EvidenceLedger,
        freeze_receipt: FreezeReceipt,
        gold: GoldItem,
        history_text: TextRootDocument | None = None,
    ) -> GoldEvidenceMatch:
        ledger = self.writer_ledger(
            response=response,
            frozen_ledger=frozen_ledger,
            freeze_receipt=freeze_receipt,
            history_text=history_text,
        )
        return self._matcher.match(gold, ledger)

    def writer_ledger(
        self,
        *,
        response: ContextWriterResponse,
        frozen_ledger: EvidenceLedger,
        freeze_receipt: FreezeReceipt,
        history_text: TextRootDocument | None = None,
    ) -> EvidenceLedger:
        if not freeze_receipt.frozen_before_reveal:
            raise WriterResponseAdapterError("readout must freeze before Gold reveal")
        if not response.frozen_before_gold_reveal:
            raise WriterResponseAdapterError("Writer answer was not frozen before Gold reveal")
        entries: list[EvidenceLedgerEntry] = []
        for conclusion in response.conclusions:
            resolved = tuple(
                self._resolve_ref(ref, frozen_ledger, history_text)
                for ref in conclusion.evidence_refs
            )
            template = frozen_ledger.entries[0] if frozen_ledger.entries else None
            scope = template.information_scope if template is not None else "writer_safe"
            entries.append(
                EvidenceLedgerEntry(
                    ledger_id=StableId(f"writer.{conclusion.conclusion_id.root}"[:128]),
                    evidence_refs=resolved,
                    claim_excerpt=conclusion.text,
                    source_commit=response.basis_commit_id,
                    information_scope=scope,
                )
            )
        return EvidenceLedger(
            contract_version=frozen_ledger.contract_version,
            entries=tuple(entries),
            rendered_tokens=sum(len(entry.claim_excerpt) for entry in entries),
        )

    def _resolve_ref(
        self,
        actual: EvidenceRef,
        frozen_ledger: EvidenceLedger,
        history_text: TextRootDocument | None,
    ) -> EvidenceRef:
        if self._matcher.ledger_contains(actual, frozen_ledger):
            return actual
        if history_text is None:
            raise WriterResponseAdapterError("Writer evidence is not in the frozen ledger")
        try:
            validate_evidence_ref(actual, history_text)
        except BenchmarkImportError as error:
            raise WriterResponseAdapterError(
                "Writer evidence is not in the frozen ledger"
            ) from error
        return actual


class QaWriterResponseAdapter:
    """Track A caller: freeze-gate QA answers without a second scorer."""

    def adapt(
        self,
        *,
        response: QaWriterResponse,
        freeze_receipt: FreezeReceipt,
        checkpoint_chapter: int,
        gold_revealed: bool = False,
    ) -> QaWriterResponse:
        if gold_revealed:
            raise WriterResponseAdapterError("readout must freeze before Gold reveal")
        if not freeze_receipt.frozen_before_reveal:
            raise WriterResponseAdapterError("readout must freeze before Gold reveal")
        for item in response.evidence:
            if item.chapter > checkpoint_chapter:
                raise WriterResponseAdapterError("QA evidence chapter is after the freeze")
        return response
