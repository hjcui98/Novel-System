"""Stage 2W memory-write workflow contracts.

The write workflow deliberately lives in its own domain module.  The existing
Stage 2 replay contracts remain valid for compatibility, while this module
contains the framework-neutral contract used by benchmark, runtime, and future
control-plane adapters.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from novel_agent.domain.artifacts import ArtifactRef, RootKind, RootManifest
from novel_agent.domain.base import DomainModel
from novel_agent.domain.benchmark import PlanRootDocument, TextRootDocument
from novel_agent.domain.changes import (
    CandidateChangeBundle,
    ObservedChangeSet,
    WorldRecordKind,
)
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import FreshnessDecision, WorldRootDocument
from novel_agent.domain.runtime import EffectStatus, ResumabilityStatus
from novel_agent.domain.stage2 import (
    AccessScope,
    AgentExecutionReceipt,
    ContractRef,
    GuardianDecision,
    PatchRiskAssessment,
    WriteGateDecision,
)
from novel_agent.domain.text import EvidenceRef


class NarrativePosition(DomainModel):
    """A monotonic position used by information-boundary checks."""

    chapter_index: int = Field(ge=0)
    scene_index: int | None = Field(default=None, ge=0)
    block_index: int | None = Field(default=None, ge=0)


class MemoryWriteTriggerKind(StrEnum):
    CHAPTER_REVEAL = "chapter_reveal"
    BOOTSTRAP = "bootstrap"
    PLAN_CHANGE = "plan_change"
    MAINTENANCE = "maintenance"
    HUMAN_CORRECTION = "human_correction"


class ChapterRevealTrigger(DomainModel):
    kind: Literal[MemoryWriteTriggerKind.CHAPTER_REVEAL] = MemoryWriteTriggerKind.CHAPTER_REVEAL
    chapter_id: StableId
    chapter_index: int = Field(ge=0)
    reveal_position: NarrativePosition


class BootstrapTrigger(DomainModel):
    kind: Literal[MemoryWriteTriggerKind.BOOTSTRAP] = MemoryWriteTriggerKind.BOOTSTRAP
    bootstrap_bundle_id: StableId


class PlanChangeTrigger(DomainModel):
    kind: Literal[MemoryWriteTriggerKind.PLAN_CHANGE] = MemoryWriteTriggerKind.PLAN_CHANGE
    plan_change_id: StableId


class MaintenanceTrigger(DomainModel):
    kind: Literal[MemoryWriteTriggerKind.MAINTENANCE] = MemoryWriteTriggerKind.MAINTENANCE
    maintenance_task_id: StableId


class HumanCorrectionTrigger(DomainModel):
    kind: Literal[MemoryWriteTriggerKind.HUMAN_CORRECTION] = MemoryWriteTriggerKind.HUMAN_CORRECTION
    correction_request_id: StableId


MemoryWriteTrigger = Annotated[
    ChapterRevealTrigger
    | BootstrapTrigger
    | PlanChangeTrigger
    | MaintenanceTrigger
    | HumanCorrectionTrigger,
    Field(discriminator="kind"),
]


class MemoryWriteCommitProfile(StrEnum):
    CHAPTER_REVEAL_ATOMIC = "chapter_reveal_atomic"
    CHANGED_ROOTS_ONLY = "changed_roots_only"
    REQUIRE_CANONICAL_COMMIT = "require_canonical_commit"


class RootUpdateKind(StrEnum):
    CREATE = "create"
    REPLACE = "replace"
    NOOP = "noop"


class RootUpdateIntent(DomainModel):
    intent_id: StableId
    root_kind: RootKind
    update_kind: RootUpdateKind
    expected_base_root: ArtifactRef
    update_artifact: ArtifactRef
    producer_receipt: ArtifactRef
    builder_policy_ref: ContractRef

    @model_validator(mode="after")
    def validate_root_kinds(self) -> RootUpdateIntent:
        expected_kind = getattr(self.expected_base_root, "root_kind", None)
        if expected_kind is not None and expected_kind != self.root_kind:
            raise ValueError("RootUpdateIntent expected base root kind does not match root_kind")
        if (
            self.update_kind is RootUpdateKind.NOOP
            and self.update_artifact != self.expected_base_root
        ):
            raise ValueError("NOOP RootUpdateIntent must retain the expected base artifact")
        return self


class SourceProvenance(StrEnum):
    AUTHOR_INPUT = "author_input"
    REVEALED_TEXT = "revealed_text"
    CANONICAL_ROOT = "canonical_root"
    TRUSTED_DERIVED = "trusted_derived"
    HUMAN_CORRECTION = "human_correction"


class InformationBoundary(DomainModel):
    boundary_id: StableId
    base_commit: CommitId
    reveal_position: NarrativePosition | None = None
    maximum_visible_position: NarrativePosition | None = None
    evaluator_sources_forbidden: bool
    policy_ref: ContractRef

    @model_validator(mode="after")
    def validate_positions(self) -> InformationBoundary:
        if (
            self.reveal_position is not None
            and self.maximum_visible_position is not None
            and _position_key(self.maximum_visible_position) < _position_key(self.reveal_position)
        ):
            raise ValueError("maximum visible position cannot precede reveal position")
        return self


class SourceVisibilityReceipt(DomainModel):
    receipt_id: StableId
    source_artifact: ArtifactRef
    boundary_id: StableId
    visible_through: NarrativePosition | None
    access_scope: AccessScope
    provenance: SourceProvenance
    issuer: StableId
    receipt_hash: ArtifactId


class BoundaryPropagationReceipt(DomainModel):
    receipt_id: StableId
    boundary_id: StableId
    base_commit: CommitId
    input_source_artifact_refs: tuple[ArtifactRef, ...] = ()
    source_visibility_receipt_refs: tuple[ArtifactRef, ...] = ()
    input_derivation_receipt_refs: tuple[ArtifactRef, ...] = ()
    output_artifact_hash: ArtifactId
    builder_policy_hash: ArtifactId
    effective_visible_through: NarrativePosition | None
    effective_access_scope: AccessScope
    receipt_hash: ArtifactId

    @model_validator(mode="after")
    def validate_inputs(self) -> BoundaryPropagationReceipt:
        if not self.input_source_artifact_refs and not self.input_derivation_receipt_refs:
            raise ValueError("propagation receipt must have at least one direct input")
        refs = (
            *self.source_visibility_receipt_refs,
            *self.input_derivation_receipt_refs,
        )
        if len({ref.artifact_id for ref in refs}) != len(refs):
            raise ValueError("propagation receipt input receipt refs must be unique")
        return self


class CuratorWorldProposalInput(DomainModel):
    mode: Literal["curator_proposal"] = "curator_proposal"
    curator_agent_spec: ContractRef


class TrustedWorldCandidateInput(DomainModel):
    mode: Literal["trusted_candidate"] = "trusted_candidate"
    candidate_artifact: ArtifactRef
    producer_receipt: ArtifactRef


class NoWorldMutationInput(DomainModel):
    mode: Literal["none"] = "none"


WorldMutationInput = Annotated[
    CuratorWorldProposalInput | TrustedWorldCandidateInput | NoWorldMutationInput,
    Field(discriminator="mode"),
]


class MemoryTransportBudget(DomainModel):
    """The model transport budget is intentionally separate from repair budget."""

    max_attempts: int = Field(default=3, ge=1)
    timeout_seconds: float = Field(default=60.0, gt=0)
    backoff_profile: str = Field(default="exponential-v1", min_length=1)


class MemoryWriteBudget(DomainModel):
    max_curator_proposal_attempts: int = Field(default=3, ge=1)
    max_curator_proposal_rejections: int = Field(default=3, ge=0)
    max_candidate_revisions: int = Field(default=3, ge=1)
    max_curator_repairs: int = Field(default=2, ge=0)
    max_normalization_passes: int = Field(default=3, ge=0)
    max_guardian_reviews: int = Field(default=2, ge=0)
    max_context_refreshes: int = Field(default=1, ge=0)
    max_total_model_calls: int = Field(default=4, ge=0)
    token_budget: int = Field(default=24_000, ge=0)
    wall_clock_budget_ms: int = Field(default=180_000, ge=1)
    same_content_hash_limit: int = Field(default=2, ge=1)
    same_finding_signature_limit: int = Field(default=2, ge=1)
    on_budget_exhausted: Literal["quarantine", "stop"] = "quarantine"
    on_guardian_reject: Literal["quarantine", "stop"] = "quarantine"
    model_transport: MemoryTransportBudget = Field(default_factory=MemoryTransportBudget)


class MemoryWriteBudgetUsage(DomainModel):
    curator_proposal_attempts: int = Field(default=0, ge=0)
    curator_proposal_rejections: int = Field(default=0, ge=0)
    structured_generation_attempts: int = Field(default=0, ge=0)
    candidate_revisions: int = Field(default=0, ge=0)
    curator_repairs: int = Field(default=0, ge=0)
    normalization_passes: int = Field(default=0, ge=0)
    guardian_reviews: int = Field(default=0, ge=0)
    context_refreshes: int = Field(default=0, ge=0)
    total_model_calls: int = Field(default=0, ge=0)
    transport_attempts: int = Field(default=0, ge=0)
    tokens_used: int = Field(default=0, ge=0)
    elapsed_ms: int = Field(default=0, ge=0)


class MemoryWriteBudgetRemaining(DomainModel):
    curator_proposal_attempts: int = Field(default=0, ge=0)
    curator_proposal_rejections: int = Field(default=0, ge=0)
    structured_generation_attempts: int = Field(default=0, ge=0)
    candidate_revisions: int = Field(ge=0)
    curator_repairs: int = Field(ge=0)
    normalization_passes: int = Field(ge=0)
    guardian_reviews: int = Field(ge=0)
    context_refreshes: int = Field(ge=0)
    total_model_calls: int = Field(ge=0)
    token_budget: int = Field(ge=0)
    wall_clock_budget_ms: int = Field(ge=0)


class CanonicalWriteBasis(DomainModel):
    """Verified read-only basis supplied to write-side ports."""

    project_id: ProjectId
    commit_id: CommitId
    root_manifest: RootManifest | None = None
    canonical_root_refs: RootManifest | None = None
    canonical_world: WorldRootDocument | None = None
    canonical_text: TextRootDocument | None = None
    canonical_plan: PlanRootDocument | None = None

    @model_validator(mode="after")
    def validate_basis(self) -> CanonicalWriteBasis:
        manifest = self.root_manifest or self.canonical_root_refs
        if manifest is None:
            raise ValueError("canonical write basis requires a RootManifest")
        if manifest.project_id != self.project_id:
            raise ValueError("canonical basis manifest belongs to another project")
        if self.root_manifest is None:
            object.__setattr__(self, "root_manifest", manifest)
        if self.canonical_root_refs is None:
            object.__setattr__(self, "canonical_root_refs", manifest)
        return self


class MemoryWriteWorkflowRequest(DomainModel):
    request_id: StableId
    run_id: RunId
    task_id: TaskId
    project_id: ProjectId
    trigger: MemoryWriteTrigger
    commit_profile: MemoryWriteCommitProfile
    base_commit: CommitId
    source_artifacts: tuple[ArtifactRef, ...] = ()
    root_update_intents: tuple[RootUpdateIntent, ...] = ()
    world_mutation: WorldMutationInput
    canonical_root_refs: RootManifest
    information_boundary: InformationBoundary
    source_visibility_receipts: tuple[SourceVisibilityReceipt, ...] = ()
    access_scope: AccessScope
    source_provenance: tuple[SourceProvenance, ...] = ()
    configuration_fingerprint: ArtifactId
    prompt_contract_refs: tuple[ContractRef, ...] = ()
    skill_contract_refs: tuple[ContractRef, ...] = ()
    tool_policy_ref: ContractRef
    repair_policy_ref: ContractRef
    budget: MemoryWriteBudget = Field(default_factory=MemoryWriteBudget)
    idempotency_key: StableId
    resume_checkpoint: ArtifactRef | None = None

    @model_validator(mode="after")
    def validate_request_contract(self) -> MemoryWriteWorkflowRequest:
        if self.information_boundary.base_commit != self.base_commit:
            raise ValueError("information boundary must use the request base commit")
        if len(self.source_artifacts) != len(self.source_visibility_receipts):
            raise ValueError("every source artifact requires exactly one visibility receipt")
        if self.source_provenance and len(self.source_provenance) != len(self.source_artifacts):
            raise ValueError("source provenance must align one-to-one with source artifacts")
        if not self.source_provenance and self.source_artifacts:
            raise ValueError("source provenance must be supplied for every source artifact")
        intent_ids = tuple(intent.intent_id for intent in self.root_update_intents)
        if len(intent_ids) != len(set(intent_ids)):
            raise ValueError("RootUpdateIntent ids must be unique")
        root_kinds = tuple(intent.root_kind for intent in self.root_update_intents)
        if len(root_kinds) != len(set(root_kinds)):
            raise ValueError("a request may contain at most one intent per Root kind")

        trigger_kind = self.trigger.kind
        if self.commit_profile is MemoryWriteCommitProfile.CHAPTER_REVEAL_ATOMIC:
            if trigger_kind is not MemoryWriteTriggerKind.CHAPTER_REVEAL:
                raise ValueError("CHAPTER_REVEAL_ATOMIC requires a ChapterReveal trigger")
            if not any(intent.root_kind is RootKind.TEXT for intent in self.root_update_intents):
                raise ValueError("ChapterReveal requires a TextRoot update intent")
        elif self.commit_profile is MemoryWriteCommitProfile.CHANGED_ROOTS_ONLY:
            allowed = {
                MemoryWriteTriggerKind.PLAN_CHANGE,
                MemoryWriteTriggerKind.MAINTENANCE,
                MemoryWriteTriggerKind.HUMAN_CORRECTION,
            }
            if trigger_kind not in allowed:
                raise ValueError("CHANGED_ROOTS_ONLY trigger is not registered")
        else:
            allowed = {
                MemoryWriteTriggerKind.BOOTSTRAP,
                MemoryWriteTriggerKind.MAINTENANCE,
                MemoryWriteTriggerKind.HUMAN_CORRECTION,
            }
            if trigger_kind not in allowed:
                raise ValueError("REQUIRE_CANONICAL_COMMIT trigger is not registered")

        if trigger_kind is MemoryWriteTriggerKind.PLAN_CHANGE and not any(
            intent.root_kind is RootKind.PLAN for intent in self.root_update_intents
        ):
            raise ValueError("PlanChange requires a PlanRoot update intent")
        if (
            isinstance(self.world_mutation, TrustedWorldCandidateInput)
            and self.world_mutation.candidate_artifact.artifact_id
            == self.world_mutation.producer_receipt.artifact_id
        ):
            raise ValueError("trusted candidate and producer receipt must be distinct artifacts")
        return self


class MemoryWriteWorkflowPhase(StrEnum):
    PRECOMMIT = "precommit"
    CANON_COMMITTED = "canon_committed"
    PROJECTION_PENDING = "projection_pending"
    COMPLETE = "complete"


class MemoryWriteWorkflowStatus(StrEnum):
    COMMITTED = "committed"
    NOOP = "noop"
    QUARANTINED = "quarantined"
    SUSPENDED = "suspended"
    HUMAN_REQUIRED = "human_required"
    REPLAN_REQUIRED = "replan_required"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FATAL = "fatal"


class ContinuationDecision(StrEnum):
    SAFE_TO_CONTINUE = "safe_to_continue"
    BLOCK_NEXT_CHAPTER = "block_next_chapter"
    REVIEW_BEFORE_CHECKPOINT = "review_before_checkpoint"


class MemoryWriteWorkflowResult(DomainModel):
    request_id: StableId
    status: MemoryWriteWorkflowStatus
    workflow_phase: MemoryWriteWorkflowPhase
    canonical_commit_accepted: bool
    base_commit: CommitId
    resulting_commit: CommitId | None = None
    world_mutation_noop: bool = False
    accepted_candidate_id: StableId | None = None
    terminal_candidate_id: StableId | None = None
    validation_receipt: ArtifactRef | None = None
    guardian_receipt: ArtifactRef | None = None
    commit_receipt: ArtifactRef | None = None
    projection_receipt_ref: ArtifactRef | None = None
    freshness_receipt_ref: ArtifactRef | None = None
    projection_snapshot_id: StableId | None = None
    freshness: FreshnessDecision | None = None
    checkpoint_ref: ArtifactRef | None = None
    degraded: bool = False
    quarantine_refs: tuple[ArtifactRef, ...] = ()
    committed_operation_ids: tuple[StableId, ...] = ()
    quarantined_operation_ids: tuple[StableId, ...] = ()
    blocked_capabilities: tuple[str, ...] = ()
    continuation_decision: ContinuationDecision = ContinuationDecision.BLOCK_NEXT_CHAPTER
    budget_usage: MemoryWriteBudgetUsage = Field(default_factory=MemoryWriteBudgetUsage)
    terminal_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_result_contract(self) -> MemoryWriteWorkflowResult:
        accepted = self.canonical_commit_accepted
        if accepted:
            if self.resulting_commit is None or self.commit_receipt is None:
                raise ValueError("accepted Canon requires resulting commit and commit receipt")
            if self.workflow_phase is MemoryWriteWorkflowPhase.PRECOMMIT:
                raise ValueError("accepted Canon cannot remain in PRECOMMIT")
        else:
            if self.resulting_commit is not None or self.commit_receipt is not None:
                raise ValueError("unaccepted workflow cannot carry a commit result")
            if (
                self.workflow_phase is not MemoryWriteWorkflowPhase.PRECOMMIT
                and self.status is not MemoryWriteWorkflowStatus.NOOP
            ):
                raise ValueError("precommit blocking result must use PRECOMMIT phase")

        if self.status is MemoryWriteWorkflowStatus.COMMITTED:
            if self.workflow_phase is not MemoryWriteWorkflowPhase.COMPLETE or not accepted:
                raise ValueError("COMMITTED result must be a completed accepted Canon")
            required = (
                self.accepted_candidate_id,
                self.validation_receipt,
                self.projection_receipt_ref,
                self.freshness_receipt_ref,
                self.projection_snapshot_id,
                self.freshness,
            )
            if any(value is None for value in required):
                raise ValueError("COMMITTED result requires validation, projection, and freshness")
        if self.status is MemoryWriteWorkflowStatus.NOOP and (
            accepted or self.workflow_phase is not MemoryWriteWorkflowPhase.COMPLETE
        ):
            raise ValueError("NOOP must be an uncommitted completed result")
        if (
            self.status
            in {
                MemoryWriteWorkflowStatus.HUMAN_REQUIRED,
                MemoryWriteWorkflowStatus.REPLAN_REQUIRED,
                MemoryWriteWorkflowStatus.SUSPENDED,
            }
            and self.checkpoint_ref is None
        ):
            raise ValueError("resumable workflow status requires a checkpoint")
        if self.status is MemoryWriteWorkflowStatus.QUARANTINED and not self.quarantine_refs:
            raise ValueError("QUARANTINED result requires a quarantine artifact")
        if self.degraded:
            if self.status is not MemoryWriteWorkflowStatus.COMMITTED:
                raise ValueError("degraded result must represent a committed candidate")
            if not self.committed_operation_ids or not self.quarantined_operation_ids:
                raise ValueError(
                    "degraded commit requires committed and quarantined operation sets"
                )
            if set(self.committed_operation_ids) & set(self.quarantined_operation_ids):
                raise ValueError("committed and quarantined operation sets must be disjoint")
            if not self.quarantine_refs:
                raise ValueError("degraded commit requires quarantine refs")
        if (
            accepted
            and self.status
            in {
                MemoryWriteWorkflowStatus.SUSPENDED,
                MemoryWriteWorkflowStatus.FATAL,
            }
            and self.continuation_decision is ContinuationDecision.SAFE_TO_CONTINUE
        ):
            raise ValueError("post-commit non-success result cannot default to safe continuation")
        if (
            self.status
            in {
                MemoryWriteWorkflowStatus.HUMAN_REQUIRED,
                MemoryWriteWorkflowStatus.REPLAN_REQUIRED,
                MemoryWriteWorkflowStatus.BUDGET_EXHAUSTED,
                MemoryWriteWorkflowStatus.FATAL,
            }
            and self.continuation_decision is ContinuationDecision.SAFE_TO_CONTINUE
        ):
            raise ValueError("blocking terminal status cannot continue safely by default")
        return self


class CandidateProducerKind(StrEnum):
    CURATOR_PROPOSE = "curator_propose"
    TRUSTED_CANDIDATE = "trusted_candidate"
    EMPTY_DELTA = "empty_delta"
    DETERMINISTIC_NORMALIZER = "deterministic_normalizer"
    CURATOR_REPAIR = "curator_repair"
    HUMAN_PATCH = "human_patch"
    OPERATION_QUARANTINE = "operation_quarantine"


class CuratorProposalAttemptStatus(StrEnum):
    REQUESTED = "requested"
    RUNNING = "running"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"
    ABANDONED = "abandoned"


class ProposalRejectionStage(StrEnum):
    STRUCTURED_SCHEMA = "structured_schema"
    TRUSTED_NORMALIZATION = "trusted_normalization"
    SEMANTIC_CONTRACT = "semantic_contract"
    INFORMATION_BOUNDARY = "information_boundary"


class ProposalRejectionKind(StrEnum):
    SCHEMA_REJECTED = "schema_rejected"
    CHAPTER_MISMATCH = "chapter_mismatch"
    RECORD_KIND_MISMATCH = "record_kind_mismatch"
    DANGLING_ENTITY_REFERENCE = "dangling_entity_reference"
    DUPLICATE_TARGET = "duplicate_target"
    NORMALIZED_TARGET_COLLISION = "normalized_target_collision"
    INCOMPLETE_DELTA = "incomplete_delta"
    INVALID_EVIDENCE = "invalid_evidence"
    SCOPE_VIOLATION = "scope_violation"
    POISON_LOOP = "poison_loop"


class ProposalConflict(DomainModel):
    record_kind: WorldRecordKind
    target_id: StableId
    operation_indexes: tuple[int, ...] = Field(min_length=2)
    semantic_hashes: tuple[ArtifactId, ...] = Field(min_length=1)
    evidence_hashes: tuple[ArtifactId, ...] = ()

    @model_validator(mode="after")
    def validate_indexes(self) -> ProposalConflict:
        if self.operation_indexes != tuple(sorted(set(self.operation_indexes))):
            raise ValueError("proposal conflict operation indexes must be unique and ascending")
        return self


class CuratorProposalRejection(DomainModel):
    rejection_id: StableId
    attempt_id: StableId
    workflow_request_id: StableId
    base_commit: CommitId
    stage: ProposalRejectionStage
    kind: ProposalRejectionKind
    reason_code: str = Field(min_length=1)
    retryable: bool
    rejection_signature: ArtifactId
    output_hash: ArtifactId | None = None
    conflicts: tuple[ProposalConflict, ...] = ()
    validation_error_paths: tuple[str, ...] = ()
    safe_feedback: tuple[str, ...] = ()
    operation_indexes: tuple[int, ...] = ()
    json_pointers: tuple[str, ...] = ()
    violation_rule: str | None = None
    raw_draft_ref: ArtifactRef | None = None
    normalized_output_ref: ArtifactRef | None = None
    created_at: datetime

    @model_validator(mode="after")
    def validate_boundary_failure(self) -> CuratorProposalRejection:
        if self.stage is ProposalRejectionStage.INFORMATION_BOUNDARY and self.retryable:
            raise ValueError("information-boundary proposal rejection cannot be retried")
        if any(not item or len(item) > 240 for item in self.safe_feedback):
            raise ValueError("proposal feedback must be non-empty bounded text")
        return self


class CuratorProposalAttemptReceipt(DomainModel):
    attempt_id: StableId
    workflow_request_id: StableId
    run_id: RunId
    task_id: TaskId
    attempt_no: int = Field(ge=1)
    base_commit: CommitId
    boundary_id: StableId
    configuration_fingerprint: ArtifactId
    status: CuratorProposalAttemptStatus
    model_request_ids: tuple[StableId, ...] = ()
    model_call_receipt_refs: tuple[ArtifactRef, ...] = ()
    prompt_fingerprint: ArtifactId
    feedback_artifact_ref: ArtifactRef | None = None
    raw_response_refs: tuple[ArtifactRef, ...] = ()
    parsed_draft_ref: ArtifactRef | None = None
    normalized_output_ref: ArtifactRef | None = None
    output_hashes: tuple[ArtifactId, ...] = ()
    rejection_ref: ArtifactRef | None = None
    agent_execution_receipt_ref: ArtifactRef | None = None
    producer_receipt_ref: ArtifactRef | None = None
    transform_receipt_refs: tuple[ArtifactRef, ...] = ()
    provider_call_count: int = Field(default=0, ge=0)
    transport_attempt_count: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    started_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_attempt_shape(self) -> CuratorProposalAttemptReceipt:
        if len(self.model_request_ids) != len(set(self.model_request_ids)):
            raise ValueError("proposal attempt model request ids must be unique")
        if self.status in {
            CuratorProposalAttemptStatus.ACCEPTED,
            CuratorProposalAttemptStatus.REJECTED,
        } and len(self.model_request_ids) != len(self.model_call_receipt_refs):
            raise ValueError("proposal model request ids require one call receipt each")
        if self.provider_call_count < len(self.model_call_receipt_refs):
            raise ValueError("provider call count cannot be smaller than call receipts")
        accepted_refs = (
            self.normalized_output_ref,
            self.agent_execution_receipt_ref,
            self.producer_receipt_ref,
        )
        if self.status is CuratorProposalAttemptStatus.ACCEPTED:
            if any(item is None for item in accepted_refs) or self.completed_at is None:
                raise ValueError("accepted proposal attempt requires output and producer receipts")
        elif any(item is not None for item in accepted_refs):
            raise ValueError("non-accepted proposal attempt cannot carry accepted output")
        if self.status is CuratorProposalAttemptStatus.REJECTED and (
            self.rejection_ref is None or self.completed_at is None
        ):
            raise ValueError("rejected proposal attempt requires rejection and completion")
        if (
            self.status
            in {
                CuratorProposalAttemptStatus.REQUESTED,
                CuratorProposalAttemptStatus.RUNNING,
                CuratorProposalAttemptStatus.UNCERTAIN,
            }
            and self.completed_at is not None
        ):
            raise ValueError("inflight proposal attempt cannot be completed")
        return self


class CuratorProposalAccepted(DomainModel):
    status: Literal["accepted"] = "accepted"
    observed_changes: ObservedChangeSet
    attempt_receipt: CuratorProposalAttemptReceipt

    @model_validator(mode="after")
    def validate_accepted_receipt(self) -> CuratorProposalAccepted:
        if self.attempt_receipt.status is not CuratorProposalAttemptStatus.ACCEPTED:
            raise ValueError("accepted proposal outcome requires an accepted attempt receipt")
        return self


class CuratorProposalRejected(DomainModel):
    status: Literal["rejected"] = "rejected"
    rejection: CuratorProposalRejection
    attempt_receipt: CuratorProposalAttemptReceipt

    @model_validator(mode="after")
    def validate_rejected_receipt(self) -> CuratorProposalRejected:
        if self.attempt_receipt.status is not CuratorProposalAttemptStatus.REJECTED:
            raise ValueError("rejected proposal outcome requires a rejected attempt receipt")
        if self.rejection.attempt_id != self.attempt_receipt.attempt_id:
            raise ValueError("proposal rejection belongs to another attempt")
        return self


CuratorProposalAttemptOutcome = Annotated[
    CuratorProposalAccepted | CuratorProposalRejected,
    Field(discriminator="status"),
]


class ProposalEvidenceMergeReceipt(DomainModel):
    transform_id: StableId
    base_commit: CommitId
    record_kind: WorldRecordKind
    target_id: StableId
    semantic_hash: ArtifactId
    source_operation_hashes: tuple[ArtifactId, ...] = Field(min_length=2)
    merged_evidence_hashes: tuple[ArtifactId, ...] = Field(min_length=1)


class ProposalRepairScope(DomainModel):
    mutable_operation_indexes: tuple[int, ...] = ()
    immutable_operation_semantic_hashes: tuple[ArtifactId, ...] = ()
    allow_complete_replacement: bool = False
    json_pointers: tuple[str, ...] = ()
    violation_rule: str | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> ProposalRepairScope:
        if self.mutable_operation_indexes != tuple(sorted(set(self.mutable_operation_indexes))):
            raise ValueError("mutable proposal operation indexes must be unique and ascending")
        if self.mutable_operation_indexes and self.allow_complete_replacement:
            raise ValueError("scoped proposal repair cannot also allow complete replacement")
        return self


class CuratorProposalRepairDirective(DomainModel):
    directive_id: StableId
    workflow_request_id: StableId
    prior_attempt_id: StableId
    action: Literal[
        "retry_with_feedback",
        "deterministic_evidence_merge",
        "human_review",
        "quarantine",
        "budget_stop",
        "fatal",
    ]
    reason_codes: tuple[str, ...] = Field(min_length=1)
    rejection_signature: ArtifactId
    previous_output_hash: ArtifactId | None = None
    scope: ProposalRepairScope
    feedback_artifact_ref: ArtifactRef | None = None


class ProposalHumanDecisionKind(StrEnum):
    RETRY = "retry"
    TRUSTED_REPLACEMENT = "trusted_replacement"
    REJECT = "reject"


class ProposalHumanReviewRequest(DomainModel):
    approval_request_id: StableId
    workflow_request_id: StableId
    base_commit: CommitId
    proposal_attempt_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    latest_rejected_draft_ref: ArtifactRef
    latest_rejection_ref: ArtifactRef
    safe_feedback: tuple[str, ...] = ()
    created_at: datetime


class ProposalHumanReviewDecision(DomainModel):
    decision_id: StableId
    approval_request_id: StableId
    workflow_request_id: StableId
    base_commit: CommitId
    kind: ProposalHumanDecisionKind
    trusted_replacement_draft_ref: ArtifactRef | None = None
    reason: str | None = None
    decided_at: datetime

    @model_validator(mode="after")
    def validate_decision(self) -> ProposalHumanReviewDecision:
        if (
            self.kind is ProposalHumanDecisionKind.TRUSTED_REPLACEMENT
            and self.trusted_replacement_draft_ref is None
        ):
            raise ValueError("trusted proposal replacement requires a Draft artifact")
        if self.kind is ProposalHumanDecisionKind.REJECT and not self.reason:
            raise ValueError("proposal rejection requires a reason")
        return self


class MemoryWriteCandidatePayload(DomainModel):
    observed_changes: ObservedChangeSet
    root_update_intents: tuple[RootUpdateIntent, ...]
    commit_profile: MemoryWriteCommitProfile


class RepairScope(DomainModel):
    operation_ids: tuple[StableId, ...] = ()
    field_paths: tuple[str, ...] = ()
    allow_identity_rebind: bool = False
    allow_operation_type_change: bool = False
    allow_successor_creation: bool = False


class CandidateRevision(DomainModel):
    candidate_id: StableId
    parent_candidate_id: StableId | None = None
    revision_no: int = Field(ge=1)
    base_commit: CommitId
    basis_hash: ArtifactId
    candidate_artifact: ArtifactRef
    source_artifacts: tuple[ArtifactRef, ...] = ()
    producer_kind: CandidateProducerKind
    producer_receipt: ArtifactRef | None = None
    origin_proposal_attempt_id: StableId | None = None
    origin_proposal_attempt_receipt: ArtifactRef | None = None
    proposal_attempt_chain_refs: tuple[ArtifactRef, ...] = ()
    repair_scope: RepairScope | None = None
    applied_directive_ids: tuple[StableId, ...] = ()
    supersedes_candidate_id: StableId | None = None
    content_hash: ArtifactId
    created_at: datetime

    @model_validator(mode="after")
    def validate_revision_shape(self) -> CandidateRevision:
        if self.revision_no == 1 and self.parent_candidate_id is not None:
            raise ValueError("first Candidate revision cannot have a parent")
        if self.revision_no > 1 and self.parent_candidate_id is None:
            raise ValueError("child Candidate revision requires a parent")
        if self.supersedes_candidate_id == self.candidate_id:
            raise ValueError("Candidate cannot supersede itself")
        if (self.origin_proposal_attempt_id is None) != (
            self.origin_proposal_attempt_receipt is None
        ):
            raise ValueError("proposal-origin Candidate requires attempt id and receipt together")
        if self.revision_no > 1 and self.origin_proposal_attempt_id is not None:
            raise ValueError("only Candidate v1 may bind a proposal attempt")
        return self


class CandidateMaterialization(DomainModel):
    candidate_id: StableId
    candidate_content_hash: ArtifactId
    bundle_artifact: ArtifactRef
    proposed_roots_hash: ArtifactId
    materialization_receipt: ArtifactRef
    materializer_policy_ref: ContractRef
    bundle: CandidateChangeBundle | None = None

    @model_validator(mode="after")
    def validate_bundle_binding(self) -> CandidateMaterialization:
        if self.bundle is not None and (
            self.bundle.bundle_id != StableId(f"bundle.{self.candidate_id.root}")
            or self.bundle.base_commit != self.bundle.observed_changes.base_commit
        ):
            raise ValueError("materialized bundle has an invalid candidate binding")
        return self


class NormalizationStatus(StrEnum):
    UNCHANGED = "unchanged"
    TRANSFORMED = "transformed"
    AMBIGUOUS = "ambiguous"


class NormalizationTransformReceipt(DomainModel):
    receipt_id: StableId
    rule_id: StableId
    before_hash: ArtifactId
    after_hash: ArtifactId
    finding_ids: tuple[StableId, ...] = ()
    affected_operation_ids: tuple[StableId, ...] = ()
    reason: str = Field(min_length=1)


class NormalizationResult(DomainModel):
    status: NormalizationStatus
    candidate: CandidateRevision
    transforms: tuple[NormalizationTransformReceipt, ...] = ()
    reason_codes: tuple[str, ...] = ()


class ValidationDisposition(StrEnum):
    PASS = "pass"
    REPAIRABLE = "repairable"
    PARTIAL_REPAIRABLE = "partial_repairable"
    REVIEW_REQUIRED = "review_required"
    NON_REPAIRABLE = "non_repairable"


class ValidationFindingCategory(StrEnum):
    EVIDENCE = "evidence"
    IDENTITY = "identity"
    TRANSITION = "transition"
    OPERATION = "operation"
    TRUTH = "truth"
    BASIS = "basis"
    SCHEMA = "schema"
    INFORMATION_BOUNDARY = "information_boundary"
    UNKNOWN = "unknown"


class ValidationSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class FindingRetryability(StrEnum):
    REPAIRABLE = "repairable"
    CONDITIONAL = "conditional"
    REVIEW = "review"
    NON_REPAIRABLE = "non_repairable"


class RepairStrategy(StrEnum):
    SUCCESSOR_STATE = "successor_state"
    CORRECT_TARGET = "correct_target"
    CREATE_TO_REPLACE = "create_to_replace"
    REPLACE_TO_CREATE = "replace_to_create"
    REBIND_EVIDENCE = "rebind_evidence"
    REFRESH_CONTEXT = "refresh_context"
    CURATOR_REPAIR = "curator_repair"
    GUARDIAN_REVIEW = "guardian_review"
    QUARANTINE = "quarantine"
    HUMAN = "human"


class BlockingScope(StrEnum):
    CANDIDATE = "candidate"
    OPERATION = "operation"
    FIELD = "field"


class ValidationFindingV2(DomainModel):
    finding_id: StableId
    code: str = Field(min_length=1)
    category: ValidationFindingCategory
    severity: ValidationSeverity
    message: str = Field(min_length=1)
    operation_ids: tuple[StableId, ...] = ()
    field_paths: tuple[str, ...] = ()
    canonical_record_refs: tuple[StableId, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    retryability: FindingRetryability
    suggested_strategies: tuple[RepairStrategy, ...] = ()
    blocking_scope: BlockingScope
    allowed_repair_scope: RepairScope
    requires_context_refresh: bool = False
    requires_guardian: bool = False
    requires_human: bool = False


class ValidationDecision(DomainModel):
    decision_id: StableId
    candidate_id: StableId
    candidate_content_hash: ArtifactId
    materialization_receipt: ArtifactRef
    proposed_roots_hash: ArtifactId
    base_commit: CommitId
    disposition: ValidationDisposition
    findings: tuple[ValidationFindingV2, ...] = ()
    deterministic_profile: str = Field(min_length=1)
    model_profile: str | None = None
    validated_at: datetime

    @model_validator(mode="after")
    def validate_findings(self) -> ValidationDecision:
        blocking = {
            ValidationSeverity.ERROR,
            ValidationSeverity.CRITICAL,
        }
        if self.disposition is ValidationDisposition.PASS and any(
            finding.severity in blocking for finding in self.findings
        ):
            raise ValueError("PASS validation cannot contain blocking findings")
        if self.disposition is ValidationDisposition.NON_REPAIRABLE and not self.findings:
            raise ValueError("non-repairable validation requires a finding")
        return self


class RepairAction(StrEnum):
    DETERMINISTIC_REPAIR = "deterministic_repair"
    CURATOR_REPAIR = "curator_repair"
    GUARDIAN_REVIEW = "guardian_review"
    QUARANTINE_OPERATION = "quarantine_operation"
    RETRY_AFTER_SOURCE_CONTEXT_REFRESH = "retry_after_source_context_refresh"
    REPLAN = "replan"
    HUMAN = "human"
    STOP_BUDGET_EXHAUSTED = "stop_budget_exhausted"
    STOP_FATAL = "stop_fatal"


class RepairActionReceipt(DomainModel):
    receipt_id: StableId
    action: RepairAction
    directive_id: StableId
    candidate_id: StableId
    artifact_ref: ArtifactRef | None = None
    reason_codes: tuple[str, ...] = ()


class RepairDirective(DomainModel):
    directive_id: StableId
    action: RepairAction
    finding_ids: tuple[StableId, ...] = ()
    operation_ids: tuple[StableId, ...] = ()
    allowed_scope: RepairScope = Field(default_factory=RepairScope)
    reason_codes: tuple[str, ...] = ()
    checkpoint_required: bool = False

    @model_validator(mode="after")
    def validate_scope(self) -> RepairDirective:
        if set(self.operation_ids) - set(self.allowed_scope.operation_ids):
            raise ValueError("repair directive operation exceeds allowed repair scope")
        if self.action is RepairAction.STOP_BUDGET_EXHAUSTED and not self.reason_codes:
            raise ValueError("budget stop requires a reason code")
        return self


class RepairContext(DomainModel):
    request_id: StableId
    candidate: CandidateRevision
    validation: ValidationDecision | None = None
    risk: PatchRiskAssessment | None = None
    guardian: GuardianDecision | None = None
    gate: WriteGateDecision | None = None
    budget_remaining: MemoryWriteBudgetRemaining
    prior_actions: tuple[RepairActionReceipt, ...] = ()
    repeated_content_hashes: tuple[ArtifactId, ...] = ()
    current_canonical_commit: CommitId


class MemoryWriteState(StrEnum):
    LOAD_BASIS = "load_basis"
    PREPARE_CANDIDATE = "prepare_candidate"
    CURATE = "curate"
    CURATE_ATTEMPT_PREPARE = "curate_attempt_prepare"
    CURATE_ATTEMPT_EXECUTE = "curate_attempt_execute"
    PROPOSAL_VALIDATE = "proposal_validate"
    PROPOSAL_REPAIR_POLICY = "proposal_repair_policy"
    PROPOSAL_RETRY = "proposal_retry"
    PROPOSAL_HUMAN_SUSPEND = "proposal_human_suspend"
    PROPOSAL_HUMAN_RESUME = "proposal_human_resume"
    NORMALIZE = "normalize"
    MATERIALIZE = "materialize"
    VALIDATE = "validate"
    REPAIR_POLICY = "repair_policy"
    REFRESH_SOURCE_CONTEXT = "refresh_source_context"
    CURATOR_REPAIR = "curator_repair"
    RISK_CLASSIFY = "risk_classify"
    GUARDIAN = "guardian"
    HUMAN_SUSPEND = "human_suspend"
    HUMAN_RESUME = "human_resume"
    PRECOMMIT = "precommit"
    COMMIT = "commit"
    PROJECT = "project"
    FRESHNESS_GATE = "freshness_gate"
    QUARANTINE = "quarantine"
    BUDGET_STOP = "budget_stop"
    COMPLETE = "complete"
    STOP = "stop"


class ProjectionReadinessStatus(StrEnum):
    READY = "ready"
    PENDING = "pending"
    FAILED = "failed"


class ProjectionReadinessResult(DomainModel):
    effect_id: StableId
    status: ProjectionReadinessStatus
    projection_receipt_ref: ArtifactRef | None = None
    freshness_receipt_ref: ArtifactRef | None = None
    projection_snapshot_id: StableId | None = None
    freshness: FreshnessDecision | None = None
    resumable: bool = True
    reason: str | None = None

    @model_validator(mode="after")
    def validate_ready(self) -> ProjectionReadinessResult:
        if self.status is ProjectionReadinessStatus.READY:
            if (
                self.projection_receipt_ref is None
                or self.freshness_receipt_ref is None
                or self.projection_snapshot_id is None
                or self.freshness is None
            ):
                raise ValueError("ready projection requires projection and freshness receipts")
        elif self.reason is None:
            raise ValueError("pending or failed projection requires a reason")
        return self


class MemoryWriteCheckpoint(DomainModel):
    checkpoint_id: StableId
    request_identity_hash: ArtifactId
    request_artifact_ref: ArtifactRef
    run_id: RunId
    task_id: TaskId
    project_id: ProjectId
    base_commit: CommitId
    source_artifacts: tuple[ArtifactRef, ...]
    root_update_intents: tuple[RootUpdateIntent, ...]
    world_mutation: WorldMutationInput
    information_boundary: InformationBoundary
    configuration_fingerprint: ArtifactId
    workflow_phase: MemoryWriteWorkflowPhase
    state: MemoryWriteState
    resume_state: MemoryWriteState
    current_candidate_id: StableId | None = None
    proposal_attempt_no: int = Field(default=0, ge=0)
    inflight_proposal_attempt_id: StableId | None = None
    inflight_proposal_attempt_ref: ArtifactRef | None = None
    proposal_attempt_status: CuratorProposalAttemptStatus | None = None
    proposal_attempt_refs: tuple[ArtifactRef, ...] = ()
    proposal_rejection_refs: tuple[ArtifactRef, ...] = ()
    proposal_feedback_ref: ArtifactRef | None = None
    proposal_directive_ref: ArtifactRef | None = None
    last_proposal_output_hash: ArtifactId | None = None
    last_proposal_rejection_signature: ArtifactId | None = None
    same_proposal_output_count: int = Field(default=0, ge=0)
    same_proposal_rejection_count: int = Field(default=0, ge=0)
    proposal_budget_reservation_ref: ArtifactRef | None = None
    lineage_head_artifact: ArtifactRef | None = None
    materialization_artifact: ArtifactRef | None = None
    validation_artifact: ArtifactRef | None = None
    risk_artifact: ArtifactRef | None = None
    guardian_artifact: ArtifactRef | None = None
    gate_artifact: ArtifactRef | None = None
    approval_request_artifact: ArtifactRef | None = None
    proposal_human_request_artifact: ArtifactRef | None = None
    commit_effect_id: StableId | None = None
    commit_request_ref: ArtifactRef | None = None
    commit_attempt_status: EffectStatus | None = None
    accepted_commit_id: CommitId | None = None
    commit_receipt_ref: ArtifactRef | None = None
    projection_effect_id: StableId | None = None
    projection_status: EffectStatus | None = None
    projection_receipt_ref: ArtifactRef | None = None
    projection_snapshot_id: StableId | None = None
    freshness_receipt_ref: ArtifactRef | None = None
    terminal_result_ref: ArtifactRef | None = None
    completed_effect_ids: tuple[StableId, ...] = ()
    budget_usage: MemoryWriteBudgetUsage = Field(default_factory=MemoryWriteBudgetUsage)
    last_event_sequence: int = Field(default=0, ge=0)
    resumability_status: ResumabilityStatus

    @model_validator(mode="after")
    def validate_checkpoint_phase(self) -> MemoryWriteCheckpoint:
        post_commit = {
            MemoryWriteWorkflowPhase.CANON_COMMITTED,
            MemoryWriteWorkflowPhase.PROJECTION_PENDING,
        }
        if self.workflow_phase is MemoryWriteWorkflowPhase.PRECOMMIT:
            if self.accepted_commit_id is not None or self.commit_receipt_ref is not None:
                raise ValueError("PRECOMMIT checkpoint cannot carry an accepted commit")
            if self.resume_state is MemoryWriteState.COMMIT and (
                self.commit_effect_id is None
                or self.commit_request_ref is None
                or self.commit_attempt_status
                not in {EffectStatus.REQUESTED, EffectStatus.UNCERTAIN}
            ):
                raise ValueError("COMMIT resume requires exact request/effect and attempt status")
        if self.workflow_phase in post_commit and (
            self.accepted_commit_id is None or self.commit_receipt_ref is None
        ):
            raise ValueError("post-commit checkpoint requires accepted commit and receipt")
        if self.workflow_phase is MemoryWriteWorkflowPhase.PROJECTION_PENDING and (
            self.projection_effect_id is None or self.projection_status is None
        ):
            raise ValueError("PROJECTION_PENDING checkpoint requires a projection effect")
        if self.workflow_phase is MemoryWriteWorkflowPhase.COMPLETE:
            committed = self.accepted_commit_id is not None
            if committed and (
                self.commit_receipt_ref is None
                or self.projection_receipt_ref is None
                or self.freshness_receipt_ref is None
            ):
                raise ValueError("completed committed checkpoint requires final receipts")
        allowed_resume_states = {
            MemoryWriteWorkflowPhase.PRECOMMIT: {
                MemoryWriteState.LOAD_BASIS,
                MemoryWriteState.PREPARE_CANDIDATE,
                MemoryWriteState.CURATE,
                MemoryWriteState.CURATE_ATTEMPT_PREPARE,
                MemoryWriteState.CURATE_ATTEMPT_EXECUTE,
                MemoryWriteState.PROPOSAL_VALIDATE,
                MemoryWriteState.PROPOSAL_REPAIR_POLICY,
                MemoryWriteState.PROPOSAL_RETRY,
                MemoryWriteState.PROPOSAL_HUMAN_SUSPEND,
                MemoryWriteState.PROPOSAL_HUMAN_RESUME,
                MemoryWriteState.NORMALIZE,
                MemoryWriteState.MATERIALIZE,
                MemoryWriteState.VALIDATE,
                MemoryWriteState.REPAIR_POLICY,
                MemoryWriteState.REFRESH_SOURCE_CONTEXT,
                MemoryWriteState.CURATOR_REPAIR,
                MemoryWriteState.RISK_CLASSIFY,
                MemoryWriteState.GUARDIAN,
                MemoryWriteState.HUMAN_SUSPEND,
                MemoryWriteState.HUMAN_RESUME,
                MemoryWriteState.PRECOMMIT,
                MemoryWriteState.COMMIT,
                MemoryWriteState.QUARANTINE,
                MemoryWriteState.BUDGET_STOP,
                MemoryWriteState.STOP,
            },
            MemoryWriteWorkflowPhase.CANON_COMMITTED: {MemoryWriteState.PROJECT},
            MemoryWriteWorkflowPhase.PROJECTION_PENDING: {
                MemoryWriteState.PROJECT,
                MemoryWriteState.FRESHNESS_GATE,
            },
            MemoryWriteWorkflowPhase.COMPLETE: {MemoryWriteState.COMPLETE},
        }
        if self.resume_state not in allowed_resume_states[self.workflow_phase]:
            raise ValueError("checkpoint resume state is not allowed for its workflow phase")
        if self.resume_state is MemoryWriteState.CURATE and self.current_candidate_id is not None:
            raise ValueError(
                "curation resume cannot pretend a current candidate is already accepted"
            )
        pre_candidate_states = {
            MemoryWriteState.CURATE,
            MemoryWriteState.CURATE_ATTEMPT_PREPARE,
            MemoryWriteState.CURATE_ATTEMPT_EXECUTE,
            MemoryWriteState.PROPOSAL_VALIDATE,
            MemoryWriteState.PROPOSAL_REPAIR_POLICY,
            MemoryWriteState.PROPOSAL_RETRY,
            MemoryWriteState.PROPOSAL_HUMAN_SUSPEND,
            MemoryWriteState.PROPOSAL_HUMAN_RESUME,
        }
        if self.resume_state in pre_candidate_states and self.current_candidate_id is not None:
            raise ValueError("Pre-Candidate checkpoint cannot carry a Candidate")
        if self.resume_state is MemoryWriteState.CURATE_ATTEMPT_EXECUTE and (
            self.inflight_proposal_attempt_id is None
            or self.inflight_proposal_attempt_ref is None
            or self.proposal_attempt_status
            not in {
                CuratorProposalAttemptStatus.REQUESTED,
                CuratorProposalAttemptStatus.RUNNING,
                CuratorProposalAttemptStatus.UNCERTAIN,
            }
        ):
            raise ValueError("proposal execution resume requires an inflight attempt")
        if self.resume_state is MemoryWriteState.PROPOSAL_VALIDATE and (
            self.inflight_proposal_attempt_ref is None
            or self.proposal_attempt_status
            not in {
                CuratorProposalAttemptStatus.ACCEPTED,
                CuratorProposalAttemptStatus.REJECTED,
            }
        ):
            raise ValueError("proposal validation resume requires a terminal attempt")
        if (
            self.resume_state
            in {
                MemoryWriteState.PROPOSAL_REPAIR_POLICY,
                MemoryWriteState.PROPOSAL_RETRY,
            }
            and not self.proposal_rejection_refs
        ):
            raise ValueError("proposal repair resume requires a rejection")
        if self.resume_state is MemoryWriteState.PROPOSAL_HUMAN_RESUME and (
            self.proposal_human_request_artifact is None or self.proposal_directive_ref is None
        ):
            raise ValueError("proposal human resume requires its review request and directive")
        if (
            self.resume_state is MemoryWriteState.NORMALIZE
            and self.current_candidate_id is not None
            and not self.proposal_attempt_refs
            and self.world_mutation.mode == "curator_proposal"
        ):
            raise ValueError("Curator Candidate resume requires an accepted proposal attempt")
        if (
            self.resume_state is MemoryWriteState.CURATOR_REPAIR
            and self.current_candidate_id is None
        ):
            raise ValueError("curator repair resume requires its parent candidate")
        return self


class HumanDecisionKind(StrEnum):
    APPROVE_EXACT_CANDIDATE = "approve_exact_candidate"
    REQUEST_REVISION = "request_revision"
    HUMAN_PATCH = "human_patch"
    REJECT = "reject"


class HumanApprovalRequest(DomainModel):
    approval_request_id: StableId
    request_id: StableId
    candidate_id: StableId
    candidate_content_hash: ArtifactId
    base_commit: CommitId
    validation: ArtifactRef
    risk: ArtifactRef | None = None
    guardian: ArtifactRef | None = None
    created_at: datetime


class HumanApprovalRequestReceipt(DomainModel):
    approval_request: HumanApprovalRequest
    artifact_ref: ArtifactRef


class HumanApprovalDecision(DomainModel):
    decision_id: StableId
    approval_request_id: StableId
    request_id: StableId
    candidate_id: StableId
    candidate_content_hash: ArtifactId
    base_commit: CommitId
    kind: HumanDecisionKind
    directive: RepairDirective | None = None
    patch_candidate_artifact: ArtifactRef | None = None
    reason: str | None = None
    decided_at: datetime

    @model_validator(mode="after")
    def validate_decision_shape(self) -> HumanApprovalDecision:
        if self.kind is HumanDecisionKind.REQUEST_REVISION and self.directive is None:
            raise ValueError("human revision decision requires a repair directive")
        if self.kind is HumanDecisionKind.HUMAN_PATCH and self.patch_candidate_artifact is None:
            raise ValueError("human patch decision requires a child candidate artifact")
        if self.kind is HumanDecisionKind.REJECT and not self.reason:
            raise ValueError("human rejection requires a reason")
        if self.kind is HumanDecisionKind.APPROVE_EXACT_CANDIDATE and self.directive is not None:
            raise ValueError("exact approval cannot carry a repair directive")
        return self


class QuarantinePackage(DomainModel):
    package_id: StableId
    request_id: StableId
    base_commit: CommitId
    candidate_ids: tuple[StableId, ...]
    source_artifacts: tuple[ArtifactRef, ...]
    validation_ref: ArtifactRef | None = None
    guardian_ref: ArtifactRef | None = None
    gate_ref: ArtifactRef | None = None
    repair_directive_refs: tuple[ArtifactRef, ...] = ()
    proposal_attempt_refs: tuple[ArtifactRef, ...] = ()
    proposal_rejection_refs: tuple[ArtifactRef, ...] = ()
    proposal_feedback_ref: ArtifactRef | None = None
    quarantined_operation_ids: tuple[StableId, ...] = ()
    current_project_commit: CommitId | None = None
    configuration_fingerprint: ArtifactId
    terminal_reason: str = Field(min_length=1)
    recommended_action: str = Field(min_length=1)


def _position_key(position: NarrativePosition) -> tuple[int, int, int]:
    return (
        position.chapter_index,
        -1 if position.scene_index is None else position.scene_index,
        -1 if position.block_index is None else position.block_index,
    )


__all__ = [
    "AccessScope",
    "AgentExecutionReceipt",
    "ArtifactRef",
    "BlockingScope",
    "BootstrapTrigger",
    "BoundaryPropagationReceipt",
    "CandidateMaterialization",
    "CandidateProducerKind",
    "CandidateRevision",
    "CanonicalWriteBasis",
    "ChapterRevealTrigger",
    "ContinuationDecision",
    "ContractRef",
    "CuratorProposalAccepted",
    "CuratorProposalAttemptOutcome",
    "CuratorProposalAttemptReceipt",
    "CuratorProposalAttemptStatus",
    "CuratorProposalRejected",
    "CuratorProposalRejection",
    "CuratorProposalRepairDirective",
    "CuratorWorldProposalInput",
    "EffectStatus",
    "FindingRetryability",
    "HumanApprovalDecision",
    "HumanApprovalRequest",
    "HumanApprovalRequestReceipt",
    "HumanCorrectionTrigger",
    "HumanDecisionKind",
    "InformationBoundary",
    "MaintenanceTrigger",
    "MemoryTransportBudget",
    "MemoryWriteBudget",
    "MemoryWriteBudgetRemaining",
    "MemoryWriteBudgetUsage",
    "MemoryWriteCandidatePayload",
    "MemoryWriteCheckpoint",
    "MemoryWriteCommitProfile",
    "MemoryWriteState",
    "MemoryWriteTrigger",
    "MemoryWriteTriggerKind",
    "MemoryWriteWorkflowPhase",
    "MemoryWriteWorkflowRequest",
    "MemoryWriteWorkflowResult",
    "MemoryWriteWorkflowStatus",
    "NarrativePosition",
    "NoWorldMutationInput",
    "NormalizationResult",
    "NormalizationStatus",
    "NormalizationTransformReceipt",
    "PlanChangeTrigger",
    "ProjectionReadinessResult",
    "ProjectionReadinessStatus",
    "ProposalConflict",
    "ProposalEvidenceMergeReceipt",
    "ProposalHumanDecisionKind",
    "ProposalHumanReviewDecision",
    "ProposalHumanReviewRequest",
    "ProposalRejectionKind",
    "ProposalRejectionStage",
    "ProposalRepairScope",
    "QuarantinePackage",
    "RepairAction",
    "RepairActionReceipt",
    "RepairContext",
    "RepairDirective",
    "RepairScope",
    "RepairStrategy",
    "RootUpdateIntent",
    "RootUpdateKind",
    "SourceProvenance",
    "SourceVisibilityReceipt",
    "TrustedWorldCandidateInput",
    "ValidationDecision",
    "ValidationDisposition",
    "ValidationFindingCategory",
    "ValidationFindingV2",
    "ValidationSeverity",
    "WorldMutationInput",
]
