"""Assemble bounded writer-facing claims and a separate evidence ledger."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Literal

from pydantic import Field

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import CommitId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    NeedFacet,
    RequirementLevel,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1MemoryNeed,
)
from novel_agent.domain.stage2 import ContextAssemblySpec
from novel_agent.domain.text import EvidenceRef
from novel_agent.domain.writer_context import (
    BenchmarkTaskContract,
    ClaimSupportGroup,
    ClaimSupportReceipt,
    ClaimVariant,
    ContextAssemblyStatus,
    ContextGap,
    ContextLineage,
    CutoffAttestation,
    EvidenceLedger,
    EvidenceLedgerEntry,
    EvidenceResolutionStatus,
    SemanticSupportStatus,
    WriterContextBudgetReport,
    WriterContextItem,
    WriterContextPackage,
    WriterContextSection,
    WriterContextValidity,
)
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.canonical_alias_registry import canonical_alias_receipt_ref
from novel_agent.services.content_addressing import canonical_json_bytes, quote_hash
from novel_agent.services.retrieval_unit_normalizer import (
    NormalizedRetrievalSet,
    RetrievalUnitNormalizer,
)

TokenCounter = Callable[[str], int]


class WriterContextAssemblyResult(DomainModel):
    status: ContextAssemblyStatus
    package: WriterContextPackage
    evidence_ledger: EvidenceLedger
    diagnostic_codes: tuple[str, ...] = ()
    assembler_version: str = Field(min_length=1)


class WriterContextAssembler:
    version = "writer_context_assembler.v18"
    contract_version = "writer_context.v1"

    def __init__(
        self,
        *,
        token_counter: TokenCounter | None = None,
        tokenizer_name: str = "deterministic_unicode",
        tokenizer_version: str = "v1",
        max_reduction_rounds: int = 2,
        normalizer: RetrievalUnitNormalizer | None = None,
    ) -> None:
        if max_reduction_rounds < 0:
            raise ValueError("max reduction rounds cannot be negative")
        self._count = token_counter or self._default_token_count
        self._tokenizer_name = tokenizer_name
        self._tokenizer_version = tokenizer_version
        self._max_reduction_rounds = max_reduction_rounds
        self._normalizer = normalizer or RetrievalUnitNormalizer()

    def count_tokens(self, text: str) -> int:
        return self._count(text)

    def assemble_from_spec(
        self,
        *,
        task: BenchmarkTaskContract,
        assembly_spec: ContextAssemblySpec,
        support_groups: tuple[ClaimSupportGroup, ...],
        claim_variants: tuple[ClaimVariant, ...],
        support_receipts: tuple[ClaimSupportReceipt, ...],
        cutoff_attestations: tuple[CutoffAttestation, ...],
        needs: tuple[Stage1MemoryNeed, ...],
        basis_commit_id: CommitId,
        basis_snapshot_id: StableId,
        arm: Literal["A", "B", "C"],
    ) -> WriterContextAssemblyResult:
        """Validate and pack only Controller-selected receipt-bound variants."""

        group_by_id = {item.support_group_id: item for item in support_groups}
        variant_by_id = {item.claim_variant_id: item for item in claim_variants}
        receipt_by_ref = {
            self._artifact_ref(
                item,
                "application/vnd.novel-agent.claim-support-receipt+json",
            ).artifact_id: item
            for item in support_receipts
        }
        attestation_by_ref = {
            self._artifact_ref(
                item,
                "application/vnd.novel-agent.cutoff-attestation+json",
            ).artifact_id: item
            for item in cutoff_attestations
        }
        diagnostics: list[str] = []
        if tuple(group_by_id) != assembly_spec.selected_support_group_ids:
            diagnostics.append("ASSEMBLY_SPEC_SUPPORT_GROUP_SET_MISMATCH")
        selected_items: list[WriterContextItem] = []
        ledger_entries: list[EvidenceLedgerEntry] = []
        mandatory_ids = set(assembly_spec.mandatory_support_group_ids)
        need_by_id = {need.need_id: need for need in needs}
        facet_by_id = {facet.need_facet_id: facet for need in needs for facet in need.need_facets}
        legal_variant_by_group: dict[StableId, ClaimVariant] = {}

        for group_id in assembly_spec.selected_support_group_ids:
            group = group_by_id.get(group_id)
            if group is None:
                diagnostics.append(f"SUPPORT_GROUP_MISSING:{group_id.root}")
                continue
            receipt = receipt_by_ref.get(group.support_receipt_ref.artifact_id)
            if receipt is None or not self._receipt_matches_group(
                receipt,
                group,
                basis_commit_id=basis_commit_id,
                basis_snapshot_id=basis_snapshot_id,
                attestation=attestation_by_ref.get(group.cutoff_attestation_ref.artifact_id),
                checkpoint_chapter=task.checkpoint_chapter,
            ):
                diagnostics.append(f"SUPPORT_RECEIPT_INVALID:{group_id.root}")
                continue
            allowed_ids = assembly_spec.allowed_claim_variant_ids_by_support_group.get(
                group_id.root,
                (),
            )
            for variant_id in allowed_ids:
                variant = variant_by_id.get(variant_id)
                if variant is not None and self._variant_matches_receipt(variant, receipt):
                    legal_variant_by_group[group_id] = variant
                    break
            if group_id not in legal_variant_by_group:
                diagnostics.append(f"CLAIM_VARIANT_INVALID:{group_id.root}")

        missing_mandatory = mandatory_ids - set(legal_variant_by_group)
        diagnostics.extend(
            f"MANDATORY_SUPPORT_GROUP_UNAVAILABLE:{item.root}"
            for item in sorted(missing_mandatory, key=lambda value: value.root)
        )
        ordered_groups = (
            *assembly_spec.mandatory_support_group_ids,
            *assembly_spec.ordered_optional_support_group_ids,
        )
        for group_id in ordered_groups:
            group = group_by_id.get(group_id)
            variant = legal_variant_by_group.get(group_id)
            if group is None or variant is None:
                continue
            matching_needs = tuple(
                need_by_id[need_id] for need_id in group.need_ids if need_id in need_by_id
            )
            section = next(
                (
                    need.expected_section
                    for need in matching_needs
                    if need.expected_section is not None
                ),
                WriterContextSection.CONTINUITY_CONSTRAINTS,
            )
            validity = self._validity_from_facets(
                group.need_facet_ids,
                facet_by_id,
            )
            ledger_id = StableId(f"ledger.{group_id.root}"[:128])
            ledger_entries.append(
                EvidenceLedgerEntry(
                    ledger_id=ledger_id,
                    evidence_refs=group.evidence_refs,
                    plan_node_ids=group.plan_node_ids,
                    claim_excerpt=variant.claim_text[:240],
                    source_commit=basis_commit_id,
                    information_scope=(
                        receipt_by_ref[group.support_receipt_ref.artifact_id].information_scope
                    ),
                    need_ids=group.need_ids,
                    retrieval_unit_ids=group.retrieval_unit_ids,
                    support_group_id=group.support_group_id,
                    need_facet_ids=group.need_facet_ids,
                    support_receipt_ref=group.support_receipt_ref,
                )
            )
            selected_items.append(
                WriterContextItem(
                    context_item_id=StableId(f"context-item.{variant.claim_variant_id.root}"[:128]),
                    section=section,
                    claim=variant.claim_text,
                    validity=validity,
                    mandatory=group_id in mandatory_ids,
                    confidence=1.0,
                    need_ids=group.need_ids,
                    retrieval_unit_ids=group.retrieval_unit_ids,
                    evidence_ledger_ids=(ledger_id,),
                    claim_variant_id=variant.claim_variant_id,
                    support_group_id=group.support_group_id,
                    need_facet_ids=variant.covered_need_facet_ids,
                    support_receipt_ref=variant.support_receipt_ref,
                )
            )

        mandatory = [item for item in selected_items if item.mandatory]
        optional_by_group = {
            item.support_group_id: item
            for item in selected_items
            if not item.mandatory and item.support_group_id is not None
        }
        gaps = tuple(
            ContextGap(
                gap_id=StableId(f"gap.unclosed.{facet_id.root}"[:128]),
                description=f"public Need facet remains unresolved: {facet_id.root}",
                need_ids=tuple(
                    need.need_id
                    for need in needs
                    if need.completion_spec is not None
                    and facet_id in need.completion_spec.required_need_facet_ids
                ),
            )
            for facet_id in assembly_spec.unresolved_need_facet_ids
        )
        selected = list(mandatory)
        mandatory_rendered = self._render(mandatory, gaps)
        mandatory_ledger_ids = {
            ledger_id for item in mandatory for ledger_id in item.evidence_ledger_ids
        }
        ledger_by_id = {item.ledger_id: item for item in ledger_entries}
        mandatory_ledger_tokens = self._evidence_tokens(
            tuple(ledger_by_id[item] for item in mandatory_ledger_ids)
        )
        writer_budget = assembly_spec.writer_token_budget or assembly_spec.token_budget
        ledger_budget = assembly_spec.evidence_ledger_token_budget
        if self._count(mandatory_rendered) <= writer_budget and (
            mandatory_ledger_tokens <= ledger_budget
        ):
            for group_id in assembly_spec.ordered_optional_support_group_ids:
                item = optional_by_group.get(group_id)
                if item is None:
                    continue
                proposed = [*selected, item]
                proposed_ledger_ids = {
                    ledger_id
                    for proposed_item in proposed
                    for ledger_id in proposed_item.evidence_ledger_ids
                }
                if (
                    self._count(self._render(proposed, gaps)) <= writer_budget
                    and self._evidence_tokens(
                        tuple(ledger_by_id[value] for value in proposed_ledger_ids)
                    )
                    <= ledger_budget
                ):
                    selected.append(item)

        selected_ledger_ids = {
            ledger_id for item in selected for ledger_id in item.evidence_ledger_ids
        }
        ledger = EvidenceLedger(
            contract_version="evidence_ledger.v2",
            entries=tuple(
                entry for entry in ledger_entries if entry.ledger_id in selected_ledger_ids
            ),
            rendered_tokens=0,
        )
        ledger = ledger.model_copy(
            update={"rendered_tokens": self._evidence_tokens(ledger.entries)}
        )
        rendered = self._render(selected, gaps)
        rendered_tokens = self._count(rendered)
        status = ContextAssemblyStatus.READY
        mandatory_facet_ids = {
            facet_id
            for need in needs
            if need.requirement is RequirementLevel.MANDATORY and need.completion_spec is not None
            for facet_id in need.completion_spec.required_need_facet_ids
        }
        unresolved_mandatory = mandatory_facet_ids.intersection(
            assembly_spec.unresolved_need_facet_ids
        )
        if any(
            code.startswith(("ASSEMBLY_SPEC_", "SUPPORT_GROUP_MISSING")) for code in diagnostics
        ):
            status = ContextAssemblyStatus.POLICY_BLOCKED
        elif missing_mandatory or any(
            code.startswith(("SUPPORT_RECEIPT_INVALID", "CLAIM_VARIANT_INVALID"))
            for code in diagnostics
        ):
            status = ContextAssemblyStatus.EVIDENCE_INSUFFICIENT
        elif unresolved_mandatory:
            status = ContextAssemblyStatus.EVIDENCE_INSUFFICIENT
            diagnostics.append("MANDATORY_NEED_FACETS_UNCLOSED")
        elif rendered_tokens > writer_budget:
            status = ContextAssemblyStatus.CONTEXT_BUDGET_INSUFFICIENT
            diagnostics.append("MANDATORY_SUPPORT_GROUPS_EXCEED_WRITER_BUDGET")
        elif ledger.rendered_tokens > ledger_budget:
            status = ContextAssemblyStatus.EVIDENCE_INSUFFICIENT
            diagnostics.append("MANDATORY_SUPPORT_GROUPS_EXCEED_LEDGER_BUDGET")

        dropped = tuple(item.context_item_id for item in selected_items if item not in selected)
        budget_report = WriterContextBudgetReport(
            tokenizer=self._tokenizer_name,
            tokenizer_version=self._tokenizer_version,
            configured_writer_token_budget=writer_budget,
            actual_rendered_writer_tokens=rendered_tokens,
            evidence_ledger_tokens=ledger.rendered_tokens,
            mandatory_conclusion_tokens=self._count(self._render(mandatory, ())),
            optional_conclusion_tokens=self._count(
                self._render([item for item in selected if not item.mandatory], ())
            ),
            header_citation_gap_tokens=0,
            deduplicated_item_count=0,
            superseded_item_count=0,
            dropped_optional_ids=dropped,
            dropped_optional_reasons={item.root: "deterministic_budget_pack" for item in dropped},
            reduction_rounds=0,
            final_status=status,
        )
        ledger_ref = self._artifact_ref(
            ledger,
            "application/vnd.novel-agent.evidence-ledger+json",
        )
        spec_ref = self._artifact_ref(
            assembly_spec,
            "application/vnd.novel-agent.context-assembly-spec+json",
        )
        package = WriterContextPackage(
            contract_version="writer_context.v2",
            task_contract=task,
            basis_commit_id=basis_commit_id,
            basis_snapshot_id=basis_snapshot_id,
            arm=arm,
            continuity_constraints=self._section(
                selected, WriterContextSection.CONTINUITY_CONSTRAINTS
            ),
            current_world_state=self._section(selected, WriterContextSection.CURRENT_WORLD_STATE),
            relationship_and_emotion=self._section(
                selected, WriterContextSection.RELATIONSHIP_AND_EMOTION
            ),
            causal_history=self._section(selected, WriterContextSection.CAUSAL_HISTORY),
            knowledge_and_disclosure=self._section(
                selected, WriterContextSection.KNOWLEDGE_AND_DISCLOSURE
            ),
            plan_and_obligations=self._section(selected, WriterContextSection.PLAN_AND_OBLIGATIONS),
            long_range_callbacks=self._section(selected, WriterContextSection.LONG_RANGE_CALLBACKS),
            gaps=gaps,
            budget_report=budget_report,
            evidence_ledger_ref=ledger_ref,
            lineage=ContextLineage(
                need_ids=tuple(dict.fromkeys(need.need_id for need in needs)),
                retrieval_unit_ids=tuple(
                    dict.fromkeys(
                        unit_id for group in support_groups for unit_id in group.retrieval_unit_ids
                    )
                ),
                assembler_version=self.version,
                normalized_unit_count=len(
                    {unit_id for group in support_groups for unit_id in group.retrieval_unit_ids}
                ),
                selected_claim_variant_ids=tuple(
                    item.claim_variant_id for item in selected if item.claim_variant_id is not None
                ),
                context_assembly_spec_ref=spec_ref,
            ),
            rendered_context=rendered,
        )
        return WriterContextAssemblyResult(
            status=status,
            package=package,
            evidence_ledger=ledger,
            diagnostic_codes=tuple(dict.fromkeys(diagnostics)),
            assembler_version=self.version,
        )

    @classmethod
    def _receipt_matches_group(
        cls,
        receipt: ClaimSupportReceipt,
        group: ClaimSupportGroup,
        *,
        basis_commit_id: CommitId,
        basis_snapshot_id: StableId,
        attestation: CutoffAttestation | None,
        checkpoint_chapter: int,
    ) -> bool:
        return (
            receipt.support_group_id == group.support_group_id
            and receipt.claim_id == group.claim_id
            and receipt.need_ids == group.need_ids
            and receipt.need_facet_ids == group.need_facet_ids
            and receipt.retrieval_unit_ids == group.retrieval_unit_ids
            and receipt.evidence_refs == group.evidence_refs
            and receipt.plan_node_ids == group.plan_node_ids
            and group.evidence_resolution_status is receipt.evidence_resolution_status
            and group.semantic_support_status is receipt.semantic_support_status
            and group.counter_evidence_refs == receipt.counter_evidence_refs
            and receipt.evidence_resolution_status is EvidenceResolutionStatus.RESOLVED
            and receipt.semantic_support_status is SemanticSupportStatus.VERIFIED
            and not receipt.counter_evidence_refs
            and receipt.basis_commit_id == basis_commit_id
            and receipt.basis_snapshot_id == basis_snapshot_id
            and receipt.cutoff_attestation_ref == group.cutoff_attestation_ref
            and cls._artifact_ref(
                receipt,
                "application/vnd.novel-agent.claim-support-receipt+json",
            )
            == group.support_receipt_ref
            and attestation is not None
            and attestation.basis_commit_id == basis_commit_id
            and attestation.basis_snapshot_id == basis_snapshot_id
            and attestation.checkpoint_chapter == checkpoint_chapter
            and attestation.retrieval_unit_ids == group.retrieval_unit_ids
            and attestation.information_scope == receipt.information_scope
        )

    def _variant_matches_receipt(
        self,
        variant: ClaimVariant,
        receipt: ClaimSupportReceipt,
    ) -> bool:
        return (
            variant.claim_id == receipt.claim_id
            and variant.support_group_id == receipt.support_group_id
            and variant.claim_text_hash == sha256_id(variant.claim_text.encode("utf-8"))
            and variant.claim_text_hash == receipt.claim_text_hash
            and variant.covered_need_facet_ids == receipt.need_facet_ids
            and variant.support_receipt_ref
            == self._artifact_ref(
                receipt,
                "application/vnd.novel-agent.claim-support-receipt+json",
            )
            and variant.token_cost == max(1, self._count(variant.claim_text))
        )

    @staticmethod
    def _validity_from_facets(
        facet_ids: tuple[StableId, ...],
        facet_by_id: dict[StableId, NeedFacet],
    ) -> WriterContextValidity:
        scopes = {
            getattr(facet_by_id.get(facet_id), "expected_claim_scope", None)
            for facet_id in facet_ids
        }
        scope_values = {getattr(scope, "value", None) for scope in scopes if scope is not None}
        if "planned" in scope_values:
            return WriterContextValidity.PLANNED
        if "historical" in scope_values:
            return WriterContextValidity.HISTORICAL
        if scope_values.intersection({"current", "knowledge"}):
            return WriterContextValidity.CURRENT
        return WriterContextValidity.UNCERTAIN

    @staticmethod
    def _artifact_ref(model: DomainModel, media_type: str) -> ArtifactRef:
        payload = canonical_json_bytes(model.model_dump(mode="json"))
        return ArtifactRef(
            artifact_id=sha256_id(payload),
            media_type=media_type,
            byte_length=len(payload),
            schema_version=SchemaVersion("1.0.0"),
        )

    def assemble(
        self,
        *,
        task: BenchmarkTaskContract,
        units: tuple[RetrievalUnit, ...],
        needs: tuple[Stage1MemoryNeed, ...],
        basis_commit_id: CommitId,
        basis_snapshot_id: StableId,
        arm: Literal["A", "B", "C"],
        writer_token_budget: int,
        evidence_ledger_token_budget: int = 12_000,
        unit_need_ids: Mapping[StableId, tuple[StableId, ...]] | None = None,
    ) -> WriterContextAssemblyResult:
        if arm not in {"A", "B", "C"}:
            raise ValueError("writer context arm must be A, B, or C")
        if writer_token_budget < 1 or evidence_ledger_token_budget < 1:
            raise ValueError("writer and evidence budgets must be positive")
        normalized = self._normalizer.normalize(units)
        normalized_unit_need_ids: dict[StableId, tuple[StableId, ...]] | None = None
        if unit_need_ids is not None:
            normalized_unit_need_ids = {}
            original_by_identity: dict[tuple[object, ...], list[RetrievalUnit]] = {}
            for unit in units:
                original_by_identity.setdefault(
                    self._normalizer._canonical_identity(unit), []
                ).append(unit)
            for unit in normalized.units:
                identities = tuple(
                    dict.fromkeys(
                        need_id
                        for original in original_by_identity.get(
                            self._normalizer._canonical_identity(unit), []
                        )
                        for need_id in unit_need_ids.get(original.unit_id, ())
                    )
                )
                if identities:
                    normalized_unit_need_ids[unit.unit_id] = identities
        need_by_id = {need.need_id: need for need in needs}
        claims, ledger_entries, gaps = self._claims(
            normalized,
            needs,
            basis_commit_id,
            unit_need_ids=normalized_unit_need_ids,
        )
        conflicts = tuple(
            ContextGap(
                gap_id=StableId(f"gap.conflict.{index}"),
                description=description,
                conflict=True,
            )
            for index, description in enumerate(normalized.conflicts, start=1)
        )
        gaps = (*gaps, *conflicts)

        mandatory = [item for item in claims if item.mandatory]
        optional = [item for item in claims if not item.mandatory]
        ledger_by_id = {entry.ledger_id: entry for entry in ledger_entries}

        def evidence_tokens(items: list[WriterContextItem]) -> int:
            ledger_ids = tuple(
                dict.fromkeys(ledger_id for item in items for ledger_id in item.evidence_ledger_ids)
            )
            return self._evidence_tokens(
                tuple(
                    ledger_by_id[ledger_id] for ledger_id in ledger_ids if ledger_id in ledger_by_id
                )
            )

        reduction_rounds = 0
        mandatory_rendered = self._render(mandatory, gaps)
        while (
            self._count(mandatory_rendered) > writer_token_budget
            and reduction_rounds < self._max_reduction_rounds
        ):
            reduction_rounds += 1
            mandatory = [
                item.model_copy(
                    update={
                        "claim": self._reduce_claim(item.claim, reduction_rounds),
                    }
                )
                for item in mandatory
            ]
            mandatory_rendered = self._render(mandatory, gaps)

        dropped_optional: list[StableId] = []
        dropped_reasons: dict[str, str] = {}
        selected_optional: list[WriterContextItem] = []
        if self._count(mandatory_rendered) <= writer_token_budget:
            optional = self._order_optional_by_marginal_value(
                optional,
                need_by_id,
                ledger_by_id,
            )
            for item in optional:
                proposed_items = [*mandatory, *selected_optional, item]
                proposed = self._render(proposed_items, gaps)
                writer_fits = self._count(proposed) <= writer_token_budget
                evidence_fits = evidence_tokens(proposed_items) <= evidence_ledger_token_budget
                if writer_fits and evidence_fits:
                    selected_optional.append(item)
                else:
                    dropped_optional.append(item.context_item_id)
                    dropped_reasons[item.context_item_id.root] = (
                        "writer_token_budget" if not writer_fits else "evidence_ledger_token_budget"
                    )
        else:
            for item in optional:
                dropped_optional.append(item.context_item_id)
                dropped_reasons[item.context_item_id.root] = "mandatory_overflow"

        selected = [*mandatory, *selected_optional]
        selected_ledger_ids = {
            ledger_id for item in selected for ledger_id in item.evidence_ledger_ids
        }
        ledger = EvidenceLedger(
            contract_version="evidence_ledger.v1",
            entries=tuple(
                entry for entry in ledger_entries if entry.ledger_id in selected_ledger_ids
            ),
            rendered_tokens=0,
        )
        ledger_tokens = self._evidence_tokens(ledger.entries)
        ledger = ledger.model_copy(update={"rendered_tokens": ledger_tokens})
        rendered = self._render(selected, gaps)
        rendered_tokens = self._count(rendered)
        status = ContextAssemblyStatus.READY
        diagnostics: list[str] = []
        if rendered_tokens > writer_token_budget:
            status = ContextAssemblyStatus.CONTEXT_BUDGET_INSUFFICIENT
            diagnostics.append("MANDATORY_CONCLUSIONS_EXCEED_WRITER_BUDGET")
        elif ledger_tokens > evidence_ledger_token_budget:
            status = ContextAssemblyStatus.EVIDENCE_INSUFFICIENT
            diagnostics.append("EVIDENCE_LEDGER_BUDGET_EXCEEDED")
        elif conflicts:
            status = ContextAssemblyStatus.EVIDENCE_INSUFFICIENT
            diagnostics.append("UNRESOLVED_CURRENT_RECORD_CONFLICT")

        mandatory_tokens = self._count(self._render(mandatory, ()))
        optional_tokens = self._count(self._render(selected_optional, ()))
        header_gap_tokens = max(0, rendered_tokens - mandatory_tokens - optional_tokens)
        budget_report = WriterContextBudgetReport(
            tokenizer=self._tokenizer_name,
            tokenizer_version=self._tokenizer_version,
            configured_writer_token_budget=writer_token_budget,
            actual_rendered_writer_tokens=rendered_tokens,
            evidence_ledger_tokens=ledger_tokens,
            mandatory_conclusion_tokens=mandatory_tokens,
            optional_conclusion_tokens=optional_tokens,
            header_citation_gap_tokens=header_gap_tokens,
            deduplicated_item_count=len(normalized.duplicate_unit_ids),
            superseded_item_count=len(normalized.superseded_unit_ids),
            dropped_optional_ids=tuple(dropped_optional),
            dropped_optional_reasons=dropped_reasons,
            reduction_rounds=reduction_rounds,
            final_status=status,
        )
        ledger_bytes = canonical_json_bytes(ledger.model_dump(mode="json"))
        ledger_ref = ArtifactRef(
            artifact_id=sha256_id(ledger_bytes),
            media_type="application/vnd.novel-agent.evidence-ledger+json",
            byte_length=len(ledger_bytes),
            schema_version=SchemaVersion("1.0.0"),
        )
        package = WriterContextPackage(
            contract_version=self.contract_version,
            task_contract=task,
            basis_commit_id=basis_commit_id,
            basis_snapshot_id=basis_snapshot_id,
            arm=arm,
            continuity_constraints=self._section(
                selected, WriterContextSection.CONTINUITY_CONSTRAINTS
            ),
            current_world_state=self._section(selected, WriterContextSection.CURRENT_WORLD_STATE),
            relationship_and_emotion=self._section(
                selected, WriterContextSection.RELATIONSHIP_AND_EMOTION
            ),
            causal_history=self._section(selected, WriterContextSection.CAUSAL_HISTORY),
            knowledge_and_disclosure=self._section(
                selected, WriterContextSection.KNOWLEDGE_AND_DISCLOSURE
            ),
            plan_and_obligations=self._section(selected, WriterContextSection.PLAN_AND_OBLIGATIONS),
            long_range_callbacks=self._section(selected, WriterContextSection.LONG_RANGE_CALLBACKS),
            gaps=gaps,
            budget_report=budget_report,
            evidence_ledger_ref=ledger_ref,
            lineage=ContextLineage(
                need_ids=tuple(dict.fromkeys(need.need_id for need in needs)),
                retrieval_unit_ids=tuple(item.unit_id for item in normalized.units),
                assembler_version=self.version,
                normalized_unit_count=len(normalized.units),
                canonical_alias_receipts=normalized.canonical_alias_receipts,
                canonical_alias_receipt_refs=tuple(
                    canonical_alias_receipt_ref(item)
                    for item in normalized.canonical_alias_receipts
                ),
            ),
            rendered_context=rendered,
        )
        return WriterContextAssemblyResult(
            status=status,
            package=package,
            evidence_ledger=ledger,
            diagnostic_codes=tuple(diagnostics),
            assembler_version=self.version,
        )

    @classmethod
    def _order_optional_by_marginal_value(
        cls,
        items: list[WriterContextItem],
        need_by_id: dict[StableId, Stage1MemoryNeed],
        ledger_by_id: dict[StableId, EvidenceLedgerEntry],
    ) -> list[WriterContextItem]:
        """Round-robin needs so one broad entity cannot consume the audit budget."""

        groups: dict[StableId, list[WriterContextItem]] = {}
        group_priorities: dict[StableId, int] = {}
        for item in items:
            matching_needs = tuple(
                sorted(
                    (need_by_id[need_id] for need_id in item.need_ids if need_id in need_by_id),
                    key=lambda need: (-need.priority, need.need_id.root),
                )
            )
            group_id = (
                matching_needs[0].need_id
                if matching_needs
                else StableId(f"needless.{item.context_item_id.root}"[:128])
            )
            groups.setdefault(group_id, []).append(item)
            group_priorities[group_id] = matching_needs[0].priority if matching_needs else 0

        for group in groups.values():
            group.sort(
                key=lambda item: cls._optional_item_order(
                    item,
                    ledger_by_id,
                    need_by_id,
                )
            )
        group_ids = sorted(
            groups,
            key=lambda group_id: (-group_priorities[group_id], group_id.root),
        )
        ordered: list[WriterContextItem] = []
        for index in range(max((len(group) for group in groups.values()), default=0)):
            ordered.extend(
                groups[group_id][index] for group_id in group_ids if index < len(groups[group_id])
            )
        return ordered

    @classmethod
    def _optional_item_order(
        cls,
        item: WriterContextItem,
        ledger_by_id: dict[StableId, EvidenceLedgerEntry],
        need_by_id: dict[StableId, Stage1MemoryNeed],
    ) -> tuple[float, float, float, int, str]:
        chapter = cls._item_evidence_chapter(item, ledger_by_id)
        matching_needs = tuple(
            need_by_id[need_id] for need_id in item.need_ids if need_id in need_by_id
        )
        relevance = max(
            (cls._claim_query_relevance(item.claim, need.query_text) for need in matching_needs),
            default=0.0,
        )
        if item.section is WriterContextSection.LONG_RANGE_CALLBACKS:
            # Callback retrieval exists specifically to surface distant setup;
            # newest-first ordering systematically erases the long-range signal.
            return (
                -relevance,
                1 if chapter < 0 else 0,
                chapter,
                cls._item_evidence_ref_count(item, ledger_by_id),
                item.context_item_id.root,
            )
        if any(
            need.need_type in {"continuity_constraint", "eligibility_and_destination"}
            for need in matching_needs
        ):
            return (
                1 if chapter < 0 else 0,
                chapter,
                -relevance,
                cls._item_evidence_ref_count(item, ledger_by_id),
                item.context_item_id.root,
            )
        return (
            -relevance,
            1 if chapter < 0 else 0,
            -chapter,
            cls._item_evidence_ref_count(item, ledger_by_id),
            item.context_item_id.root,
        )

    @classmethod
    def _claim_query_relevance(cls, claim: str, query: str) -> float:
        terms = cls._query_terms(query)
        if not terms:
            return 0.0
        folded = claim.casefold()
        return sum(term in folded for term in terms) / len(terms)

    @staticmethod
    def _item_evidence_chapter(
        item: WriterContextItem,
        ledger_by_id: dict[StableId, EvidenceLedgerEntry],
    ) -> int:
        chapters: list[int] = []
        for ledger_id in item.evidence_ledger_ids:
            entry = ledger_by_id.get(ledger_id)
            if entry is None:
                continue
            for evidence in entry.evidence_refs:
                if evidence.chapter_id is None:
                    continue
                if evidence.chapter_id.root.endswith(
                    ".prelude"
                ) or evidence.chapter_id.root.startswith("prelude."):
                    chapters.append(0)
                    continue
                match = re.search(r"(?:^|[._:-])(\d+)$", evidence.chapter_id.root)
                if match is not None:
                    chapters.append(int(match.group(1)))
        return max(chapters, default=-1)

    @staticmethod
    def _item_evidence_ref_count(
        item: WriterContextItem,
        ledger_by_id: dict[StableId, EvidenceLedgerEntry],
    ) -> int:
        return sum(
            len(entry.evidence_refs)
            for ledger_id in item.evidence_ledger_ids
            if (entry := ledger_by_id.get(ledger_id)) is not None
        )

    def _claims(
        self,
        normalized: NormalizedRetrievalSet,
        needs: tuple[Stage1MemoryNeed, ...],
        basis_commit: CommitId,
        *,
        unit_need_ids: Mapping[StableId, tuple[StableId, ...]] | None = None,
    ) -> tuple[
        list[WriterContextItem],
        list[EvidenceLedgerEntry],
        tuple[ContextGap, ...],
    ]:
        claims: list[WriterContextItem] = []
        ledger: list[EvidenceLedgerEntry] = []
        gaps: list[ContextGap] = []
        grounded_kinds = {
            RetrievalUnitKind.GROUNDED_BLOCK,
            RetrievalUnitKind.GROUNDED_SPAN,
        }
        satisfied_mandatory_need_ids: set[StableId] = set()
        need_by_id = {need.need_id: need for need in needs}
        for unit in normalized.units:
            explicit_need_ids = () if unit_need_ids is None else unit_need_ids.get(unit.unit_id, ())
            compatible = (
                tuple(need_by_id[need_id] for need_id in explicit_need_ids if need_id in need_by_id)
                if explicit_need_ids
                else tuple(need for need in needs if self._need_matches(need, unit))
            )
            default_section = self._section_for_unit(unit)
            section_group_items: tuple[
                tuple[WriterContextSection, tuple[Stage1MemoryNeed, ...]], ...
            ]
            if explicit_need_ids and unit.unit_kind in grounded_kinds:
                # A chapter block may support unrelated Needs in the same
                # section (for example, a health constraint and a marriage
                # obligation). Extract each Need independently.
                section_group_items = tuple(
                    ((need.expected_section or default_section), (need,))
                    for need in compatible
                    if self._unit_is_legal_for_need(need, unit)
                )
            elif explicit_need_ids:
                section_groups: dict[WriterContextSection, tuple[Stage1MemoryNeed, ...]] = {}
                for need in compatible:
                    if not self._unit_is_legal_for_need(need, unit):
                        continue
                    section = need.expected_section or default_section
                    section_groups[section] = (
                        *section_groups.get(section, ()),
                        need,
                    )
                section_group_items = tuple(section_groups.items())
            else:
                section = (
                    compatible[0].expected_section
                    if compatible and compatible[0].expected_section is not None
                    else default_section
                )
                section_group_items = ((section, compatible),)
            plan_node_ids = (
                (StableId(unit.unit_id.root.removeprefix("anchor.")),)
                if unit.unit_kind in {RetrievalUnitKind.PLAN_ANCHOR, RetrievalUnitKind.ARC_ANCHOR}
                and not unit.evidence_refs
                else ()
            )
            for section_index, (section, section_needs) in enumerate(section_group_items):
                claim_text = unit.text
                evidence_refs = unit.evidence_refs
                if unit.unit_kind in grounded_kinds:
                    # One raw block can contain distinct facts relevant to
                    # continuity and callback Needs. Compress independently per
                    # Writer section so each claim gets its own precise spans.
                    claim_text, evidence_refs = self._extract_grounded_claim(
                        unit,
                        section_needs,
                    )
                if not claim_text or (not evidence_refs and not plan_node_ids):
                    gaps.append(
                        ContextGap(
                            gap_id=StableId(
                                (
                                    f"gap.untraceable.{section.value}.{unit.unit_id.root}"
                                    if unit.unit_kind in grounded_kinds
                                    else f"gap.untraceable.{unit.unit_id.root}"
                                )[:128]
                            ),
                            description=f"retrieved claim has no legal evidence: {unit.text[:160]}",
                            need_ids=tuple(need.need_id for need in section_needs),
                        )
                    )
                    continue
                grounded_identity = "|".join(
                    (
                        section.value,
                        unit.unit_id.root,
                        *(need.need_id.root for need in section_needs),
                    )
                )
                ledger_id = (
                    StableId(
                        "ledger.grounded."
                        + sha256_id(grounded_identity.encode("utf-8")).root.removeprefix("sha256:")
                    )
                    if unit.unit_kind in grounded_kinds
                    else StableId(f"ledger.{unit.unit_id.root}"[:128])
                )
                ledger.append(
                    EvidenceLedgerEntry(
                        ledger_id=ledger_id,
                        evidence_refs=evidence_refs,
                        plan_node_ids=plan_node_ids,
                        claim_excerpt=claim_text[:240],
                        source_commit=basis_commit,
                        information_scope=unit.access_scope,
                        need_ids=tuple(need.need_id for need in section_needs),
                        retrieval_unit_ids=(unit.unit_id,),
                    )
                )
                newly_satisfied_mandatory = tuple(
                    need
                    for need in section_needs
                    if need.requirement is RequirementLevel.MANDATORY
                    and need.need_id not in satisfied_mandatory_need_ids
                )
                # A RetrievalUnit may be mandatory in its original Stage 1
                # section. When explicit Need lineage projects that unit into a
                # different Writer section, the source flag must not make every
                # reuse mandatory. The current Need contract owns mandatory
                # closure for an explicitly selected Stage 2M context.
                is_mandatory = (
                    not explicit_need_ids and unit.mandatory and section_index == 0
                ) or bool(newly_satisfied_mandatory)
                if is_mandatory:
                    satisfied_mandatory_need_ids.update(
                        need.need_id
                        for need in section_needs
                        if need.requirement is RequirementLevel.MANDATORY
                    )
                claims.append(
                    WriterContextItem(
                        context_item_id=(
                            StableId(
                                "context-item.grounded."
                                + sha256_id(grounded_identity.encode("utf-8")).root.removeprefix(
                                    "sha256:"
                                )
                            )
                            if unit.unit_kind in grounded_kinds
                            else StableId(f"context-item.{section.value}.{unit.unit_id.root}"[:128])
                        ),
                        section=section,
                        claim=self._clean_claim(claim_text),
                        validity=self._validity(unit),
                        mandatory=is_mandatory,
                        confidence=(1.0 if unit.support_status in {None, "supported"} else 0.75),
                        need_ids=tuple(need.need_id for need in section_needs),
                        retrieval_unit_ids=(unit.unit_id,),
                        evidence_ledger_ids=(ledger_id,),
                    )
                )
        return claims, ledger, tuple(gaps)

    @staticmethod
    def _unit_is_legal_for_need(need: Stage1MemoryNeed, unit: RetrievalUnit) -> bool:
        """Keep Plan intent and observed history in distinct Writer sections."""

        plan_kinds = {
            RetrievalUnitKind.PLAN_ANCHOR,
            RetrievalUnitKind.ARC_ANCHOR,
        }
        if need.query_intent.value in {"plan_node", "plan_obligation"}:
            return unit.unit_kind in plan_kinds
        return unit.unit_kind not in plan_kinds

    @classmethod
    def _extract_grounded_claim(
        cls,
        unit: RetrievalUnit,
        needs: tuple[Stage1MemoryNeed, ...],
    ) -> tuple[str, tuple[EvidenceRef, ...]]:
        """Project a raw block into a bounded set of precise, query-relevant excerpts."""

        if not unit.evidence_refs or not needs:
            return "", ()
        segment_list: list[tuple[int, int, str]] = []
        content_start = cls._narrative_content_start(unit.text)
        sentence_pattern = r"[^\u3002\uff01\uff1f!?;\uff1b\n]+[\u3002\uff01\uff1f!?;\uff1b]?"
        for match in re.finditer(sentence_pattern, unit.text):
            if match.start() < content_start:
                continue
            raw = match.group()
            text = raw.strip()
            if not text:
                continue
            leading = len(raw) - len(raw.lstrip())
            segment_list.append(
                (match.start() + leading, match.start() + leading + len(text), text)
            )
        segments = tuple(segment_list)
        if not segments:
            return "", ()
        query = " ".join(need.query_text for need in needs)
        raw_terms = cls._query_terms(query)
        frequency_limit = max(2, len(segments) // 20)
        terms = tuple(
            term
            for term in raw_terms
            if sum(term in segment[2].casefold() for segment in segments) <= frequency_limit
        )
        if not terms:
            terms = raw_terms
        frequencies = {
            term: max(1, sum(term in segment[2].casefold() for segment in segments))
            for term in terms
        }

        def score(segment: tuple[int, int, str]) -> tuple[float, int, int]:
            start, _end, text = segment
            folded = text.casefold()
            matches = sum(1.0 / frequencies[term] for term in terms if term in folded)
            return matches, min(len(text), 180), -start

        ranked = sorted(segments, key=score, reverse=True)
        positive = [segment for segment in ranked if score(segment)[0] > 0]
        chosen: list[tuple[int, int, str]] = []
        projected_chars = 0
        for segment in positive or list(ranked):
            segment_chars = min(len(segment[2]), 180)
            if len(chosen) >= 3 and projected_chars + segment_chars > 720:
                continue
            chosen.append(segment)
            projected_chars += segment_chars
            if len(chosen) >= 10:
                break
        excerpts: list[str] = []
        refs: list[EvidenceRef] = []
        base = unit.evidence_refs[0]
        if base.span is None:
            return "", ()
        for index, (start, end, text) in enumerate(chosen, start=1):
            clipped_start, clipped_end, clipped = cls._clip_excerpt(
                unit.text,
                start,
                end,
                text,
                terms,
            )
            if not clipped:
                continue
            excerpts.append(clipped)
            digest = quote_hash(clipped)
            evidence_id = StableId(
                f"evidence.extract.{digest.root.removeprefix('sha256:')[:24]}.{index}"
            )
            refs.append(
                base.model_copy(
                    update={
                        "evidence_id": evidence_id,
                        "quote_hash": digest,
                        "span": base.span.model_copy(
                            update={
                                "start": base.span.start + clipped_start,
                                "end": base.span.start + clipped_end,
                            }
                        ),
                    }
                )
            )
        claim = " ".join(excerpts)
        long_range_need_types = {"learning_foundation", "long_range_callback"}
        composite_need_types = {
            "behavioral_principle",
            "behavioral_profile",
            "behavioral_routine",
            "capability_history",
            "environment_and_resources",
            "environment_current_history",
            "environment_history",
            "peer_knowledge_history",
            "relationship_emotion",
        }
        claim_limit = (
            300
            if any(need.need_type in long_range_need_types for need in needs)
            else (180 if any(need.need_type in composite_need_types for need in needs) else 140)
        )
        if len(claim) > claim_limit:
            claim = claim[: claim_limit - 1].rstrip() + "…"
        ordered_refs = tuple(
            ref
            for _index, ref in sorted(
                enumerate(refs),
                key=lambda item: (
                    item[1].span.start if item[1].span is not None else -1,
                    item[0],
                ),
            )
        )
        return claim, ordered_refs

    @staticmethod
    def _narrative_content_start(value: str) -> int:
        marker = re.search(
            r"(?im)^(?:第[一二三四五六七八九十百千万0-9]+卷(?:\s|$)|"
            r"序(?:章)?(?:\s|$)|楔子(?:\s|$)|prologue(?:\s|$))",
            value,
        )
        return 0 if marker is None else marker.start()

    @staticmethod
    def _query_terms(value: str) -> tuple[str, ...]:
        tokens = tuple(
            token
            for token in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", value.casefold())
            if len(token) >= 2
        )
        terms: list[str] = []
        for token in tokens:
            if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) > 2:
                terms.extend(token[index : index + 2] for index in range(len(token) - 1))
            else:
                terms.append(token)
        return tuple(dict.fromkeys(terms))

    @staticmethod
    def _clip_excerpt(
        source: str,
        start: int,
        end: int,
        text: str,
        terms: tuple[str, ...],
        *,
        limit: int = 180,
    ) -> tuple[int, int, str]:
        if len(text) <= limit:
            leading = len(source[start:end]) - len(source[start:end].lstrip())
            precise_start = start + leading
            return precise_start, precise_start + len(text), text
        folded = text.casefold()
        positions = [folded.find(term) for term in terms if folded.find(term) >= 0]
        center = min(positions) if positions else 0
        local_start = max(0, min(center - limit // 3, len(text) - limit))
        local_end = min(len(text), local_start + limit)
        clipped = text[local_start:local_end].strip()
        leading = len(text[local_start:local_end]) - len(text[local_start:local_end].lstrip())
        precise_start = start + local_start + leading
        return precise_start, precise_start + len(clipped), clipped

    @staticmethod
    def _need_matches(need: Stage1MemoryNeed, unit: RetrievalUnit) -> bool:
        section = need.expected_section
        if section is not None and section is not WriterContextAssembler._section_for_unit(unit):
            return False
        if need.entity_ids:
            return bool(set(need.entity_ids).intersection(unit.entity_ids))
        return section is not None

    @staticmethod
    def _section_for_unit(unit: RetrievalUnit) -> WriterContextSection:
        if unit.unit_kind is RetrievalUnitKind.STATE_ANCHOR:
            return WriterContextSection.CURRENT_WORLD_STATE
        if unit.unit_kind is RetrievalUnitKind.RELATION_ANCHOR:
            return WriterContextSection.RELATIONSHIP_AND_EMOTION
        if unit.unit_kind in {
            RetrievalUnitKind.EVENT_ANCHOR,
            RetrievalUnitKind.SCENE_ANCHOR,
            RetrievalUnitKind.CHAPTER_ANCHOR,
        }:
            return WriterContextSection.CAUSAL_HISTORY
        if unit.unit_kind in {
            RetrievalUnitKind.PLAN_ANCHOR,
            RetrievalUnitKind.ARC_ANCHOR,
        }:
            return WriterContextSection.PLAN_AND_OBLIGATIONS
        predicate = (unit.predicate or "").casefold()
        if any(token in predicate for token in ("know", "secret", "truth", "disclos")):
            return WriterContextSection.KNOWLEDGE_AND_DISCLOSURE
        return WriterContextSection.CONTINUITY_CONSTRAINTS

    @staticmethod
    def _validity(unit: RetrievalUnit) -> WriterContextValidity:
        if unit.unit_kind in {
            RetrievalUnitKind.PLAN_ANCHOR,
            RetrievalUnitKind.ARC_ANCHOR,
        }:
            return WriterContextValidity.PLANNED
        if unit.unit_kind in {
            RetrievalUnitKind.EVENT_ANCHOR,
            RetrievalUnitKind.SCENE_ANCHOR,
            RetrievalUnitKind.CHAPTER_ANCHOR,
            RetrievalUnitKind.GROUNDED_BLOCK,
            RetrievalUnitKind.GROUNDED_SPAN,
        }:
            return WriterContextValidity.HISTORICAL
        return WriterContextValidity.CURRENT

    @staticmethod
    def _clean_claim(value: str) -> str:
        # Anchor text may append a prose excerpt after the structured conclusion.
        # The excerpt belongs in EvidenceLedger, never in the Writer conclusion.
        first_line = next((line for line in value.splitlines() if line.strip()), value)
        return re.sub(r"\s+", " ", first_line).strip()

    @classmethod
    def _reduce_claim(cls, value: str, round_number: int) -> str:
        cleaned = cls._clean_claim(value)
        limit = 240 if round_number == 1 else 120
        return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "…"

    @staticmethod
    def _section(
        items: list[WriterContextItem],
        section: WriterContextSection,
    ) -> tuple[WriterContextItem, ...]:
        return tuple(item for item in items if item.section is section)

    @staticmethod
    def _default_token_count(text: str) -> int:
        # Conservative deterministic fallback: CJK codepoints count as one,
        # whitespace-delimited ASCII fragments count approximately as tokens.
        cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
        non_cjk = len(re.findall(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_\u3400-\u9fff]", text))
        return max(1, cjk + non_cjk)

    def _evidence_tokens(self, entries: tuple[EvidenceLedgerEntry, ...]) -> int:
        """Count the semantic evidence rendering, excluding audit-only hash metadata."""

        lines: list[str] = []
        for entry in entries:
            citations = [
                self._render_evidence_citation(reference) for reference in entry.evidence_refs
            ]
            citations.extend(f"plan:{value.root}" for value in entry.plan_node_ids)
            lines.append(f"[{entry.ledger_id.root}] {entry.claim_excerpt} ({'|'.join(citations)})")
        return self._count("\n".join(lines)) if lines else 0

    @staticmethod
    def _render_evidence_citation(reference: EvidenceRef) -> str:
        evidence_id = reference.evidence_id.root
        chapter = reference.chapter_id
        span = reference.span
        location = chapter.root if chapter is not None else "no-chapter"
        if span is not None:
            location = f"{location}:{span.block_id.root}:{span.start}-{span.end}"
        return f"{evidence_id}@{location}"

    def _render(
        self,
        items: list[WriterContextItem],
        gaps: tuple[ContextGap, ...],
    ) -> str:
        labels = {
            WriterContextSection.CONTINUITY_CONSTRAINTS: "当前必须遵守",
            WriterContextSection.CURRENT_WORLD_STATE: "当前世界状态",
            WriterContextSection.RELATIONSHIP_AND_EMOTION: "关系与情绪",
            WriterContextSection.CAUSAL_HISTORY: "相关因果历史",
            WriterContextSection.KNOWLEDGE_AND_DISCLOSURE: "知识与披露边界",
            WriterContextSection.PLAN_AND_OBLIGATIONS: "计划与未决义务",
            WriterContextSection.LONG_RANGE_CALLBACKS: "长程伏笔与回收",
        }
        lines: list[str] = []
        for section in WriterContextSection:
            section_items = [item for item in items if item.section is section]
            if not section_items:
                continue
            lines.append(f"[{labels[section]}]")
            for item in section_items:
                citations = ",".join(value.root for value in item.evidence_ledger_ids)
                lines.append(f"- {item.claim} [{citations}]")
        if gaps:
            lines.append("[缺口与冲突]")
            lines.extend(f"- {gap.description}" for gap in gaps)
        return "\n".join(lines)
