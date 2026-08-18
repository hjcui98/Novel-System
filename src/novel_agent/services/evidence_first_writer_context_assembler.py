"""Evidence-first Writer Context package assembly (ADR-0008, ``writer_context.v2``).

The default Stage 2M read-side product stops at evidence selection and packing:
selected exact L0 slices become a WriterContextPackage v2 plus a bound
EvidenceLedger v2.  No Claim proposal, multi-slice synthesis, whole-claim
verifier, semantic receipt or evaluator request is created here; a READY
package requires only ledger-backed evidence items or typed gaps.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Literal, cast

from pydantic import Field

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    ExpectedClaimScope,
    FacetEvidenceReceipt,
    NeedFacet,
    RequirementLevel,
    Stage1MemoryNeed,
)
from novel_agent.domain.stage2 import ContextAssemblySpec  # noqa: F401  (legacy import parity)
from novel_agent.domain.text import EvidenceRef, TextBlock
from novel_agent.domain.writer_context import (
    BenchmarkTaskContract,
    ContextAssemblyStatus,
    EvidenceFirstGap,
    EvidenceFirstLineage,
    EvidenceGapKind,
    EvidenceLedgerEntryV2,
    EvidenceLedgerV2,
    EvidenceSlice,
    NeedEvidenceJudgmentBatchReceipt,
    NeedEvidenceSemanticStatus,
    NeedFacetSemanticReceipt,
    UnresolvedLexicalAnchor,
    WriterContextBudgetReportV2,
    WriterContextEvidenceItem,
    WriterContextPackageV2,
    WriterContextSection,
    WriterContextValidity,
)
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes, quote_hash
from novel_agent.services.evidence_slice_resolver import EvidenceSliceResolver
from novel_agent.services.memory_benchmark_contract import assert_safe_public_payload

TokenCounter = Callable[[str], int]

PREVIEW_CHAR_LIMIT = 400


class SliceSelectionTrace(DomainModel):
    """One selected slice plus its mechanical retrieval selection trace."""

    slice_id: StableId
    unit_id: StableId
    route_channel: str = Field(min_length=1)
    fused_rank: int = Field(ge=1)
    rerank_score: float | None = Field(default=None, ge=0.0, le=1.0)
    selection_reason: str = Field(min_length=1)
    evidence_ref: EvidenceRef | None = None
    # Facet ids of the public Need this slice's retrieval unit can serve with
    # exact evidence; the package gap report and route receipts share this.
    supported_facet_ids: tuple[StableId, ...] = ()


class NeedEvidenceSelection(DomainModel):
    """Selected exact slices and traces for one public Need."""

    need: Stage1MemoryNeed
    selections: tuple[SliceSelectionTrace, ...] = ()
    slices: tuple[EvidenceSlice, ...] = ()
    facet_receipts: tuple[FacetEvidenceReceipt, ...] = ()
    semantic_receipts: tuple[NeedFacetSemanticReceipt, ...] = ()
    semantic_batch_receipts: tuple[NeedEvidenceJudgmentBatchReceipt, ...] = ()


class EvidenceFirstAssemblyResult(DomainModel):
    status: ContextAssemblyStatus
    package: WriterContextPackageV2
    evidence_ledger: EvidenceLedgerV2
    diagnostic_codes: tuple[str, ...] = ()
    mechanical_failure_counts: dict[str, int] = Field(default_factory=dict)
    # Required, no success default: the assembler always computes it
    # (2026-08-14 review follow-up P1).
    mandatory_facet_closure: Literal["COMPLETE", "INCOMPLETE"]
    structural_mandatory_facet_closure: Literal["COMPLETE", "INCOMPLETE"] = "INCOMPLETE"
    semantic_status: Literal["COMPLETE", "INCOMPLETE", "UNASSESSED"] = "UNASSESSED"
    usable_with_gaps: bool = True
    unclosed_mandatory_need_facets: tuple[StableId, ...] = ()
    semantic_receipts: tuple[NeedFacetSemanticReceipt, ...] = ()
    semantic_batch_receipts: tuple[NeedEvidenceJudgmentBatchReceipt, ...] = ()
    assembler_version: str = Field(min_length=1)


class EvidenceFirstWriterContextAssembler:
    """Pack selected exact slices into a READY evidence-first package + ledger."""

    version = "evidence_first_writer_context_assembler.v1"
    contract_version = "writer_context.v2"
    ledger_contract_version = "evidence_ledger.v2"

    def __init__(
        self,
        *,
        token_counter: TokenCounter | None = None,
        tokenizer_name: str = "deterministic_unicode",
        tokenizer_version: str = "v1",
        resolver: EvidenceSliceResolver | None = None,
    ) -> None:
        self._count = token_counter or self._default_token_count
        self._tokenizer_name = tokenizer_name
        self._tokenizer_version = tokenizer_version
        self._resolver = resolver or EvidenceSliceResolver()

    def count_tokens(self, text: str) -> int:
        return self._count(text)

    def assemble(
        self,
        *,
        task: BenchmarkTaskContract,
        selections: tuple[NeedEvidenceSelection, ...],
        text_root: TextRootDocument,
        basis_commit_id: CommitId,
        basis_snapshot_id: StableId,
        arm: str = "A",
        writer_token_budget: int = 4000,
        evidence_ledger_token_budget: int = 12_000,
        grounder_version: str = "",
        validator_version: str = "",
        generator_version: str = "",
        query_compiler_version: str = "",
        route_plan_version: str = "",
        planner_artifact_ref: ArtifactRef | None = None,
        planner_artifact_hash: ArtifactId | None = None,
        planner_fallback_used: bool = False,
        unresolved_lexical_anchors: tuple[UnresolvedLexicalAnchor, ...] = (),
    ) -> EvidenceFirstAssemblyResult:
        """Build a v2 package + ledger from selected exact slices only.

        Fail-closed invariants:
        - every slice is re-verified against the immutable text root before it
          may enter the ledger (dereference_receipt ``verified_read``);
        - every item carries ledger refs or a typed gap;
        - every exposed ledger entry binds at least one public Need;
        - previews are bounded prefixes of exact ledger text with an explicit
          truncation flag; no model rewriting or Gold/future reading.
        """
        if arm not in {"A", "B", "C"}:
            raise ValueError("evidence-first writer context arm must be A, B, or C")
        if writer_token_budget < 1 or evidence_ledger_token_budget < 1:
            raise ValueError("writer and ledger budgets must be positive")
        if not selections:
            raise ValueError("evidence-first assembly requires at least one Need selection")
        blocks = self._blocks(text_root)
        need_ids = tuple(selection.need.need_id for selection in selections)
        if len(need_ids) != len(set(need_ids)):
            raise ValueError("evidence-first selections must be unique by Need")
        diagnostics: list[str] = []
        mechanical_failure_counts: dict[str, int] = {
            "dereference": 0,
            "scope": 0,
            "cutoff": 0,
        }
        need_by_id = {selection.need.need_id: selection.need for selection in selections}
        ledger_by_span: dict[tuple[str, int, int], EvidenceLedgerEntryV2] = {}
        ledger_order: list[tuple[str, int, int]] = []
        dropped_slice_reasons: dict[str, str] = {}

        ledger_tokens = 0

        def add_ledger_entry(
            span_key: tuple[str, int, int],
            slice_: EvidenceSlice,
            evidence_refs: tuple[EvidenceRef, ...],
            unit_ids: tuple[StableId, ...],
            need_id: StableId,
            facet_ids: tuple[StableId, ...],
        ) -> StableId:
            nonlocal ledger_tokens
            existing = ledger_by_span.get(span_key)
            if existing is not None:
                merged = existing.model_copy(
                    update={
                        "need_ids": tuple(dict.fromkeys((*existing.need_ids, need_id))),
                        "need_facet_ids": tuple(
                            dict.fromkeys((*existing.need_facet_ids, *facet_ids))
                        ),
                        "retrieval_unit_ids": tuple(
                            dict.fromkeys((*existing.retrieval_unit_ids, *unit_ids))
                        ),
                        "evidence_refs": tuple(
                            dict.fromkeys((*existing.evidence_refs, *evidence_refs))
                        ),
                    }
                )
                ledger_by_span[span_key] = merged
                return merged.ledger_id
            entry_id = StableId(f"ledger.evidence.{slice_.slice_id.root}"[:128])
            text_hash = sha256_id(slice_.text.encode("utf-8"))
            entry = EvidenceLedgerEntryV2(
                ledger_id=entry_id,
                evidence_slices=(slice_,),
                evidence_text=slice_.text,
                evidence_refs=evidence_refs,
                retrieval_unit_ids=unit_ids,
                basis_commit_id=basis_commit_id,
                basis_snapshot_id=basis_snapshot_id,
                cutoff_chapter=task.checkpoint_chapter,
                information_scope=slice_.access_scope,
                taint="none",
                text_hash=text_hash,
                span_hash=self._span_hash(slice_),
                quote_hash=slice_.quote_hash,
                dereference_receipt="verified_read",
                need_ids=(need_id,),
                need_facet_ids=facet_ids,
            )
            ledger_by_span[span_key] = entry
            ledger_order.append(span_key)
            ledger_tokens += self._count(entry.evidence_text)
            return entry_id

        # Per-Need ordered packing plans: (trace, ledger id, served facet ids).
        need_plans: dict[
            StableId,
            tuple[tuple[SliceSelectionTrace, StableId, tuple[StableId, ...]], ...],
        ] = {}
        for selection in selections:
            need = selection.need
            seen_spans: dict[tuple[str, int, int], SliceSelectionTrace] = {}
            slice_by_id = {slice_.slice_id: slice_ for slice_ in selection.slices}
            for trace in selection.selections:
                slice_ = slice_by_id.get(trace.slice_id)
                if slice_ is None:
                    diagnostics.append(f"SELECTED_SLICE_MISSING:{trace.slice_id.root}")
                    continue
                reverified, rejection = self._reverify_slice(slice_, blocks, task, need)
                if reverified is None:
                    reason = rejection or "dereference_failed"
                    mechanical_failure_counts[reason.removesuffix("_failed")] += 1
                    dropped_slice_reasons[trace.slice_id.root] = reason
                    diagnostics.append(f"SLICE_REJECTED:{reason}:{trace.slice_id.root}")
                    continue
                span_key = self._span_key(slice_)
                previous = seen_spans.get(span_key)
                if previous is None or trace.fused_rank < previous.fused_rank:
                    seen_spans[span_key] = trace
            ordered = tuple(
                sorted(
                    seen_spans.values(),
                    key=lambda trace: (trace.fused_rank, trace.slice_id.root),
                )
            )
            plans: list[tuple[SliceSelectionTrace, StableId, tuple[StableId, ...]]] = []
            for trace in ordered:
                slice_ = slice_by_id[trace.slice_id]
                span_key = self._span_key(slice_)
                evidence_refs = (trace.evidence_ref,) if trace.evidence_ref is not None else ()
                unit_ids = (trace.unit_id,)
                # Facet support is predicate-bound (2026-08-14 review P1): an
                # entry serves exactly the facets its unit's predicate
                # established.  An empty supported set is an honest "no
                # semantic support" and must NOT fall back to serving every
                # facet of the Need (that was the blanket-closure false
                # success).
                entry_facet_ids = trace.supported_facet_ids
                entry_id = add_ledger_entry(
                    span_key,
                    slice_,
                    evidence_refs,
                    unit_ids,
                    need.need_id,
                    entry_facet_ids,
                )
                plans.append((trace, entry_id, entry_facet_ids))
            need_plans[need.need_id] = tuple(plans)

        required_facets: dict[StableId, tuple[StableId, ...]] = {
            selection.need.need_id: (
                selection.need.completion_spec.required_need_facet_ids
                if selection.need.completion_spec is not None
                else tuple(facet.need_facet_id for facet in selection.need.need_facets)
            )
            for selection in selections
        }
        mandatory_need_ids = {
            selection.need.need_id
            for selection in selections
            if selection.need.requirement is RequirementLevel.MANDATORY
        }
        # Pass 1: round-robin over Needs, one minimal exact slice per required
        # facet, so early Needs cannot starve later mandatory facets of ledger
        # tokens.  Pass 2: remaining slices by Need priority and route rank.
        packed_entries: dict[StableId, list[StableId]] = {need_id: [] for need_id in need_plans}
        packed_span_keys: set[tuple[str, int, int]] = set()
        used_writer_tokens = 0
        used_ledger_tokens = 0
        budget_exhausted = False

        def span_key_for(entry_id: StableId) -> tuple[str, int, int]:
            return next(
                span_key
                for span_key, entry in ledger_by_span.items()
                if entry.ledger_id == entry_id
            )

        def new_ledger_tokens(entry_id: StableId) -> int:
            span_key = span_key_for(entry_id)
            if span_key in packed_span_keys:
                return 0
            return self._count(ledger_by_span[span_key].evidence_text)

        def item_tokens(need: Stage1MemoryNeed, entry_ids: tuple[StableId, ...]) -> int:
            texts = tuple(
                ledger_by_span[span_key_for(entry_id)].evidence_text for entry_id in entry_ids
            )
            preview, truncated = self._preview(texts)
            section = (need.expected_section or WriterContextSection.CONTINUITY_CONSTRAINTS).value
            return self._count(
                f"[{section}] {self._purpose(need)}\n"
                f"  {preview}{' [truncated]' if truncated else ''}"
            )

        def fits(need: Stage1MemoryNeed, entry_id: StableId) -> bool:
            nonlocal used_writer_tokens, used_ledger_tokens
            cost_writer = item_tokens(need, (*packed_entries[need.need_id], entry_id)) - (
                item_tokens(need, tuple(packed_entries[need.need_id]))
                if packed_entries[need.need_id]
                else 0
            )
            cost_ledger = new_ledger_tokens(entry_id)
            if (
                used_writer_tokens + cost_writer <= writer_token_budget
                and used_ledger_tokens + cost_ledger <= evidence_ledger_token_budget
            ):
                used_writer_tokens += cost_writer
                used_ledger_tokens += cost_ledger
                span_key = span_key_for(entry_id)
                packed_span_keys.add(span_key)
                packed_entries[need.need_id].append(entry_id)
                return True
            return False

        satisfied_facets: dict[StableId, set[StableId]] = {need_id: set() for need_id in need_plans}
        # Best plan index per (need, facet); facets without any serving plan
        # stay unsatisfied and become typed gaps.
        facet_plan_index: dict[tuple[StableId, StableId], int] = {}
        for need_id, need_plans_for_need in need_plans.items():
            for facet_id in required_facets.get(need_id, ()):
                index = next(
                    (
                        index
                        for index, plan in enumerate(need_plans_for_need)
                        if facet_id in plan[2]
                    ),
                    None,
                )
                if index is not None:
                    facet_plan_index[(need_id, facet_id)] = index

        need_order = tuple(
            sorted(
                need_plans,
                key=lambda need_id: (
                    need_id not in mandatory_need_ids,
                    -need_by_id[need_id].priority,
                    need_id.root,
                ),
            )
        )
        # Pass 1: round-robin cycles, one facet slice per Need per cycle.
        while not budget_exhausted:
            progressed = False
            for need_id in need_order:
                if budget_exhausted:
                    break
                next_facet = next(
                    (
                        facet_id
                        for facet_id in required_facets.get(need_id, ())
                        if facet_id not in satisfied_facets[need_id]
                        and (need_id, facet_id) in facet_plan_index
                    ),
                    None,
                )
                if next_facet is None:
                    continue
                plan = need_plans[need_id][facet_plan_index[(need_id, next_facet)]]
                if fits(need_by_id[need_id], plan[1]):
                    satisfied_facets[need_id].update(
                        facet_id
                        for facet_id in plan[2]
                        if facet_id in required_facets.get(need_id, ())
                    )
                    progressed = True
                else:
                    budget_exhausted = True
            if not progressed:
                break
        # Pass 2: remaining slices by Need priority then route rank.
        if not budget_exhausted:
            for need_id in need_order:
                if budget_exhausted:
                    break
                for plan in need_plans[need_id]:
                    if plan[1] in packed_entries[need_id]:
                        continue
                    if fits(need_by_id[need_id], plan[1]):
                        satisfied_facets[need_id].update(
                            facet_id
                            for facet_id in plan[2]
                            if facet_id in required_facets.get(need_id, ())
                        )
                    else:
                        budget_exhausted = True
                        break

        items: list[WriterContextEvidenceItem] = []
        for selection in selections:
            need = selection.need
            entries = packed_entries.get(need.need_id, ())
            facet_ids = tuple(facet.need_facet_id for facet in need.need_facets)
            semantic_by_facet = {
                receipt.need_facet_id: receipt
                for receipt in selection.semantic_receipts
                if receipt.need_id == need.need_id
            }
            if not entries:
                gap = EvidenceFirstGap(
                    gap_id=StableId(f"gap.{need.need_id.root}.no-evidence"[:128]),
                    need_ids=(need.need_id,),
                    need_facet_ids=facet_ids,
                    kind=EvidenceGapKind.NO_SELECTED_EVIDENCE,
                    reason="no verified exact slice was selected for this public Need",
                )
                items.append(
                    WriterContextEvidenceItem(
                        item_id=StableId(f"context-item.{need.need_id.root}"[:128]),
                        section=need.expected_section
                        or WriterContextSection.CONTINUITY_CONSTRAINTS,
                        need_ids=(need.need_id,),
                        need_facet_ids=facet_ids,
                        purpose=self._purpose(need),
                        mandatory=need.requirement is RequirementLevel.MANDATORY,
                        semantic_status=(
                            NeedEvidenceSemanticStatus.UNRESOLVED if semantic_by_facet else None
                        ),
                        gap=gap,
                    )
                )
                continue
            texts = tuple(
                ledger_by_span[span_key_for(entry_id)].evidence_text for entry_id in entries
            )
            preview, truncated = self._preview(texts)
            top = next(
                trace
                for trace, _entry_id, _facets in need_plans[need.need_id]
                if _entry_id == entries[0]
            )
            validity = self._validity_from_facets(need.need_facets)
            item_semantic_status = self._item_semantic_status(need, semantic_by_facet)
            slice_to_entry = {
                trace.slice_id: entry_id for trace, entry_id, _facets in need_plans[need.need_id]
            }
            answering_ids = tuple(
                dict.fromkeys(
                    slice_to_entry[slice_id]
                    for receipt in semantic_by_facet.values()
                    for slice_id in receipt.supporting_slice_ids
                    if slice_id in slice_to_entry
                )
            )
            answering_set = set(answering_ids)
            partial_ids = tuple(
                dict.fromkeys(
                    slice_to_entry[slice_id]
                    for receipt in semantic_by_facet.values()
                    for slice_id in receipt.partial_slice_ids
                    if slice_id in slice_to_entry
                )
            )
            partial_set = set(partial_ids)
            related_ids = tuple(
                entry_id
                for entry_id in entries
                if entry_id not in answering_set and entry_id not in partial_set
            )
            items.append(
                WriterContextEvidenceItem(
                    item_id=StableId(f"context-item.{need.need_id.root}"[:128]),
                    section=need.expected_section or WriterContextSection.CONTINUITY_CONSTRAINTS,
                    need_ids=(need.need_id,),
                    need_facet_ids=facet_ids,
                    purpose=self._purpose(need),
                    evidence_ledger_ids=tuple(dict.fromkeys(entries)),
                    raw_preview=preview,
                    preview_truncated=truncated,
                    source_scope=need.access_scope,
                    source_kind=need.need_type,
                    validity=validity,
                    mandatory=need.requirement is RequirementLevel.MANDATORY,
                    semantic_status=item_semantic_status,
                    semantic_answering_ledger_ids=answering_ids,
                    semantic_partial_ledger_ids=partial_ids,
                    semantic_related_ledger_ids=related_ids,
                    selection_reason=(
                        f"route={top.route_channel};fused_rank={top.fused_rank};"
                        f"{top.selection_reason}"
                    ),
                )
            )
            # Per-facet typed gaps for mandatory facets with no exact evidence,
            # using the same facet language the route recorded.
            if need.need_id in mandatory_need_ids:
                for facet in need.need_facets:
                    structural_supported = facet.need_facet_id in satisfied_facets.get(
                        need.need_id, ()
                    )
                    semantic_receipt = semantic_by_facet.get(facet.need_facet_id)
                    if semantic_by_facet:
                        if (
                            semantic_receipt is not None
                            and semantic_receipt.status is NeedEvidenceSemanticStatus.SUPPORTED
                        ):
                            continue
                        semantic_kind = {
                            NeedEvidenceSemanticStatus.PARTIAL: EvidenceGapKind.SEMANTIC_PARTIAL,
                            NeedEvidenceSemanticStatus.UNSUPPORTED: (
                                EvidenceGapKind.SEMANTIC_UNSUPPORTED
                            ),
                            NeedEvidenceSemanticStatus.UNRESOLVED: (
                                EvidenceGapKind.SEMANTIC_UNRESOLVED
                            ),
                        }.get(
                            semantic_receipt.status
                            if semantic_receipt is not None
                            else NeedEvidenceSemanticStatus.UNRESOLVED,
                            EvidenceGapKind.SEMANTIC_UNRESOLVED,
                        )
                        reason = (
                            semantic_receipt.reason
                            if semantic_receipt is not None and semantic_receipt.reason
                            else "semantic evidence judgment did not close this mandatory facet"
                        )
                    else:
                        if structural_supported:
                            continue
                        semantic_kind = EvidenceGapKind.NO_SELECTED_EVIDENCE
                        reason = "mandatory facet closed by no exact L0 evidence"
                    gap = EvidenceFirstGap(
                        gap_id=StableId(
                            f"gap.{need.need_id.root}.facet.{facet.facet_kind.value}"[:128]
                        ),
                        need_ids=(need.need_id,),
                        need_facet_ids=(facet.need_facet_id,),
                        kind=semantic_kind,
                        reason=reason,
                    )
                    items.append(
                        WriterContextEvidenceItem(
                            item_id=StableId(
                                f"context-item.{need.need_id.root}.{facet.facet_kind.value}"[:128]
                            ),
                            section=need.expected_section
                            or WriterContextSection.CONTINUITY_CONSTRAINTS,
                            need_ids=(need.need_id,),
                            need_facet_ids=(facet.need_facet_id,),
                            purpose=f"{self._purpose(need)} [{facet.facet_kind.value}]",
                            mandatory=True,
                            gap=gap,
                        )
                    )
                    diagnostics.append(f"MANDATORY_FACET_GAP:{facet.facet_kind.value}")

        ordered_items = tuple(
            sorted(
                items,
                key=lambda item: (
                    item.mandatory is not True,
                    -self._need_priority(need_by_id, item),
                    item.need_ids[0].root,
                ),
            )
        )
        packed, dropped = self._pack_items(
            ordered_items,
            ledger_by_span,
            writer_token_budget=writer_token_budget,
            ledger_budget=evidence_ledger_token_budget,
        )
        for dropped_item in dropped:
            gap = EvidenceFirstGap(
                gap_id=StableId(f"gap.{dropped_item.need_ids[0].root}.budget"[:128]),
                need_ids=dropped_item.need_ids,
                need_facet_ids=dropped_item.need_facet_ids,
                kind=EvidenceGapKind.BUDGET_EXCEEDED,
                reason=f"evidence dropped by {dropped_item.selection_reason or 'budget packing'}",
            )
            packed.append(
                dropped_item.model_copy(
                    update={
                        "evidence_ledger_ids": (),
                        "raw_preview": "",
                        "preview_truncated": False,
                        "semantic_answering_ledger_ids": (),
                        "semantic_partial_ledger_ids": (),
                        "semantic_related_ledger_ids": (),
                        "gap": gap,
                    }
                )
            )
        packed_items = tuple(
            sorted(
                packed,
                key=lambda item: (
                    item.mandatory is not True,
                    -self._need_priority(need_by_id, item),
                    item.need_ids[0].root,
                ),
            )
        )
        retained_spans = {
            self._span_key(entry.evidence_slices[0])
            for item in packed_items
            if item.gap is None
            for ledger_id in item.evidence_ledger_ids
            for entry in (self._entry_by_id(ledger_by_span, ledger_id),)
            if entry is not None
        }
        ledger_entries = tuple(
            ledger_by_span[span_key] for span_key in ledger_order if span_key in retained_spans
        )
        ledger = EvidenceLedgerV2(
            contract_version=self.ledger_contract_version,
            entries=ledger_entries,
            rendered_tokens=0,
        )
        ledger = ledger.model_copy(
            update={"rendered_tokens": self._evidence_tokens(ledger.entries)}
        )
        materialized_slice_ids = {
            slice_.slice_id for entry in ledger.entries for slice_ in entry.evidence_slices
        }
        semantic_receipts_raw = tuple(
            receipt for selection in selections for receipt in selection.semantic_receipts
        )
        if semantic_receipts_raw:
            expected_semantic_keys = {
                (selection.need.need_id, facet.need_facet_id)
                for selection in selections
                if selection.need.requirement is RequirementLevel.MANDATORY
                for facet in selection.need.need_facets
                if (
                    selection.need.completion_spec is None
                    or facet.need_facet_id in selection.need.completion_spec.required_need_facet_ids
                )
            }
            present_semantic_keys = {
                (receipt.need_id, receipt.need_facet_id) for receipt in semantic_receipts_raw
            }
            missing_semantic_keys = expected_semantic_keys - present_semantic_keys
            if missing_semantic_keys:
                diagnostics.append("SEMANTIC_RECEIPT_MISSING")
                for selection in selections:
                    for facet in selection.need.need_facets:
                        key = (selection.need.need_id, facet.need_facet_id)
                        if key not in missing_semantic_keys:
                            continue
                        semantic_receipts_raw += (
                            NeedFacetSemanticReceipt(
                                need_id=selection.need.need_id,
                                need_facet_id=facet.need_facet_id,
                                facet_kind=facet.facet_kind.value,
                                mandatory=True,
                                status=NeedEvidenceSemanticStatus.UNRESOLVED,
                                reason=(
                                    "semantic judge did not return a receipt for this "
                                    "mandatory facet"
                                ),
                                judge_version="",
                            ),
                        )
        semantic_receipts = tuple(
            receipt.model_copy(
                update={
                    "status": NeedEvidenceSemanticStatus.UNRESOLVED,
                    "reason": (
                        "semantic judgment referenced exact slices not materialized "
                        "in the retained Ledger"
                    ),
                }
            )
            if set(receipt.evaluated_slice_ids) - materialized_slice_ids
            else receipt
            for receipt in semantic_receipts_raw
        )
        if any(
            set(receipt.evaluated_slice_ids) - materialized_slice_ids
            for receipt in semantic_receipts_raw
        ):
            diagnostics.append("SEMANTIC_RECEIPT_NOT_MATERIALIZED")
        semantic_gap_kind = {
            NeedEvidenceSemanticStatus.PARTIAL: EvidenceGapKind.SEMANTIC_PARTIAL,
            NeedEvidenceSemanticStatus.UNSUPPORTED: EvidenceGapKind.SEMANTIC_UNSUPPORTED,
            NeedEvidenceSemanticStatus.UNRESOLVED: EvidenceGapKind.SEMANTIC_UNRESOLVED,
        }
        existing_gap_keys = {
            (item.need_ids[0], facet_id)
            for item in packed_items
            if item.gap is not None
            for facet_id in item.need_facet_ids
        }
        for receipt in semantic_receipts:
            key = (receipt.need_id, receipt.need_facet_id)
            if (
                not receipt.mandatory
                or receipt.status is NeedEvidenceSemanticStatus.SUPPORTED
                or key in existing_gap_keys
            ):
                continue
            semantic_need = need_by_id.get(receipt.need_id)
            if semantic_need is None:
                continue
            gap = EvidenceFirstGap(
                gap_id=StableId(f"gap.{receipt.need_id.root}.semantic.{receipt.facet_kind}"[:128]),
                need_ids=(receipt.need_id,),
                need_facet_ids=(receipt.need_facet_id,),
                kind=semantic_gap_kind[receipt.status],
                reason=receipt.reason or "semantic evidence judgment did not close this facet",
            )
            packed_items = (
                *packed_items,
                WriterContextEvidenceItem(
                    item_id=StableId(
                        f"context-item.{receipt.need_id.root}.semantic.{receipt.facet_kind}"[:128]
                    ),
                    section=(
                        semantic_need.expected_section
                        or WriterContextSection.CONTINUITY_CONSTRAINTS
                    ),
                    need_ids=(receipt.need_id,),
                    need_facet_ids=(receipt.need_facet_id,),
                    purpose=f"{self._purpose(semantic_need)} [{receipt.facet_kind}]",
                    mandatory=True,
                    semantic_status=receipt.status,
                    gap=gap,
                ),
            )
            existing_gap_keys.add(key)
            diagnostics.append(f"MANDATORY_FACET_GAP:{receipt.facet_kind}")
        packed_items = tuple(
            sorted(
                packed_items,
                key=lambda item: (
                    item.mandatory is not True,
                    -self._need_priority(need_by_id, item),
                    item.need_ids[0].root,
                ),
            )
        )
        gaps = tuple(item.gap for item in packed_items if item.gap is not None)
        rendered = self._render(packed_items)
        rendered_tokens = self._count(rendered)
        status = ContextAssemblyStatus.READY
        if rendered_tokens > writer_token_budget:
            status = ContextAssemblyStatus.CONTEXT_BUDGET_INSUFFICIENT
            diagnostics.append("EVIDENCE_ITEMS_EXCEED_WRITER_BUDGET")
        elif (
            ledger.rendered_tokens > evidence_ledger_token_budget
        ):  # pragma: no branch - pack keeps the ledger within budget
            status = ContextAssemblyStatus.EVIDENCE_INSUFFICIENT  # pragma: no cover
            diagnostics.append("EVIDENCE_LEDGER_BUDGET_EXCEEDED")  # pragma: no cover
        # Package status is the ADR-0008 mechanical delivery status: typed gaps
        # may coexist with READY.  Mandatory-facet closure is reported
        # separately and is the repair-campaign gate, never the mechanical one.
        mandatory_gap_items = tuple(
            item for item in packed_items if item.gap is not None and item.mandatory
        )
        mandatory_facet_closure: Literal["COMPLETE", "INCOMPLETE"] = (
            "INCOMPLETE" if mandatory_gap_items else "COMPLETE"
        )
        if mandatory_gap_items:
            diagnostics.append("MANDATORY_FACET_CLOSURE_INCOMPLETE")
        semantic_batch_receipts = tuple(
            dict(
                (
                    receipt.batch_id,
                    receipt,
                )
                for selection in selections
                for receipt in selection.semantic_batch_receipts
            ).values()
        )
        semantic_gap_kinds = {
            EvidenceGapKind.SEMANTIC_PARTIAL,
            EvidenceGapKind.SEMANTIC_UNSUPPORTED,
            EvidenceGapKind.SEMANTIC_UNRESOLVED,
        }
        structural_mandatory_gap_items = tuple(
            item
            for item in mandatory_gap_items
            if item.gap is not None and item.gap.kind not in semantic_gap_kinds
        )
        structural_mandatory_facet_closure: Literal["COMPLETE", "INCOMPLETE"] = (
            "COMPLETE" if not structural_mandatory_gap_items else "INCOMPLETE"
        )
        if semantic_receipts:
            semantic_status: Literal["COMPLETE", "INCOMPLETE", "UNASSESSED"] = (
                "COMPLETE"
                if all(
                    receipt.status is NeedEvidenceSemanticStatus.SUPPORTED
                    for receipt in semantic_receipts
                    if receipt.mandatory
                )
                else "INCOMPLETE"
            )
        else:
            semantic_status = "UNASSESSED"
        unclosed_mandatory_need_facets = tuple(
            dict.fromkeys(
                receipt.need_facet_id
                for receipt in semantic_receipts
                if receipt.mandatory and receipt.status is not NeedEvidenceSemanticStatus.SUPPORTED
            )
        )
        usable_with_gaps = (
            status is ContextAssemblyStatus.READY
            and not any(mechanical_failure_counts.values())
            and bool(ledger.entries)
            and semantic_status == "INCOMPLETE"
        )
        budget_report = WriterContextBudgetReportV2(
            tokenizer=self._tokenizer_name,
            tokenizer_version=self._tokenizer_version,
            configured_writer_token_budget=writer_token_budget,
            actual_rendered_writer_tokens=rendered_tokens,
            configured_ledger_token_budget=evidence_ledger_token_budget,
            actual_rendered_ledger_tokens=ledger.rendered_tokens,
            item_count=len(packed_items),
            evidence_item_count=sum(item.gap is None for item in packed_items),
            gap_item_count=sum(item.gap is not None for item in packed_items),
            ledger_entry_count=len(ledger.entries),
            dropped_slice_reasons=dropped_slice_reasons,
            final_status=status,
        )
        ledger_ref = self._artifact_ref(
            ledger, "application/vnd.novel-agent.evidence-ledger-v2+json"
        )
        lineage = EvidenceFirstLineage(
            need_ids=need_ids,
            assembler_version=self.version,
            grounder_version=grounder_version,
            validator_version=validator_version,
            generator_version=generator_version,
            query_compiler_version=query_compiler_version,
            route_plan_version=route_plan_version,
            resolver_version=self._resolver.version,
            semantic_judge_version=next(
                (receipt.judge_version for receipt in semantic_receipts if receipt.judge_version),
                "",
            ),
            planner_artifact_ref=planner_artifact_ref,
            planner_artifact_hash=planner_artifact_hash,
            planner_fallback_used=planner_fallback_used,
            unresolved_lexical_anchors=unresolved_lexical_anchors,
        )
        package = WriterContextPackageV2(
            contract_version=cast(Literal["writer_context.v2"], self.contract_version),
            task_contract=task,
            basis_commit_id=basis_commit_id,
            basis_snapshot_id=basis_snapshot_id,
            arm=cast(Literal["A", "B", "C"], arm),
            items=packed_items,
            gaps=gaps,
            budget_report=budget_report,
            evidence_ledger_ref=ledger_ref,
            lineage=lineage,
            rendered_context=rendered,
            assembly_status=status.value,
            semantic_status=semantic_status,
            usable_with_gaps=usable_with_gaps,
            structural_mandatory_facet_closure=structural_mandatory_facet_closure,
            unclosed_mandatory_need_facets=unclosed_mandatory_need_facets,
            semantic_receipts=semantic_receipts,
            semantic_batch_receipts=semantic_batch_receipts,
        )
        assert_safe_public_payload(package.model_dump(mode="json"))
        return EvidenceFirstAssemblyResult(
            status=status,
            package=package,
            evidence_ledger=ledger,
            diagnostic_codes=tuple(dict.fromkeys(diagnostics)),
            mechanical_failure_counts=mechanical_failure_counts,
            mandatory_facet_closure=mandatory_facet_closure,
            structural_mandatory_facet_closure=structural_mandatory_facet_closure,
            semantic_status=semantic_status,
            usable_with_gaps=usable_with_gaps,
            unclosed_mandatory_need_facets=unclosed_mandatory_need_facets,
            semantic_receipts=semantic_receipts,
            semantic_batch_receipts=semantic_batch_receipts,
            assembler_version=self.version,
        )

    def _pack_items(
        self,
        items: tuple[WriterContextEvidenceItem, ...],
        ledger_by_span: Mapping[tuple[str, int, int], EvidenceLedgerEntryV2],
        *,
        writer_token_budget: int,
        ledger_budget: int,
    ) -> tuple[list[WriterContextEvidenceItem], list[WriterContextEvidenceItem]]:
        packed: list[WriterContextEvidenceItem] = []
        dropped: list[WriterContextEvidenceItem] = []
        used_writer_tokens = 0
        packed_span_keys: set[tuple[str, int, int]] = set()
        used_ledger_tokens = 0

        def item_spans(item: WriterContextEvidenceItem) -> set[tuple[str, int, int]]:
            return {
                span_key
                for ledger_id in item.evidence_ledger_ids
                for span_key in self._spans_by_ledger_id(ledger_by_span, ledger_id)
            }

        def new_ledger_tokens(item: WriterContextEvidenceItem) -> int:
            # The ledger renders each exact span once even when several Needs
            # share it; only spans not yet packed cost budget here.
            return sum(
                self._count(ledger_by_span[span_key].evidence_text)
                for span_key in item_spans(item) - packed_span_keys
            )

        def writer_tokens(item: WriterContextEvidenceItem) -> int:
            return self._count(self._render((item,)))

        for item in items:
            cost_writer = writer_tokens(item)
            cost_ledger = new_ledger_tokens(item)
            if (
                used_writer_tokens + cost_writer <= writer_token_budget
                and used_ledger_tokens + cost_ledger <= ledger_budget
            ):
                packed.append(item)
                used_writer_tokens += cost_writer
                packed_span_keys.update(item_spans(item))
                used_ledger_tokens += cost_ledger
            else:
                dropped.append(item)
        return packed, dropped

    @staticmethod
    def _entry_by_id(
        ledger_by_span: Mapping[tuple[str, int, int], EvidenceLedgerEntryV2],
        ledger_id: StableId,
    ) -> EvidenceLedgerEntryV2 | None:
        return next(
            (entry for entry in ledger_by_span.values() if entry.ledger_id == ledger_id),
            None,
        )

    @classmethod
    def _spans_by_ledger_id(
        cls,
        ledger_by_span: Mapping[tuple[str, int, int], EvidenceLedgerEntryV2],
        ledger_id: StableId,
    ) -> tuple[tuple[str, int, int], ...]:
        return tuple(
            span_key for span_key, entry in ledger_by_span.items() if entry.ledger_id == ledger_id
        )

    @classmethod
    def _span_key(cls, slice_: EvidenceSlice) -> tuple[str, int, int]:
        return (slice_.object_hash.root, slice_.start, slice_.end)

    @classmethod
    def _span_hash(cls, slice_: EvidenceSlice) -> ArtifactId:
        return sha256_id(f"{slice_.object_hash.root}:{slice_.start}:{slice_.end}".encode())

    @classmethod
    def _reverify_slice(
        cls,
        slice_: EvidenceSlice,
        blocks: dict[StableId, TextBlock],
        task: BenchmarkTaskContract,
        need: Stage1MemoryNeed,
    ) -> tuple[EvidenceSlice | None, str | None]:
        """Re-verify one slice against the immutable text root.

        Returns ``(slice, None)`` on success and ``(None, typed_reason)`` on
        rejection so mechanical failure counts stay typed:
        ``scope_failed`` / ``cutoff_failed`` / ``dereference_failed``.
        """
        if slice_.source_commit != need.base_commit:
            return None, "dereference_failed"
        if slice_.access_scope != need.access_scope:
            return None, "scope_failed"
        block = blocks.get(slice_.parent_block_id)
        if block is None:
            return None, "dereference_failed"
        if (
            slice_.end > len(block.text)
            or slice_.chapter_id != block.chapter_id
            or slice_.object_hash != sha256_id(block.text.encode("utf-8"))
            or block.text[slice_.start : slice_.end] != slice_.text
            or quote_hash(slice_.text) != slice_.quote_hash
        ):
            return None, "dereference_failed"
        chapter = cls._chapter_number(slice_.chapter_id)
        if chapter is not None and chapter > task.checkpoint_chapter:
            return None, "cutoff_failed"
        return slice_, None

    @staticmethod
    def _chapter_number(chapter_id: StableId) -> int | None:
        if chapter_id.root.endswith(".prelude") or chapter_id.root.startswith("prelude."):
            return 0
        match = re.search(r"(?:^|[._:-])(\d+)$", chapter_id.root)
        return int(match.group(1)) if match is not None else None

    @classmethod
    def _blocks(cls, text_root: TextRootDocument) -> dict[StableId, TextBlock]:
        return {
            block.block_id: block
            for scene in (
                *(text_root.prelude.scenes if text_root.prelude is not None else ()),
                *(scene for chapter in text_root.chapters for scene in chapter.scenes),
            )
            for block in scene.blocks
        }

    @classmethod
    def _purpose(cls, need: Stage1MemoryNeed) -> str:
        question = need.semantic_question or need.query_text
        if need.why_needed:
            return f"{question}({need.why_needed})"
        return question

    @classmethod
    def _preview(cls, texts: tuple[str, ...]) -> tuple[str, bool]:
        if not texts:  # pragma: no cover - preview is built from non-empty ledger refs
            return "", False  # pragma: no cover
        combined = "\n".join(texts)
        if len(combined) <= PREVIEW_CHAR_LIMIT:
            return combined, False
        return combined[: PREVIEW_CHAR_LIMIT - 1] + "…", True

    @staticmethod
    def _item_semantic_status(
        need: Stage1MemoryNeed,
        receipts: dict[StableId, NeedFacetSemanticReceipt],
    ) -> NeedEvidenceSemanticStatus | None:
        if not receipts:
            return None
        required = (
            need.completion_spec.required_need_facet_ids
            if need.completion_spec is not None
            else tuple(facet.need_facet_id for facet in need.need_facets)
        )
        statuses = tuple(receipts[facet_id].status for facet_id in required if facet_id in receipts)
        if not statuses or any(
            status is NeedEvidenceSemanticStatus.UNRESOLVED for status in statuses
        ):
            return NeedEvidenceSemanticStatus.UNRESOLVED
        if all(status is NeedEvidenceSemanticStatus.SUPPORTED for status in statuses):
            return NeedEvidenceSemanticStatus.SUPPORTED
        if any(status is NeedEvidenceSemanticStatus.PARTIAL for status in statuses):
            return NeedEvidenceSemanticStatus.PARTIAL
        return NeedEvidenceSemanticStatus.UNSUPPORTED

    @staticmethod
    def _validity_from_facets(facets: tuple[NeedFacet, ...]) -> WriterContextValidity:
        scopes = {facet.expected_claim_scope for facet in facets}
        if ExpectedClaimScope.PLANNED in scopes:
            return WriterContextValidity.PLANNED
        if ExpectedClaimScope.HISTORICAL in scopes:
            return WriterContextValidity.HISTORICAL
        if scopes.intersection({ExpectedClaimScope.CURRENT, ExpectedClaimScope.KNOWLEDGE}):
            return WriterContextValidity.CURRENT
        return WriterContextValidity.UNCERTAIN

    @staticmethod
    def _need_priority(
        need_by_id: Mapping[StableId, Stage1MemoryNeed], item: WriterContextEvidenceItem
    ) -> int:
        return next(
            (
                need.priority
                for need_id in item.need_ids
                if (need := need_by_id.get(need_id)) is not None
            ),
            0,
        )

    @classmethod
    def _render(cls, items: tuple[WriterContextEvidenceItem, ...]) -> str:
        rendered: list[str] = []
        for item in items:
            if item.gap is not None:
                rendered.append(f"[GAP {item.gap.kind.value}] {item.purpose}")
                continue
            rendered.append(
                f"[{item.section.value}] {item.purpose}\n"
                f"  {item.raw_preview}{' [truncated]' if item.preview_truncated else ''}"
            )
        return "\n".join(rendered)

    def _evidence_tokens(self, entries: tuple[EvidenceLedgerEntryV2, ...]) -> int:
        return sum(max(1, self._count(entry.evidence_text)) for entry in entries)

    @staticmethod
    def _artifact_ref(model: DomainModel, media_type: str) -> ArtifactRef:
        payload = canonical_json_bytes(model.model_dump(mode="json"))
        return ArtifactRef(
            artifact_id=sha256_id(payload),
            media_type=media_type,
            byte_length=len(payload),
            schema_version=SchemaVersion("1.0.0"),
        )

    @staticmethod
    def _default_token_count(text: str) -> int:
        return max(1, (len(text) + 3) // 4)


__all__ = [
    "PREVIEW_CHAR_LIMIT",
    "EvidenceFirstAssemblyResult",
    "EvidenceFirstWriterContextAssembler",
    "NeedEvidenceSelection",
    "SliceSelectionTrace",
]
