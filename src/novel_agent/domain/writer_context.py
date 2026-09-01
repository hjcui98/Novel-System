"""Public production contracts for Stage 2M tasks and Writer Context."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from novel_agent.domain.artifacts import ArtifactRef, PlanRootRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.canonical import CanonicalAliasReceipt
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, StableId
from novel_agent.domain.model_calls import ModelCallRecord
from novel_agent.domain.text import EvidenceRef, QuoteHash


class BenchmarkInformationProfile(StrEnum):
    VISIBLE_AT_CUTOFF = "visible_at_cutoff"
    TASK_INTENT_ONLY = "task_intent_only"
    AUTHOR_PLAN_CONDITIONED = "author_plan_conditioned"


class BenchmarkTaskContract(DomainModel):
    task_id: StableId
    task_text: str = Field(min_length=1)
    task_kind: Literal["memory_context_for_target_range"] = "memory_context_for_target_range"
    checkpoint_chapter: int = Field(ge=0)
    target_chapter_start: int = Field(ge=1)
    target_chapter_end: int = Field(ge=1)
    information_profile: BenchmarkInformationProfile
    task_template_version: str = Field(min_length=1)
    output_contract_version: str = Field(min_length=1)
    # Derived from the case AuthorPlanningContext; empty for blind profiles.
    task_intent: str = ""
    planning_context_ref: ArtifactId | None = None
    planning_context_hash: ArtifactId | None = None

    @model_validator(mode="after")
    def validate_range(self) -> BenchmarkTaskContract:
        if self.target_chapter_end < self.target_chapter_start:
            raise ValueError("benchmark task target range is invalid")
        if self.target_chapter_start <= self.checkpoint_chapter:
            raise ValueError("benchmark task target range must follow its checkpoint")
        return self


class PublicCheckpointPayload(DomainModel):
    case_id: StableId
    project_id: ProjectId
    target_range: tuple[int, int]
    history_range: tuple[int, int]
    task_contract: BenchmarkTaskContract
    plan_root_ref: PlanRootRef | None = None
    public_input_hash: ArtifactId


class WriterContextSection(StrEnum):
    CONTINUITY_CONSTRAINTS = "continuity_constraints"
    CURRENT_WORLD_STATE = "current_world_state"
    RELATIONSHIP_AND_EMOTION = "relationship_and_emotion"
    CAUSAL_HISTORY = "causal_history"
    KNOWLEDGE_AND_DISCLOSURE = "knowledge_and_disclosure"
    PLAN_AND_OBLIGATIONS = "plan_and_obligations"
    LONG_RANGE_CALLBACKS = "long_range_callbacks"


class WriterContextValidity(StrEnum):
    CURRENT = "current"
    HISTORICAL = "historical"
    PLANNED = "planned"
    UNCERTAIN = "uncertain"


class EvidenceResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    BASIS_MISMATCH = "basis_mismatch"
    CUTOFF_VIOLATION = "cutoff_violation"


class SemanticSupportStatus(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    CONTRADICTED = "contradicted"


class NeedEvidenceSemanticStatus(StrEnum):
    """Model relevance verdict for one public Need/facet evidence set."""

    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    UNSUPPORTED = "UNSUPPORTED"
    UNRESOLVED = "UNRESOLVED"


class NeedEvidenceJudgmentBatchStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class ClaimReductionLevel(StrEnum):
    FULL = "full"
    NARROW = "narrow"


class CutoffAttestation(DomainModel):
    attestation_id: StableId
    basis_commit_id: CommitId
    basis_snapshot_id: StableId
    checkpoint_chapter: int = Field(ge=0)
    information_scope: str = Field(min_length=1)
    retrieval_unit_ids: tuple[StableId, ...]
    producer: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)


class ClaimSupportReceipt(DomainModel):
    receipt_id: StableId
    support_group_id: StableId
    claim_id: StableId
    claim_text_hash: ArtifactId
    need_ids: tuple[StableId, ...]
    need_facet_ids: tuple[StableId, ...]
    retrieval_unit_ids: tuple[StableId, ...]
    evidence_refs: tuple[EvidenceRef, ...] = ()
    plan_node_ids: tuple[StableId, ...] = ()
    evidence_resolution_status: EvidenceResolutionStatus
    semantic_support_status: SemanticSupportStatus
    counter_evidence_refs: tuple[EvidenceRef, ...] = ()
    basis_commit_id: CommitId
    basis_snapshot_id: StableId
    cutoff_attestation_ref: ArtifactRef
    information_scope: str = Field(min_length=1)
    producer: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)
    producer_input_hash: ArtifactId | None = None
    producer_output_hash: ArtifactId | None = None
    producer_input_ref: ArtifactRef | None = None
    producer_output_ref: ArtifactRef | None = None
    model_call_record: ModelCallRecord | None = None
    verifier_input_hash: ArtifactId | None = None
    verifier_output_hash: ArtifactId | None = None
    verifier_input_ref: ArtifactRef | None = None
    verifier_output_ref: ArtifactRef | None = None
    verification_model_call_record: ModelCallRecord | None = None

    @model_validator(mode="after")
    def validate_verified_support(self) -> ClaimSupportReceipt:
        if not self.evidence_refs and not self.plan_node_ids:
            raise ValueError("support receipt requires evidence or legal Plan provenance")
        if self.semantic_support_status is SemanticSupportStatus.VERIFIED and (
            self.evidence_resolution_status is not EvidenceResolutionStatus.RESOLVED
            or self.counter_evidence_refs
            or not self.need_facet_ids
        ):
            raise ValueError("verified support requires resolved, uncontradicted public facets")
        audit_fields = (
            self.producer_input_hash,
            self.producer_output_hash,
            self.producer_input_ref,
            self.producer_output_ref,
            self.model_call_record,
        )
        if any(item is not None for item in audit_fields) and any(
            item is None for item in audit_fields
        ):
            raise ValueError("model-produced support requires a complete audit binding")
        verifier_audit_fields = (
            self.verifier_input_hash,
            self.verifier_output_hash,
            self.verifier_input_ref,
            self.verifier_output_ref,
            self.verification_model_call_record,
        )
        if any(item is not None for item in verifier_audit_fields) and any(
            item is None for item in verifier_audit_fields
        ):
            raise ValueError("semantic verification requires a complete audit binding")
        if self.model_call_record is not None and self.verification_model_call_record is None:
            raise ValueError("model-proposed support requires an independent verification call")
        if self.producer_input_ref is not None and (
            self.producer_input_ref.artifact_id != self.producer_input_hash
            or self.producer_output_ref is None
            or self.producer_output_ref.artifact_id != self.producer_output_hash
            or self.verifier_input_ref is None
            or self.verifier_input_ref.artifact_id != self.verifier_input_hash
            or self.verifier_output_ref is None
            or self.verifier_output_ref.artifact_id != self.verifier_output_hash
        ):
            raise ValueError("model support audit hashes must match retained artifacts")
        return self


class ClaimSupportGroup(DomainModel):
    support_group_id: StableId
    claim_id: StableId
    need_ids: tuple[StableId, ...]
    need_facet_ids: tuple[StableId, ...]
    retrieval_unit_ids: tuple[StableId, ...]
    evidence_refs: tuple[EvidenceRef, ...] = ()
    plan_node_ids: tuple[StableId, ...] = ()
    evidence_resolution_status: EvidenceResolutionStatus
    semantic_support_status: SemanticSupportStatus
    support_receipt_ref: ArtifactRef
    producer: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)
    counter_evidence_refs: tuple[EvidenceRef, ...] = ()
    cutoff_attestation_ref: ArtifactRef


class ClaimVariant(DomainModel):
    claim_variant_id: StableId
    claim_id: StableId
    support_group_id: StableId
    claim_text: str = Field(min_length=1)
    claim_text_hash: ArtifactId
    covered_need_facet_ids: tuple[StableId, ...]
    support_receipt_ref: ArtifactRef
    token_cost: int = Field(ge=1)
    reduction_level: ClaimReductionLevel
    producer: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)


class WriterContextItem(DomainModel):
    context_item_id: StableId
    section: WriterContextSection
    claim: str = Field(min_length=1)
    validity: WriterContextValidity
    mandatory: bool
    confidence: float = Field(ge=0.0, le=1.0)
    need_ids: tuple[StableId, ...]
    retrieval_unit_ids: tuple[StableId, ...]
    evidence_ledger_ids: tuple[StableId, ...]
    supersedes_item_ids: tuple[StableId, ...] = ()
    claim_variant_id: StableId | None = None
    support_group_id: StableId | None = None
    need_facet_ids: tuple[StableId, ...] = ()
    support_receipt_ref: ArtifactRef | None = None

    @model_validator(mode="after")
    def validate_grounding(self) -> WriterContextItem:
        if self.validity is not WriterContextValidity.UNCERTAIN and not self.evidence_ledger_ids:
            raise ValueError("non-uncertain writer context claim requires evidence")
        if len(self.retrieval_unit_ids) != len(set(self.retrieval_unit_ids)):
            raise ValueError("writer context retrieval unit ids must be unique")
        return self


class ContextGap(DomainModel):
    gap_id: StableId
    description: str = Field(min_length=1)
    need_ids: tuple[StableId, ...] = ()
    conflict: bool = False


class EvidenceLedgerEntry(DomainModel):
    ledger_id: StableId
    evidence_refs: tuple[EvidenceRef, ...] = ()
    plan_node_ids: tuple[StableId, ...] = ()
    claim_excerpt: str = Field(min_length=1)
    source_commit: CommitId
    information_scope: str = Field(min_length=1)
    need_ids: tuple[StableId, ...] = ()
    retrieval_unit_ids: tuple[StableId, ...] = ()
    support_group_id: StableId | None = None
    need_facet_ids: tuple[StableId, ...] = ()
    support_receipt_ref: ArtifactRef | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> EvidenceLedgerEntry:
        if not self.evidence_refs and not self.plan_node_ids:
            raise ValueError("evidence ledger entry requires source evidence or plan provenance")
        return self


class EvidenceLedger(DomainModel):
    contract_version: str = Field(min_length=1)
    entries: tuple[EvidenceLedgerEntry, ...] = ()
    rendered_tokens: int = Field(ge=0)


class ContextAssemblyStatus(StrEnum):
    READY = "READY"
    NEEDS_REDUCTION = "NEEDS_REDUCTION"
    CONTEXT_BUDGET_INSUFFICIENT = "CONTEXT_BUDGET_INSUFFICIENT"
    EVIDENCE_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"
    POLICY_BLOCKED = "POLICY_BLOCKED"


class WriterContextBudgetReport(DomainModel):
    tokenizer: str = Field(min_length=1)
    tokenizer_version: str = Field(min_length=1)
    configured_writer_token_budget: int = Field(ge=1)
    actual_rendered_writer_tokens: int = Field(ge=0)
    evidence_ledger_tokens: int = Field(ge=0)
    mandatory_conclusion_tokens: int = Field(ge=0)
    optional_conclusion_tokens: int = Field(ge=0)
    header_citation_gap_tokens: int = Field(ge=0)
    deduplicated_item_count: int = Field(ge=0)
    superseded_item_count: int = Field(ge=0)
    dropped_optional_ids: tuple[StableId, ...] = ()
    dropped_optional_reasons: dict[str, str] = Field(default_factory=dict)
    reduction_rounds: int = Field(ge=0)
    final_status: ContextAssemblyStatus

    @model_validator(mode="after")
    def validate_ready_budget(self) -> WriterContextBudgetReport:
        if (
            self.final_status is ContextAssemblyStatus.READY
            and self.actual_rendered_writer_tokens > self.configured_writer_token_budget
        ):
            raise ValueError("READY writer context cannot exceed its token budget")
        return self


class ContextLineage(DomainModel):
    need_ids: tuple[StableId, ...] = ()
    retrieval_unit_ids: tuple[StableId, ...] = ()
    assembler_version: str = Field(min_length=1)
    normalized_unit_count: int = Field(ge=0)
    canonical_alias_receipts: tuple[CanonicalAliasReceipt, ...] = ()
    canonical_alias_receipt_refs: tuple[ArtifactRef, ...] = ()
    selected_claim_variant_ids: tuple[StableId, ...] = ()
    context_assembly_spec_ref: ArtifactRef | None = None

    @model_validator(mode="after")
    def validate_alias_receipts(self) -> ContextLineage:
        if len(self.canonical_alias_receipts) != len(self.canonical_alias_receipt_refs):
            raise ValueError("canonical alias receipts and refs must appear together")
        return self


class WriterContextPackage(DomainModel):
    contract_version: str = Field(min_length=1)
    task_contract: BenchmarkTaskContract
    basis_commit_id: CommitId
    basis_snapshot_id: StableId
    arm: Literal["A", "B", "C"]
    continuity_constraints: tuple[WriterContextItem, ...] = ()
    current_world_state: tuple[WriterContextItem, ...] = ()
    relationship_and_emotion: tuple[WriterContextItem, ...] = ()
    causal_history: tuple[WriterContextItem, ...] = ()
    knowledge_and_disclosure: tuple[WriterContextItem, ...] = ()
    plan_and_obligations: tuple[WriterContextItem, ...] = ()
    long_range_callbacks: tuple[WriterContextItem, ...] = ()
    gaps: tuple[ContextGap, ...] = ()
    budget_report: WriterContextBudgetReport
    evidence_ledger_ref: ArtifactRef
    lineage: ContextLineage
    rendered_context: str

    @model_validator(mode="after")
    def validate_sections(self) -> WriterContextPackage:
        section_values = (
            (self.continuity_constraints, WriterContextSection.CONTINUITY_CONSTRAINTS),
            (self.current_world_state, WriterContextSection.CURRENT_WORLD_STATE),
            (self.relationship_and_emotion, WriterContextSection.RELATIONSHIP_AND_EMOTION),
            (self.causal_history, WriterContextSection.CAUSAL_HISTORY),
            (self.knowledge_and_disclosure, WriterContextSection.KNOWLEDGE_AND_DISCLOSURE),
            (self.plan_and_obligations, WriterContextSection.PLAN_AND_OBLIGATIONS),
            (self.long_range_callbacks, WriterContextSection.LONG_RANGE_CALLBACKS),
        )
        if any(item.section is not section for items, section in section_values for item in items):
            raise ValueError("writer context item is stored in the wrong section")
        return self


class FreezeReceipt(DomainModel):
    receipt_id: StableId
    public_input_hash: ArtifactId
    code_version: str = Field(min_length=1)
    run_config_hash: ArtifactId
    arm_artifact_hashes: dict[Literal["A", "B", "C"], ArtifactId]
    frozen_before_reveal: bool

    @model_validator(mode="after")
    def validate_arms(self) -> FreezeReceipt:
        if not self.frozen_before_reveal:
            raise ValueError("benchmark receipt must freeze artifacts before Gold reveal")
        if set(self.arm_artifact_hashes) != {"A", "B", "C"}:
            raise ValueError("freeze receipt must contain A, B, and C hashes")
        return self


class EvidenceSliceKind(StrEnum):
    PARAGRAPH = "paragraph"
    SENTENCE_WINDOW = "sentence_window"


class EvidenceSliceSourceRole(StrEnum):
    NARRATIVE = "narrative"
    HEADING = "heading"
    UNKNOWN = "unknown"


class EvidenceSlice(DomainModel):
    """One exact L0 paragraph or contiguous-sentence slice of a parent block.

    ``text`` is an exact substring of the parent ``TextBlock``; the slice id
    is stable over the parent identity plus the exact offsets and normalized
    text hash.  Heading/title-only units carry an explicit source role and are
    not narrative evidence by default.
    """

    slice_id: StableId
    parent_block_id: StableId
    chapter_id: StableId
    scene_id: StableId | None = None
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str = Field(min_length=1)
    object_hash: ArtifactId
    quote_hash: QuoteHash
    source_commit: CommitId
    snapshot_id: StableId
    access_scope: str = Field(min_length=1)
    slice_kind: EvidenceSliceKind
    source_role: EvidenceSliceSourceRole = EvidenceSliceSourceRole.NARRATIVE

    @model_validator(mode="after")
    def validate_slice(self) -> EvidenceSlice:
        if self.end < self.start:
            raise ValueError("evidence slice end must be greater than or equal to start")
        return self


class EvidenceLedgerEntryV2(DomainModel):
    """Evidence-first ledger entry: exact raw slice text plus full provenance.

    One entry stores one deduplicated slice text; ``need_ids`` aggregates every
    public Need that selected it.  ``dereference_receipt`` records whether the
    entry was re-read from its exact parent block text during assembly.
    """

    ledger_id: StableId
    evidence_slices: tuple[EvidenceSlice, ...]
    evidence_text: str = Field(min_length=1)
    evidence_refs: tuple[EvidenceRef, ...]
    retrieval_unit_ids: tuple[StableId, ...] = ()
    basis_commit_id: CommitId
    basis_snapshot_id: StableId
    cutoff_chapter: int = Field(ge=0)
    information_scope: str = Field(min_length=1)
    taint: str = "none"
    text_hash: ArtifactId
    span_hash: ArtifactId
    quote_hash: QuoteHash
    dereference_receipt: str = "verified_read"
    need_ids: tuple[StableId, ...]
    need_facet_ids: tuple[StableId, ...]

    @model_validator(mode="after")
    def validate_entry(self) -> EvidenceLedgerEntryV2:
        if not self.evidence_slices:
            raise ValueError("evidence ledger entry requires at least one exact slice")
        if len(self.evidence_slices) != len({slice_.slice_id for slice_ in self.evidence_slices}):
            raise ValueError("evidence ledger entry slices must be unique")
        if self.evidence_text != "".join(slice_.text for slice_ in self.evidence_slices):
            raise ValueError("evidence ledger entry text must match its exact slices")
        if not self.need_ids:
            raise ValueError("exposed ledger entry must bind at least one public Need")
        return self


class EvidenceLedgerV2(DomainModel):
    contract_version: str = "evidence_ledger.v2"
    entries: tuple[EvidenceLedgerEntryV2, ...] = ()
    rendered_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_ledger(self) -> EvidenceLedgerV2:
        ledger_ids = tuple(entry.ledger_id for entry in self.entries)
        if len(ledger_ids) != len(set(ledger_ids)):
            raise ValueError("evidence ledger entry ids must be unique")
        return self


class EvidenceGapKind(StrEnum):
    NO_SELECTED_EVIDENCE = "no_selected_evidence"
    BUDGET_EXCEEDED = "budget_exceeded"
    SLICE_OVERSIZED = "slice_oversized"
    CUTOFF_TAINTED = "cutoff_tainted"
    SCOPE_TAINTED = "scope_tainted"
    DEREFERENCE_FAILED = "dereference_failed"
    HEADING_SOURCE_ROLE = "heading_source_role"
    SEMANTIC_PARTIAL = "semantic_partial"
    SEMANTIC_UNSUPPORTED = "semantic_unsupported"
    SEMANTIC_UNRESOLVED = "semantic_unresolved"


class NeedEvidenceJudgmentBatchReceipt(DomainModel):
    """Auditable input/output boundary for one semantic-judge request."""

    batch_id: StableId
    request_id: StableId
    need_facet_ids: tuple[StableId, ...]
    slice_ids: tuple[StableId, ...]
    status: NeedEvidenceJudgmentBatchStatus
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    model: str = ""
    model_version: str = ""
    endpoint: str = ""
    error_category: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_batch(self) -> NeedEvidenceJudgmentBatchReceipt:
        if len(self.need_facet_ids) != len(set(self.need_facet_ids)):
            raise ValueError("semantic judgment batch Need facets must be unique")
        if len(self.slice_ids) != len(set(self.slice_ids)):
            raise ValueError("semantic judgment batch slices must be unique")
        if self.status is NeedEvidenceJudgmentBatchStatus.FAILED and self.error_category is None:
            raise ValueError("failed semantic judgment batch requires an error category")
        if (
            self.status is NeedEvidenceJudgmentBatchStatus.COMPLETED
            and self.error_category is not None
        ):
            raise ValueError("completed semantic judgment batch cannot carry an error category")
        return self


class NeedFacetSemanticReceipt(DomainModel):
    """Final semantic relevance receipt for one mandatory public facet."""

    need_id: StableId
    need_facet_id: StableId
    facet_kind: str = Field(min_length=1)
    mandatory: bool
    status: NeedEvidenceSemanticStatus
    evaluated_slice_ids: tuple[StableId, ...] = ()
    supporting_slice_ids: tuple[StableId, ...] = ()
    partial_slice_ids: tuple[StableId, ...] = ()
    unsupported_slice_ids: tuple[StableId, ...] = ()
    reason: str = Field(default="", max_length=4096)
    batch_receipt_ids: tuple[StableId, ...] = ()
    judge_version: str = Field(default="", min_length=0)

    @model_validator(mode="after")
    def validate_receipt(self) -> NeedFacetSemanticReceipt:
        buckets = (
            self.supporting_slice_ids,
            self.partial_slice_ids,
            self.unsupported_slice_ids,
        )
        all_ids = tuple(item for bucket in buckets for item in bucket)
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("semantic receipt slice classifications must be disjoint")
        if not set(all_ids).issubset(self.evaluated_slice_ids):
            raise ValueError("semantic receipt classifications must be evaluated slices")
        if len(self.evaluated_slice_ids) != len(set(self.evaluated_slice_ids)):
            raise ValueError("semantic receipt evaluated slices must be unique")
        if len(self.batch_receipt_ids) != len(set(self.batch_receipt_ids)):
            raise ValueError("semantic receipt batch refs must be unique")
        if self.status is NeedEvidenceSemanticStatus.SUPPORTED and not self.supporting_slice_ids:
            raise ValueError("SUPPORTED semantic receipt requires supporting slices")
        if self.status is NeedEvidenceSemanticStatus.PARTIAL and (
            self.supporting_slice_ids or not self.partial_slice_ids
        ):
            raise ValueError("PARTIAL semantic receipt requires only partial support")
        if self.status is NeedEvidenceSemanticStatus.UNSUPPORTED and (
            self.supporting_slice_ids or self.partial_slice_ids or not self.unsupported_slice_ids
        ):
            raise ValueError("UNSUPPORTED semantic receipt requires unsupported slices only")
        return self


class EvidenceFirstGap(DomainModel):
    gap_id: StableId
    need_ids: tuple[StableId, ...]
    need_facet_ids: tuple[StableId, ...]
    kind: EvidenceGapKind
    reason: str = Field(min_length=1)


class WriterContextEvidenceItem(DomainModel):
    """Writer-visible evidence item: purpose plus ledger refs or a typed gap.

    ``purpose`` is derived only from the public Need's semantic question /
    why-needed / facet metadata; it never rewrites material into a claim and
    never reads Gold or future text.  ``raw_preview`` is a bounded prefix of
    the exact Ledger text (never a model rewrite) with an explicit truncation
    flag.
    """

    item_id: StableId
    section: WriterContextSection
    need_ids: tuple[StableId, ...]
    need_facet_ids: tuple[StableId, ...]
    purpose: str = Field(min_length=1)
    evidence_ledger_ids: tuple[StableId, ...] = ()
    raw_preview: str = ""
    preview_truncated: bool = False
    source_scope: str = ""
    source_kind: str = ""
    validity: WriterContextValidity = WriterContextValidity.UNCERTAIN
    mandatory: bool = False
    selection_reason: str = ""
    semantic_status: NeedEvidenceSemanticStatus | None = None
    semantic_answering_ledger_ids: tuple[StableId, ...] = ()
    semantic_partial_ledger_ids: tuple[StableId, ...] = ()
    semantic_related_ledger_ids: tuple[StableId, ...] = ()
    advisory_artifact_refs: tuple[ArtifactRef, ...] = ()
    unverified: bool = False
    gap: EvidenceFirstGap | None = None

    @model_validator(mode="after")
    def validate_item(self) -> WriterContextEvidenceItem:
        if self.gap is not None:
            if (
                self.evidence_ledger_ids
                or self.raw_preview
                or self.semantic_answering_ledger_ids
                or self.semantic_partial_ledger_ids
                or self.semantic_related_ledger_ids
            ):
                raise ValueError("typed gap items cannot carry evidence or previews")
            if self.unverified and not self.advisory_artifact_refs:
                raise ValueError("unverified gap items require advisory artifact refs")
            return self
        if self.unverified or self.advisory_artifact_refs:
            raise ValueError("advisory markers must be typed gap items")
        if not self.evidence_ledger_ids:
            raise ValueError("writer context evidence item requires ledger refs or a typed gap")
        if not self.raw_preview:
            raise ValueError("writer context evidence item requires a raw preview")
        if len(self.evidence_ledger_ids) != len(set(self.evidence_ledger_ids)):
            raise ValueError("writer context evidence ledger ids must be unique")
        if len(self.semantic_answering_ledger_ids) != len(set(self.semantic_answering_ledger_ids)):
            raise ValueError("semantic answering ledger ids must be unique")
        if len(self.semantic_partial_ledger_ids) != len(set(self.semantic_partial_ledger_ids)):
            raise ValueError("semantic partial ledger ids must be unique")
        if len(self.semantic_related_ledger_ids) != len(set(self.semantic_related_ledger_ids)):
            raise ValueError("semantic related ledger ids must be unique")
        if not set(self.semantic_answering_ledger_ids).issubset(self.evidence_ledger_ids):
            raise ValueError("semantic answering refs must belong to the evidence item")
        if not set(self.semantic_partial_ledger_ids).issubset(self.evidence_ledger_ids):
            raise ValueError("semantic partial refs must belong to the evidence item")
        if not set(self.semantic_related_ledger_ids).issubset(self.evidence_ledger_ids):
            raise ValueError("semantic related refs must belong to the evidence item")
        return self


class WriterContextBudgetReportV2(DomainModel):
    tokenizer: str = Field(min_length=1)
    tokenizer_version: str = Field(min_length=1)
    configured_writer_token_budget: int = Field(ge=1)
    actual_rendered_writer_tokens: int = Field(ge=0)
    configured_ledger_token_budget: int = Field(ge=1)
    actual_rendered_ledger_tokens: int = Field(ge=0)
    item_count: int = Field(ge=0)
    evidence_item_count: int = Field(ge=0)
    gap_item_count: int = Field(ge=0)
    ledger_entry_count: int = Field(ge=0)
    dropped_slice_reasons: dict[str, str] = Field(default_factory=dict)
    final_status: ContextAssemblyStatus

    @model_validator(mode="after")
    def validate_ready_budget(self) -> WriterContextBudgetReportV2:
        if (
            self.final_status is ContextAssemblyStatus.READY
            and self.actual_rendered_writer_tokens > self.configured_writer_token_budget
        ):
            raise ValueError("READY evidence-first package cannot exceed its writer budget")
        if (
            self.final_status is ContextAssemblyStatus.READY
            and self.actual_rendered_ledger_tokens > self.configured_ledger_token_budget
        ):
            raise ValueError("READY evidence-first package cannot exceed its ledger budget")
        if self.item_count != self.evidence_item_count + self.gap_item_count:
            raise ValueError("evidence-first item counts are inconsistent")
        return self


class UnresolvedLexicalAnchor(DomainModel):
    mention: str = Field(min_length=1)
    source_draft_id: str = Field(min_length=1)
    source_fields: tuple[str, ...] = ()
    grounding_method: str = "no_label_match"


class EvidenceFirstLineage(DomainModel):
    need_ids: tuple[StableId, ...] = ()
    assembler_version: str = Field(min_length=1)
    grounder_version: str = ""
    validator_version: str = ""
    generator_version: str = ""
    query_compiler_version: str = ""
    route_plan_version: str = ""
    resolver_version: str = ""
    semantic_judge_version: str = ""
    planner_artifact_ref: ArtifactRef | None = None
    planner_artifact_hash: ArtifactId | None = None
    planner_fallback_used: bool = False
    unresolved_lexical_anchors: tuple[UnresolvedLexicalAnchor, ...] = ()
    advisory_artifact_refs: tuple[ArtifactRef, ...] = ()
    gateway_context_artifact: ArtifactRef | None = None
    frozen_evidence_selections_artifact: ArtifactRef | None = None
    budget_expansion_receipt: ArtifactRef | None = None


class WriterContextPackageV2(DomainModel):
    """Evidence-first Writer Context product (``writer_context.v2``).

    The default Stage 2M read-side product (ADR-0008): public
    Need/facet/scope-organized evidence items plus typed gaps, with a bound
    EvidenceLedger reference.  No claim groups, variants, receipts or semantic
    verdicts are required for READY.
    """

    contract_version: Literal["writer_context.v2"] = "writer_context.v2"
    task_contract: BenchmarkTaskContract
    basis_commit_id: CommitId
    basis_snapshot_id: StableId
    arm: Literal["A", "B", "C"]
    items: tuple[WriterContextEvidenceItem, ...]
    gaps: tuple[EvidenceFirstGap, ...] = ()
    budget_report: WriterContextBudgetReportV2
    evidence_ledger_ref: ArtifactRef
    lineage: EvidenceFirstLineage
    rendered_context: str = ""
    assembly_status: str = "READY"
    semantic_status: Literal["COMPLETE", "INCOMPLETE", "UNASSESSED"] = "UNASSESSED"
    usable_with_gaps: bool = True
    structural_mandatory_facet_closure: Literal["COMPLETE", "INCOMPLETE"] = "INCOMPLETE"
    unclosed_mandatory_need_facets: tuple[StableId, ...] = ()
    semantic_receipts: tuple[NeedFacetSemanticReceipt, ...] = ()
    semantic_batch_receipts: tuple[NeedEvidenceJudgmentBatchReceipt, ...] = ()

    @model_validator(mode="after")
    def validate_package(self) -> WriterContextPackageV2:
        gap_ids = {gap.gap_id for item in self.items if item.gap is not None for gap in (item.gap,)}
        listed_gap_ids = {gap.gap_id for gap in self.gaps}
        if gap_ids != listed_gap_ids:
            raise ValueError("package typed gaps must match item gap bindings")
        if len(self.unclosed_mandatory_need_facets) != len(
            set(self.unclosed_mandatory_need_facets)
        ):
            raise ValueError("unclosed mandatory Need facets must be unique")
        receipt_keys = tuple(
            (receipt.need_id, receipt.need_facet_id) for receipt in self.semantic_receipts
        )
        if len(receipt_keys) != len(set(receipt_keys)):
            raise ValueError("package semantic Need/facet receipts must be unique")
        batch_ids = tuple(batch.batch_id for batch in self.semantic_batch_receipts)
        if len(batch_ids) != len(set(batch_ids)):
            raise ValueError("package semantic batch receipts must be unique")
        if not set(
            batch_id for receipt in self.semantic_receipts for batch_id in receipt.batch_receipt_ids
        ).issubset(batch_ids):
            raise ValueError("package semantic receipts must reference retained batch receipts")
        return self


class EvidenceFirstPackageManifest(DomainModel):
    """Reproducible package/ledger freeze manifest for one checkpoint.

    The manifest carries the source, contract, configuration, content hashes,
    artifact refs, budgets, call counts and immutable root identity so a
    Writer, human or external strong model can verify the package without
    scanning the object store.
    """

    manifest_id: StableId
    experiment_id: str = Field(min_length=1)
    case_id: StableId
    checkpoint_chapter: int = Field(ge=0)
    basis_commit_id: CommitId
    basis_snapshot_id: StableId
    contract_version: Literal["writer_context.v2"] = "writer_context.v2"
    ledger_contract_version: str = "evidence_ledger.v2"
    assembler_version: str = Field(min_length=1)
    run_config_hash: ArtifactId
    package_artifact_ref: ArtifactRef
    evidence_ledger_ref: ArtifactRef
    package_hash: ArtifactId
    evidence_ledger_hash: ArtifactId
    generated_at: str = Field(min_length=1)
    writer_token_budget: int = Field(ge=1)
    evidence_ledger_token_budget: int = Field(ge=1)
    call_counts: dict[str, int] = Field(default_factory=dict)
    immutable_root_hashes: dict[str, str] = Field(default_factory=dict)
    need_count: int = Field(ge=0)
    item_count: int = Field(ge=0)
    gap_count: int = Field(ge=0)
    gap_codes: tuple[str, ...] = ()
    ledger_entry_count: int = Field(ge=0)
    ledger_tokens: int = Field(ge=0)
    future_leakage_count: int = Field(default=0, ge=0)
    leakage_failure_count: int = Field(default=0, ge=0)
    dereference_failure_count: int = Field(default=0, ge=0)
    scope_failure_count: int = Field(default=0, ge=0)
    cutoff_failure_count: int = Field(default=0, ge=0)
    budget_status: str = Field(min_length=1)
    root_hashes_unchanged: bool = True
    embedding_call_count: int = Field(default=0, ge=0)
    rerank_call_count: int = Field(default=0, ge=0)
    markdown_hash: ArtifactId | None = None
    assembly_status: str = Field(min_length=1)
    # Required and fail-closed: an omitted closure must not silently select
    # the success state (2026-08-14 review follow-up P1).
    mandatory_facet_closure: Literal["COMPLETE", "INCOMPLETE"]
    structural_mandatory_facet_closure: Literal["COMPLETE", "INCOMPLETE"] = "INCOMPLETE"
    semantic_status: Literal["COMPLETE", "INCOMPLETE", "UNASSESSED"] = "UNASSESSED"
    usable_with_gaps: bool = True
    unclosed_mandatory_need_facets: tuple[StableId, ...] = ()
    derived_tool_call_budget: int = Field(default=0, ge=0)
    derived_tool_call_formula: str = ""
    candidate_limit_saturated: tuple[dict[str, object], ...] = ()
    semantic_judge_planned_batch_count: int = Field(default=0, ge=0)
    semantic_judge_batch_count: int = Field(default=0, ge=0)
    semantic_judge_completed_batch_count: int = Field(default=0, ge=0)
    semantic_judge_failed_batch_count: int = Field(default=0, ge=0)
    projection_attestation_id: StableId | None = None
    graph_edge_count: int = Field(default=0, ge=0)
    graph_readiness_by_need: dict[str, str] = Field(default_factory=dict)
    verified_graph_path_receipt_ids: tuple[StableId, ...] = ()

    @model_validator(mode="after")
    def validate_manifest(self) -> EvidenceFirstPackageManifest:
        if self.package_artifact_ref.artifact_id != self.package_hash:
            raise ValueError("package manifest ref must match its retained artifact hash")
        if self.evidence_ledger_ref.artifact_id != self.evidence_ledger_hash:
            raise ValueError("ledger manifest ref must match its retained artifact hash")
        if self.leakage_failure_count != self.future_leakage_count:
            raise ValueError("leakage failure count must equal the future leakage count")
        if self.gap_codes != tuple(dict.fromkeys(self.gap_codes)):
            raise ValueError("gap codes must be unique and ordered")
        if len(self.unclosed_mandatory_need_facets) != len(
            set(self.unclosed_mandatory_need_facets)
        ):
            raise ValueError("manifest unclosed mandatory Need facets must be unique")
        if self.semantic_status == "COMPLETE" and self.unclosed_mandatory_need_facets:
            raise ValueError("COMPLETE semantic manifest cannot carry unclosed Need facets")
        judge_calls = self.call_counts.get("need_evidence_judge_calls", 0)
        if self.semantic_judge_batch_count != (
            self.semantic_judge_completed_batch_count + self.semantic_judge_failed_batch_count
        ):
            raise ValueError("semantic judge batch status counts must add up to total batches")
        if self.semantic_judge_planned_batch_count != self.semantic_judge_batch_count:
            raise ValueError("every planned semantic judge batch must be accounted")
        if judge_calls != (
            self.semantic_judge_completed_batch_count + self.semantic_judge_failed_batch_count
        ):
            raise ValueError("semantic judge call count must match persisted batch receipts")
        if self.assembly_status == "READY":
            failures = (
                self.dereference_failure_count,
                self.scope_failure_count,
                self.cutoff_failure_count,
                self.leakage_failure_count,
            )
            if any(item != 0 for item in failures):
                raise ValueError("READY manifest cannot carry mechanical failure counts")
            if not self.root_hashes_unchanged:
                raise ValueError("READY manifest requires unchanged immutable roots")
            # Model-driven Evidence-First (Plan v13 §6): this manifest *permits*
            # `need_planner_model_calls` (it is deliberately absent from the
            # claim-path zero list) so a completed model-driven case may record
            # its Planner model call. Enforcing at least one Planner call is the
            # `EvidenceFirstCheckpointRunner` owner's job when
            # `require_model_decisions=True`; this shared manifest must keep
            # admitting zero Planner calls for the deterministic mode. Claim
            # Support, whole-verifier, semantic evaluator, and Gold calls remain
            # forbidden during package construction and stay on the zero list.
            claim_path_calls = tuple(
                self.call_counts.get(key, 0)
                for key in (
                    "claim_support_calls",
                    "whole_verifier_calls",
                    "semantic_evaluator_calls",
                )
            )
            if any(item != 0 for item in claim_path_calls):
                raise ValueError("READY manifest requires zero claim-path model calls")
        graph_statuses = set(self.graph_readiness_by_need.values())
        allowed_graph_statuses = {
            "ready",
            "zero_edge",
            "missing_seed",
            "filtered_or_no_path",
            "not_required",
        }
        if not graph_statuses.issubset(allowed_graph_statuses):
            raise ValueError("package manifest has an unknown graph readiness status")
        if "ready" in graph_statuses and (
            self.graph_edge_count == 0 or not self.verified_graph_path_receipt_ids
        ):
            raise ValueError("graph READY requires visible edges and verified path receipts")
        if self.graph_edge_count == 0 and self.verified_graph_path_receipt_ids:
            raise ValueError("zero-edge projection cannot expose graph path receipts")
        return self


class MemoryContextBudgetExhaustedError(RuntimeError):
    """Writer Context remained resource-saturated after the last legal budget tier."""

    def __init__(self, message: str, *, receipt: ArtifactRef) -> None:
        super().__init__(message)
        self.receipt = receipt


class MemoryContextBudgetTier(StrEnum):
    BASE = "base"
    EXPAND_1 = "expand_1"
    EXPAND_2 = "expand_2"


class MemoryContextBudgetTierRecord(DomainModel):
    """One frozen Memory/Context budget attempt for a Need set and snapshot."""

    tier: MemoryContextBudgetTier
    request_id: StableId
    context_token_budget: int = Field(ge=1)
    evidence_ledger_token_budget: int = Field(ge=1)
    backend_call_budget: int = Field(ge=1)
    retrieval_call_count: int = Field(ge=0)
    expansion_reason: str | None = None
    stop_reason: str = Field(min_length=1)
    mandatory_need_facets_total: int = Field(ge=0)
    mandatory_need_facets_closed: int = Field(ge=0)
    frozen_context_artifact: ArtifactRef
    frozen_evidence_selections_artifact: ArtifactRef
    reexecuted_retrieval: bool


class MemoryContextBudgetExpansionReceipt(DomainModel):
    """Auditable expansion history for one Writer Context resolution."""

    contract_version: Literal["memory_context_budget_expansion.v1"] = (
        "memory_context_budget_expansion.v1"
    )
    request_id: StableId
    base_commit: CommitId
    snapshot_id: StableId
    need_ids: tuple[StableId, ...]
    tiers: tuple[MemoryContextBudgetTierRecord, ...] = Field(min_length=1, max_length=3)
    final_tier: MemoryContextBudgetTier
    terminal_reason: str = Field(min_length=1)
    budget_review: bool = False

    @model_validator(mode="after")
    def validate_tier_identity(self) -> MemoryContextBudgetExpansionReceipt:
        identities = tuple(item.request_id for item in self.tiers)
        if len(identities) != len(set(identities)):
            raise ValueError("budget expansion tiers must use unique request identities")
        if self.final_tier != self.tiers[-1].tier:
            raise ValueError("budget expansion final tier must match the last recorded attempt")
        return self


__all__ = [
    "BenchmarkInformationProfile",
    "BenchmarkTaskContract",
    "ClaimReductionLevel",
    "ClaimSupportGroup",
    "ClaimSupportReceipt",
    "ClaimVariant",
    "ContextAssemblyStatus",
    "ContextGap",
    "ContextLineage",
    "CutoffAttestation",
    "EvidenceFirstGap",
    "EvidenceFirstLineage",
    "EvidenceFirstPackageManifest",
    "EvidenceGapKind",
    "EvidenceLedger",
    "EvidenceLedgerEntry",
    "EvidenceLedgerEntryV2",
    "EvidenceLedgerV2",
    "EvidenceResolutionStatus",
    "EvidenceSlice",
    "EvidenceSliceKind",
    "EvidenceSliceSourceRole",
    "FreezeReceipt",
    "MemoryContextBudgetExhaustedError",
    "MemoryContextBudgetExpansionReceipt",
    "MemoryContextBudgetTier",
    "MemoryContextBudgetTierRecord",
    "NeedEvidenceJudgmentBatchReceipt",
    "NeedEvidenceJudgmentBatchStatus",
    "NeedEvidenceSemanticStatus",
    "NeedFacetSemanticReceipt",
    "PublicCheckpointPayload",
    "SemanticSupportStatus",
    "UnresolvedLexicalAnchor",
    "WriterContextBudgetReport",
    "WriterContextBudgetReportV2",
    "WriterContextEvidenceItem",
    "WriterContextItem",
    "WriterContextPackage",
    "WriterContextPackageV2",
    "WriterContextSection",
    "WriterContextValidity",
]
