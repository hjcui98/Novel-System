"""U8-C recovery-reasoning contracts.

The reasoner is deliberately an offline proposal producer.  It may select one
already validated action, but it cannot execute an action, mutate Canon or an
active prompt/Skill, or replace the deterministic ``FailurePolicy`` owner.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    StableId,
    TaskId,
)
from novel_agent.domain.model_calls import ModelCallRecord
from novel_agent.domain.runtime import FailureClass

RECOVERY_PROPOSAL_MEDIA_TYPE = "application/vnd.novel-agent.recovery-proposal+json"


class RecoveryActionKind(StrEnum):
    GRAPH_CURATOR = "graph_curator"
    ORDINARY_CURATOR = "ordinary_curator"
    REVIEW_REQUIRED = "review_required"


class RecoveryActionCandidate(DomainModel):
    """One existing safe action the reasoner is allowed to select."""

    action_id: StableId
    action_kind: RecoveryActionKind
    proposal_ref: ArtifactRef
    validation_ref: ArtifactRef
    basis_commit: CommitId
    safety_boundary_id: ArtifactId
    validator_accepted: Literal[True] = True
    canonical_mutation_observed: Literal[False] = False
    skill_mutation_observed: Literal[False] = False


class RecoveryReasonerBudget(DomainModel):
    max_context_bytes: int = Field(default=65536, ge=1024, le=1048576)
    max_output_tokens: int = Field(default=2048, ge=128, le=16384)
    timeout_seconds: float = Field(default=120.0, gt=0, le=900)
    enable_thinking: bool = False
    thinking_token_budget: int = Field(default=0, ge=0, le=8192)

    @model_validator(mode="after")
    def validate_thinking_budget(self) -> RecoveryReasonerBudget:
        if self.enable_thinking != (self.thinking_token_budget > 0):
            raise ValueError("thinking flag and thinking budget must agree")
        return self


class RecoveryReasonerAdmission(DomainModel):
    """Evidence gate required before the offline reasoner can be called."""

    evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=3, max_length=16)
    same_failure_boundary_has_multiple_valid_actions: Literal[True] = True
    receipt_state_cannot_disambiguate: Literal[True] = True
    held_out_beats_deterministic_baseline: Literal[True] = True
    canonical_or_skill_mutation_observed: Literal[False] = False
    gate_authority: Literal["codex_or_human"] = "codex_or_human"
    decision: Literal["U8_C_ADMITTED"] = "U8_C_ADMITTED"

    @model_validator(mode="after")
    def validate_distinct_evidence(self) -> RecoveryReasonerAdmission:
        evidence_ids = tuple(ref.artifact_id for ref in self.evidence_refs)
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("recovery admission evidence references must be unique")
        return self


class RecoveryReasonerRequest(DomainModel):
    """Immutable incident surface presented to the offline reasoner."""

    request_id: StableId
    model_request_id: StableId
    project_id: ProjectId
    run_id: RunId
    task_id: TaskId
    incident_ref: ArtifactRef
    failure_class: FailureClass
    basis_commit: CommitId
    safety_boundary_id: ArtifactId
    receipt_refs: tuple[ArtifactRef, ...] = Field(min_length=1, max_length=32)
    state_refs: tuple[ArtifactRef, ...] = Field(min_length=1, max_length=32)
    candidates: tuple[RecoveryActionCandidate, ...] = Field(min_length=2, max_length=8)
    allowed_action_kinds: tuple[RecoveryActionKind, ...] = Field(min_length=2, max_length=3)
    prompt_contract_hash: ArtifactId
    skill_contract_hash: ArtifactId
    admission: RecoveryReasonerAdmission
    budget: RecoveryReasonerBudget = Field(default_factory=RecoveryReasonerBudget)
    proposal_only: Literal[True] = True

    @model_validator(mode="after")
    def validate_safe_choice_surface(self) -> RecoveryReasonerRequest:
        forbidden = {
            FailureClass.PROVIDER_TRANSIENT,
            FailureClass.EFFECT_UNCERTAIN,
            FailureClass.PERMISSION_DENIED,
            FailureClass.COMMIT_CONFLICT,
            FailureClass.BASIS_CHANGED,
            FailureClass.WORKER_LEASE_EXPIRED,
        }
        if self.failure_class in forbidden:
            raise ValueError("failure class already has one deterministic safe owner")
        if len(set(self.allowed_action_kinds)) != len(self.allowed_action_kinds):
            raise ValueError("reasoner action allowlist must be unique")
        action_ids = tuple(candidate.action_id for candidate in self.candidates)
        if len(set(action_ids)) != len(action_ids):
            raise ValueError("recovery action identities must be unique")
        if len({candidate.proposal_ref.artifact_id for candidate in self.candidates}) < 2:
            raise ValueError("reasoner requires at least two distinct action payloads")
        for candidate in self.candidates:
            if candidate.basis_commit != self.basis_commit:
                raise ValueError("recovery candidate basis differs from request")
            if candidate.safety_boundary_id != self.safety_boundary_id:
                raise ValueError("recovery candidate safety boundary differs from request")
            if candidate.action_kind not in self.allowed_action_kinds:
                raise ValueError("recovery candidate is outside the action allowlist")
        return self


class RecoveryProposalDraft(DomainModel):
    """Structured model output; identity and authority are assigned by the host."""

    selected_action_id: StableId
    rejected_action_ids: tuple[StableId, ...] = Field(max_length=7)
    rationale: str = Field(min_length=1, max_length=1200)


class RecoveryProposal(DomainModel):
    """A durable, rejectable proposal with no execution authority."""

    proposal_id: StableId
    request_id: StableId
    project_id: ProjectId
    run_id: RunId
    task_id: TaskId
    incident_ref: ArtifactRef
    failure_class: FailureClass
    basis_commit: CommitId
    safety_boundary_id: ArtifactId
    selected_action_id: StableId
    selected_action_kind: RecoveryActionKind
    selected_action_ref: ArtifactRef
    selected_validation_ref: ArtifactRef
    considered_action_ids: tuple[StableId, ...] = Field(min_length=2, max_length=8)
    rejected_action_ids: tuple[StableId, ...] = Field(max_length=7)
    rationale: str = Field(min_length=1, max_length=1200)
    receipt_refs: tuple[ArtifactRef, ...] = Field(min_length=1, max_length=32)
    state_refs: tuple[ArtifactRef, ...] = Field(min_length=1, max_length=32)
    prompt_contract_hash: ArtifactId
    skill_contract_hash: ArtifactId
    model_call: ModelCallRecord
    proposal_only: Literal[True] = True
    may_mutate_canon: Literal[False] = False
    may_mutate_active_skill: Literal[False] = False
    may_execute_tools: Literal[False] = False

    @model_validator(mode="after")
    def validate_selection_partition(self) -> RecoveryProposal:
        considered = set(self.considered_action_ids)
        rejected = set(self.rejected_action_ids)
        if len(considered) != len(self.considered_action_ids):
            raise ValueError("considered recovery action identities must be unique")
        if len(rejected) != len(self.rejected_action_ids):
            raise ValueError("rejected recovery action identities must be unique")
        if self.selected_action_id not in considered:
            raise ValueError("selected recovery action was not considered")
        if self.selected_action_id in rejected:
            raise ValueError("selected recovery action cannot also be rejected")
        if rejected != considered - {self.selected_action_id}:
            raise ValueError("proposal must account for every unselected recovery action")
        return self


class RecoveryReasonerResult(DomainModel):
    proposal: RecoveryProposal
    proposal_ref: ArtifactRef


__all__ = [
    "RECOVERY_PROPOSAL_MEDIA_TYPE",
    "RecoveryActionCandidate",
    "RecoveryActionKind",
    "RecoveryProposal",
    "RecoveryProposalDraft",
    "RecoveryReasonerAdmission",
    "RecoveryReasonerBudget",
    "RecoveryReasonerRequest",
    "RecoveryReasonerResult",
]
