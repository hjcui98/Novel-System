"""Framework-neutral ports for the Stage 2W memory-write workflow."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from novel_agent.domain.artifacts import ArtifactRef, RootManifest
from novel_agent.domain.base import DomainModel
from novel_agent.domain.changes import CandidateChangeBundle, ObservedChangeSet
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, StableId
from novel_agent.domain.memory_write import (
    BoundaryPropagationReceipt,
    CandidateMaterialization,
    CandidateRevision,
    CanonicalWriteBasis,
    CuratorProposalAttemptOutcome,
    CuratorProposalAttemptReceipt,
    CuratorProposalRejection,
    HumanApprovalDecision,
    HumanApprovalRequest,
    HumanApprovalRequestReceipt,
    InformationBoundary,
    MemoryWriteCheckpoint,
    MemoryWriteWorkflowRequest,
    MemoryWriteWorkflowResult,
    ProjectionReadinessResult,
    ProposalHumanReviewDecision,
    ProposalHumanReviewRequest,
    RepairContext,
    RepairDirective,
    SourceVisibilityReceipt,
    ValidationDecision,
)
from novel_agent.domain.runtime import RunEvent
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    GuardianDecision,
    PatchRiskAssessment,
    WriteGateDecision,
)


class CuratorRepairRejectedError(ValueError):
    """A repair model output was rejected without corrupting workflow state."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "CURATOR_REPAIR_SCOPE_REJECTED",
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class CuratorProposalTransportError(RuntimeError):
    """Provider transport ended without a trusted proposal outcome."""

    def __init__(
        self,
        message: str,
        *,
        model_request_ids: tuple[StableId, ...] = (),
        uncertain: bool = True,
    ) -> None:
        super().__init__(message)
        self.model_request_ids = model_request_ids
        self.uncertain = uncertain


class CuratorProposalRequest(DomainModel):
    request: MemoryWriteWorkflowRequest
    basis: CanonicalWriteBasis
    source_artifacts: tuple[ArtifactRef, ...]
    source_visibility_receipts: tuple[SourceVisibilityReceipt, ...]
    parent_candidate: CandidateRevision | None = None
    applied_directive_ids: tuple[StableId, ...] = ()


class CuratorProposalResult(DomainModel):
    observed_changes: ObservedChangeSet
    agent_receipt: AgentExecutionReceipt | None = None
    producer_receipt: ArtifactRef | None = None
    candidate_artifact: ArtifactRef | None = None
    token_usage: int = 0
    transport_attempts: int = 1


class CuratorProposalAttemptRequest(DomainModel):
    request: MemoryWriteWorkflowRequest
    basis: CanonicalWriteBasis
    attempt_id: StableId
    attempt_no: int = Field(ge=1)
    model_request_id: StableId
    source_artifacts: tuple[ArtifactRef, ...]
    source_visibility_receipts: tuple[SourceVisibilityReceipt, ...]
    budget_reservation_ref: ArtifactRef
    feedback_artifact_ref: ArtifactRef | None = None
    previous_rejection_ref: ArtifactRef | None = None


class CuratorRepairRequest(DomainModel):
    request: MemoryWriteWorkflowRequest
    basis: CanonicalWriteBasis
    parent_candidate: CandidateRevision
    validation: ValidationDecision | None = None
    guardian: GuardianDecision | None = None
    directive: RepairDirective
    source_artifacts: tuple[ArtifactRef, ...]
    source_visibility_receipts: tuple[SourceVisibilityReceipt, ...]


class CuratorRepairResult(DomainModel):
    observed_changes: ObservedChangeSet
    agent_receipt: AgentExecutionReceipt | None = None
    producer_receipt: ArtifactRef | None = None
    candidate_artifact: ArtifactRef | None = None
    applied_directive_ids: tuple[StableId, ...] = ()
    token_usage: int = 0
    transport_attempts: int = 1


class GuardianReviewRequest(DomainModel):
    request: MemoryWriteWorkflowRequest
    basis: CanonicalWriteBasis
    candidate: CandidateRevision
    validation: ValidationDecision
    risk: PatchRiskAssessment


class GuardianReviewResult(DomainModel):
    decision: GuardianDecision
    receipt: ArtifactRef | None = None
    token_usage: int = 0
    transport_attempts: int = 1


class DurableMemoryWriteCommitRequest(DomainModel):
    request_id: StableId
    project_id: ProjectId
    base_commit: CommitId
    idempotency_key: StableId
    commit_effect_id: StableId
    request_hash: ArtifactId
    candidate: CandidateRevision
    materialization: CandidateMaterialization
    bundle: CandidateChangeBundle
    validation: ValidationDecision
    gate: WriteGateDecision


class MemoryWriteCommitStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CONFLICTED = "conflicted"
    DRY_RUN_REFUSED = "dry_run_refused"


class MemoryWriteCommitResult(DomainModel):
    request_id: StableId
    status: str
    commit_id: CommitId | None = None
    manifest: RootManifest | None = None
    commit_receipt_ref: ArtifactRef | None = None
    reason: str | None = None
    committed_operation_ids: tuple[StableId, ...] = ()

    @staticmethod
    def accepted_status(status: str) -> bool:
        return status in {MemoryWriteCommitStatus.ACCEPTED, "accepted"}


class RootMaterializationResult(DomainModel):
    materialization: CandidateMaterialization
    bundle: CandidateChangeBundle
    world_mutation_noop: bool
    changed_root_kinds: tuple[str, ...] = ()


@runtime_checkable
class MemoryWriteWorkflowPort(Protocol):
    async def execute(self, request: MemoryWriteWorkflowRequest) -> MemoryWriteWorkflowResult: ...


class CuratorPort(Protocol):
    async def propose_attempt(
        self, request: CuratorProposalAttemptRequest
    ) -> CuratorProposalAttemptOutcome: ...

    async def propose(self, request: CuratorProposalRequest) -> CuratorProposalResult: ...

    async def repair(self, request: CuratorRepairRequest) -> CuratorRepairResult: ...


class MutationNormalizerPort(Protocol):
    def normalize(
        self,
        candidate: CandidateRevision,
        canonical: CanonicalWriteBasis,
        directive: RepairDirective | None = None,
    ) -> Any: ...


class ValidationPort(Protocol):
    async def validate(
        self,
        candidate: CandidateRevision,
        materialization: CandidateMaterialization,
        canonical: CanonicalWriteBasis,
    ) -> ValidationDecision: ...


class GuardianPort(Protocol):
    async def review(self, request: GuardianReviewRequest) -> GuardianReviewResult: ...


class RepairPolicyPort(Protocol):
    def decide(self, context: RepairContext) -> RepairDirective: ...


class HumanApprovalPort(Protocol):
    def request(self, request: HumanApprovalRequest) -> HumanApprovalRequestReceipt: ...

    def read_decision(self, request_id: StableId) -> HumanApprovalDecision | None: ...


class ProposalHumanReviewPort(Protocol):
    def request(self, request: ProposalHumanReviewRequest) -> ArtifactRef: ...

    def read_decision(self, request_id: StableId) -> ProposalHumanReviewDecision | None: ...


class MemoryWriteCommitPort(Protocol):
    def resolve_or_replay_exact(
        self, request: DurableMemoryWriteCommitRequest
    ) -> MemoryWriteCommitResult: ...


class CanonicalReadPort(Protocol):
    def load_verified(self, project_id: ProjectId, commit_id: CommitId) -> CanonicalWriteBasis: ...

    def current_commit(self, project_id: ProjectId) -> CommitId: ...


class RootUpdatePort(Protocol):
    def materialize_atomic_bundle(
        self,
        *,
        candidate: CandidateRevision,
        basis: CanonicalWriteBasis,
    ) -> RootMaterializationResult: ...


class InformationBoundaryPort(Protocol):
    def verify_request_and_derivation_graph(
        self,
        request: MemoryWriteWorkflowRequest,
        basis: CanonicalWriteBasis,
    ) -> None: ...

    def verify_derivation_chain(
        self,
        *,
        artifact: ArtifactRef,
        producer_receipt: ArtifactRef,
        boundary: InformationBoundary,
        configuration_fingerprint: ArtifactId,
    ) -> BoundaryPropagationReceipt: ...

    def register_derivation(
        self,
        receipt: BoundaryPropagationReceipt,
        receipt_artifact: ArtifactRef | None = None,
        output_artifact: ArtifactRef | None = None,
    ) -> ArtifactRef: ...

    def read_derivation_receipt(self, reference: ArtifactRef) -> BoundaryPropagationReceipt: ...


class ArtifactRepositoryPort(Protocol):
    def put(self, data: bytes, media_type: str, schema_version: Any) -> ArtifactRef: ...

    def read_verified(self, artifact: ArtifactRef) -> bytes: ...


class CandidateLineageRepositoryPort(Protocol):
    def persist(self, candidate: CandidateRevision) -> CandidateRevision: ...

    def get(self, candidate_id: StableId) -> CandidateRevision | None: ...

    def list_for_request(self, request_id: StableId) -> tuple[CandidateRevision, ...]: ...


class QuarantineRepositoryPort(Protocol):
    def persist(self, package: Any) -> ArtifactRef: ...


class WorkflowCheckpointPort(Protocol):
    def save(self, checkpoint: MemoryWriteCheckpoint) -> ArtifactRef: ...

    def load(self, checkpoint_ref: ArtifactRef) -> MemoryWriteCheckpoint: ...


class CuratorProposalAttemptRepositoryPort(Protocol):
    def create_requested(self, receipt: CuratorProposalAttemptReceipt) -> ArtifactRef: ...

    def mark_running(self, attempt_id: StableId, model_request_id: StableId) -> ArtifactRef: ...

    def settle_accepted(
        self,
        attempt_id: StableId,
        receipt: CuratorProposalAttemptReceipt,
    ) -> ArtifactRef: ...

    def settle_rejected(
        self,
        attempt_id: StableId,
        rejection: CuratorProposalRejection,
        receipt: CuratorProposalAttemptReceipt,
    ) -> ArtifactRef: ...

    def mark_uncertain(self, attempt_id: StableId, reason: str) -> ArtifactRef: ...

    def load(self, attempt_id: StableId) -> CuratorProposalAttemptReceipt: ...

    def list_for_workflow(
        self, request_id: StableId
    ) -> tuple[CuratorProposalAttemptReceipt, ...]: ...


class RunEventSink(Protocol):
    def append(self, event: RunEvent) -> RunEvent: ...


class BudgetReservation(DomainModel):
    reservation_id: StableId
    operation: str
    granted: bool
    remaining: dict[str, int] = Field(default_factory=dict)
    reason: str | None = None


class BudgetPolicyPort(Protocol):
    def reserve(self, operation: str, *, cost: int = 1) -> BudgetReservation: ...

    def settle(
        self, reservation: BudgetReservation, *, tokens: int = 0, attempts: int = 1
    ) -> None: ...


class ProjectionReadinessPort(Protocol):
    def request_or_read_by_effect_id(
        self, project_id: ProjectId, commit_id: CommitId, effect_id: StableId
    ) -> ProjectionReadinessResult: ...

    def await_or_check(
        self, project_id: ProjectId, commit_id: CommitId, effect_id: StableId
    ) -> ProjectionReadinessResult: ...


class ClockPort(Protocol):
    def now(self) -> datetime: ...


__all__ = [
    "ArtifactRepositoryPort",
    "BudgetPolicyPort",
    "BudgetReservation",
    "CandidateLineageRepositoryPort",
    "CanonicalReadPort",
    "ClockPort",
    "CuratorPort",
    "CuratorProposalRequest",
    "CuratorProposalResult",
    "CuratorRepairRequest",
    "CuratorRepairResult",
    "DurableMemoryWriteCommitRequest",
    "GuardianPort",
    "GuardianReviewRequest",
    "GuardianReviewResult",
    "HumanApprovalPort",
    "InformationBoundaryPort",
    "MemoryWriteCommitPort",
    "MemoryWriteCommitResult",
    "MemoryWriteCommitStatus",
    "MemoryWriteWorkflowPort",
    "MutationNormalizerPort",
    "ProjectionReadinessPort",
    "QuarantineRepositoryPort",
    "RepairPolicyPort",
    "RootMaterializationResult",
    "RootUpdatePort",
    "RunEventSink",
    "ValidationPort",
    "WorkflowCheckpointPort",
]
