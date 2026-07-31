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
from novel_agent.domain.text import EvidenceRef


class BenchmarkInformationProfile(StrEnum):
    VISIBLE_AT_CUTOFF = "visible_at_cutoff"
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
    "EvidenceLedger",
    "EvidenceLedgerEntry",
    "EvidenceResolutionStatus",
    "FreezeReceipt",
    "PublicCheckpointPayload",
    "SemanticSupportStatus",
    "WriterContextBudgetReport",
    "WriterContextItem",
    "WriterContextPackage",
    "WriterContextSection",
    "WriterContextValidity",
]
