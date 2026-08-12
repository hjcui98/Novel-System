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


class NeedEvidenceSelection(DomainModel):
    """Selected exact slices and traces for one public Need."""

    need: Stage1MemoryNeed
    selections: tuple[SliceSelectionTrace, ...] = ()
    slices: tuple[EvidenceSlice, ...] = ()


class EvidenceFirstAssemblyResult(DomainModel):
    status: ContextAssemblyStatus
    package: WriterContextPackageV2
    evidence_ledger: EvidenceLedgerV2
    diagnostic_codes: tuple[str, ...] = ()
    mechanical_failure_counts: dict[str, int] = Field(default_factory=dict)
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
        facet_by_need: dict[StableId, tuple[NeedFacet, ...]] = {
            selection.need.need_id: selection.need.need_facets for selection in selections
        }
        need_by_id = {selection.need.need_id: selection.need for selection in selections}
        ledger_by_span: dict[tuple[str, int, int], EvidenceLedgerEntryV2] = {}
        ledger_order: list[tuple[str, int, int]] = []
        dropped_slice_reasons: dict[str, str] = {}
        items: list[WriterContextEvidenceItem] = []

        ledger_tokens = 0

        def add_ledger_entry(
            span_key: tuple[str, int, int],
            slice_: EvidenceSlice,
            evidence_refs: tuple[EvidenceRef, ...],
            unit_ids: tuple[StableId, ...],
            need_id: StableId,
        ) -> StableId:
            nonlocal ledger_tokens
            existing = ledger_by_span.get(span_key)
            if existing is not None:
                merged = existing.model_copy(
                    update={
                        "need_ids": tuple(dict.fromkeys((*existing.need_ids, need_id))),
                        "need_facet_ids": tuple(
                            dict.fromkeys(
                                (
                                    *existing.need_facet_ids,
                                    *(
                                        facet.need_facet_id
                                        for facet in facet_by_need.get(need_id, ())
                                    ),
                                )
                            )
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
                need_facet_ids=tuple(
                    facet.need_facet_id for facet in facet_by_need.get(need_id, ())
                ),
            )
            ledger_by_span[span_key] = entry
            ledger_order.append(span_key)
            ledger_tokens += self._count(entry.evidence_text)
            return entry_id

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
            ledger_ids: list[StableId] = []
            for trace in ordered:
                slice_ = slice_by_id[trace.slice_id]
                span_key = self._span_key(slice_)
                evidence_refs = (trace.evidence_ref,) if trace.evidence_ref is not None else ()
                unit_ids = (trace.unit_id,)
                ledger_ids.append(
                    add_ledger_entry(
                        span_key,
                        slice_,
                        evidence_refs,
                        unit_ids,
                        need.need_id,
                    )
                )
            facet_ids = tuple(facet.need_facet_id for facet in need.need_facets)
            if not ledger_ids:
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
                        gap=gap,
                    )
                )
                continue
            preview, truncated = self._preview(
                tuple(
                    ledger_by_span[self._span_key(slice_by_id[trace.slice_id])].evidence_text
                    for trace in ordered
                )
            )
            top = ordered[0]
            validity = self._validity_from_facets(need.need_facets)
            items.append(
                WriterContextEvidenceItem(
                    item_id=StableId(f"context-item.{need.need_id.root}"[:128]),
                    section=need.expected_section or WriterContextSection.CONTINUITY_CONSTRAINTS,
                    need_ids=(need.need_id,),
                    need_facet_ids=facet_ids,
                    purpose=self._purpose(need),
                    evidence_ledger_ids=tuple(dict.fromkeys(ledger_ids)),
                    raw_preview=preview,
                    preview_truncated=truncated,
                    source_scope=need.access_scope,
                    source_kind=need.need_type,
                    validity=validity,
                    mandatory=need.requirement is RequirementLevel.MANDATORY,
                    selection_reason=(
                        f"route={top.route_channel};fused_rank={top.fused_rank};"
                        f"{top.selection_reason}"
                    ),
                )
            )

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
        packed, dropped = self._pack(
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
        elif any(item.gap is not None and item.mandatory for item in packed_items):
            status = ContextAssemblyStatus.EVIDENCE_INSUFFICIENT
            diagnostics.append("MANDATORY_NEED_EVIDENCE_GAP")
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
        )
        assert_safe_public_payload(package.model_dump(mode="json"))
        return EvidenceFirstAssemblyResult(
            status=status,
            package=package,
            evidence_ledger=ledger,
            diagnostic_codes=tuple(dict.fromkeys(diagnostics)),
            mechanical_failure_counts=mechanical_failure_counts,
            assembler_version=self.version,
        )

    def _pack(
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
