"""U8-D/U8-E offline evolution, sealed evaluation, and promotion contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, CommitId, StableId
from novel_agent.domain.model_calls import ModelCallRecord
from novel_agent.domain.runtime import FailureClass

EVOLUTION_CAMPAIGN_RESULT_MEDIA_TYPE = "application/vnd.novel-agent.evolution-campaign-result+json"
EVOLUTION_PROMOTION_RECEIPT_MEDIA_TYPE = (
    "application/vnd.novel-agent.evolution-promotion-receipt+json"
)
EVOLUTION_ROLLBACK_RECEIPT_MEDIA_TYPE = (
    "application/vnd.novel-agent.evolution-rollback-receipt+json"
)


class EvolutionTargetKind(StrEnum):
    PROMPT = "prompt"
    SKILL = "skill"
    POLICY = "policy"
    CODE = "code"


class EvolutionCheckpointUse(StrEnum):
    CALIBRATION = "calibration"
    SEALED_ACCEPTANCE = "sealed_acceptance"
    CANARY = "canary"


class EvolutionDecision(StrEnum):
    PROMOTE = "promote"
    REJECT = "reject"
    HUMAN_REVIEW = "human_review"


class EvolutionIncident(DomainModel):
    incident_id: StableId
    problem_key: StableId
    failure_class: FailureClass
    safety_boundary_id: ArtifactId
    target_id: StableId
    target_kind: EvolutionTargetKind
    incident_ref: ArtifactRef


class EvolutionIncidentCluster(DomainModel):
    cluster_id: StableId
    problem_key: StableId
    failure_class: FailureClass
    safety_boundary_id: ArtifactId
    target_id: StableId
    target_kind: EvolutionTargetKind
    incidents: tuple[EvolutionIncident, ...] = Field(min_length=2, max_length=64)

    @model_validator(mode="after")
    def validate_cluster(self) -> EvolutionIncidentCluster:
        if len({item.incident_id for item in self.incidents}) != len(self.incidents):
            raise ValueError("evolution incident identities must be unique")
        for incident in self.incidents:
            observed = (
                incident.problem_key,
                incident.failure_class,
                incident.safety_boundary_id,
                incident.target_id,
                incident.target_kind,
            )
            expected = (
                self.problem_key,
                self.failure_class,
                self.safety_boundary_id,
                self.target_id,
                self.target_kind,
            )
            if observed != expected:
                raise ValueError("incident differs from its evolution cluster identity")
        return self


class EvolutionCandidateDraft(DomainModel):
    replacement_content: str = Field(min_length=1, max_length=65536)
    affected_contract_ids: tuple[StableId, ...] = Field(min_length=1, max_length=8)
    change_summary: str = Field(min_length=1, max_length=1200)


class EvolutionCandidateGenerationBudget(DomainModel):
    max_context_bytes: int = Field(default=65536, ge=1024, le=1048576)
    max_output_tokens: int = Field(default=8192, ge=256, le=65536)
    timeout_seconds: float = Field(default=120.0, gt=0, le=900)
    enable_thinking: bool = False
    thinking_token_budget: int = Field(default=0, ge=0, le=16384)

    @model_validator(mode="after")
    def validate_thinking(self) -> EvolutionCandidateGenerationBudget:
        if self.enable_thinking != (self.thinking_token_budget > 0):
            raise ValueError("thinking flag and thinking budget must agree")
        return self


class EvolutionCandidateGenerationRequest(DomainModel):
    request_id: StableId
    model_request_id: StableId
    cluster: EvolutionIncidentCluster
    base_artifact_ref: ArtifactRef
    prompt_contract_hash: ArtifactId
    skill_contract_hash: ArtifactId
    requires_human_gate: bool = False
    budget: EvolutionCandidateGenerationBudget = Field(
        default_factory=EvolutionCandidateGenerationBudget
    )
    maximum_candidates: Literal[1] = 1
    runtime_git_access: Literal[False] = False
    runtime_install_access: Literal[False] = False


class EvolutionCandidate(DomainModel):
    candidate_id: StableId
    target_id: StableId
    target_kind: EvolutionTargetKind
    base_artifact_ref: ArtifactRef
    candidate_artifact_ref: ArtifactRef
    incident_refs: tuple[ArtifactRef, ...] = Field(min_length=2, max_length=64)
    affected_contract_ids: tuple[StableId, ...] = Field(min_length=1, max_length=8)
    change_summary: str = Field(min_length=1, max_length=1200)
    bounded_single_change: Literal[True] = True
    isolated_candidate: Literal[True] = True
    runtime_hot_mutation_allowed: Literal[False] = False
    requires_human_gate: bool = False

    @model_validator(mode="after")
    def validate_candidate(self) -> EvolutionCandidate:
        if self.base_artifact_ref.artifact_id == self.candidate_artifact_ref.artifact_id:
            raise ValueError("evolution candidate must differ from its active base")
        if len(set(self.incident_refs)) != len(self.incident_refs):
            raise ValueError("evolution candidate incident corpus must be unique")
        if len(set(self.affected_contract_ids)) != len(self.affected_contract_ids):
            raise ValueError("affected contract identities must be unique")
        if self.target_kind is EvolutionTargetKind.CODE and not self.requires_human_gate:
            raise ValueError("code evolution always requires the Codex/human gate")
        return self


class EvolutionCandidateGenerationResult(DomainModel):
    candidate: EvolutionCandidate
    candidate_ref: ArtifactRef
    model_call: ModelCallRecord

    @model_validator(mode="after")
    def validate_candidate_artifact_identity(self) -> EvolutionCandidateGenerationResult:
        if self.candidate_ref != self.candidate.candidate_artifact_ref:
            raise ValueError("candidate result artifact differs from candidate identity")
        return self


class EvolutionCheckpointAssignment(DomainModel):
    checkpoint_id: StableId
    use: EvolutionCheckpointUse
    basis_commit: CommitId
    basis_ref: ArtifactRef
    incident_ids: tuple[StableId, ...] = Field(min_length=1, max_length=64)


class EvolutionMetricThreshold(DomainModel):
    metric: str = Field(min_length=1, max_length=128)
    minimum_delta: float = Field(ge=0.0, allow_inf_nan=False)
    higher_is_better: bool = True


class EvolutionCampaignManifest(DomainModel):
    manifest_version: Literal["u8-evolution-campaign.v1"] = "u8-evolution-campaign.v1"
    campaign_id: StableId
    candidate: EvolutionCandidate
    assignments: tuple[EvolutionCheckpointAssignment, ...] = Field(min_length=3)
    thresholds: tuple[EvolutionMetricThreshold, ...] = Field(min_length=1, max_length=32)
    code_source_fingerprint: ArtifactId
    configuration_fingerprint: ArtifactId
    preregistered_at: datetime
    whole_checkpoint_split: Literal[True] = True
    held_out_read_after_development_freeze: Literal[True] = True
    evaluator_feedback_writeback: Literal[False] = False
    online_policy_mutation: Literal[False] = False
    write_once: Literal[True] = True

    @model_validator(mode="after")
    def validate_split(self) -> EvolutionCampaignManifest:
        checkpoint_ids = tuple(item.checkpoint_id for item in self.assignments)
        if len(set(checkpoint_ids)) != len(checkpoint_ids):
            raise ValueError("a checkpoint may be assigned to exactly one campaign use")
        incident_ids = tuple(
            incident_id
            for assignment in self.assignments
            for incident_id in assignment.incident_ids
        )
        if len(set(incident_ids)) != len(incident_ids):
            raise ValueError("an incident identity may belong to exactly one campaign use")
        uses = {item.use for item in self.assignments}
        required = {
            EvolutionCheckpointUse.CALIBRATION,
            EvolutionCheckpointUse.SEALED_ACCEPTANCE,
            EvolutionCheckpointUse.CANARY,
        }
        if uses != required:
            raise ValueError("campaign requires calibration, sealed acceptance, and canary")
        metrics = tuple(item.metric for item in self.thresholds)
        if len(set(metrics)) != len(metrics):
            raise ValueError("evolution metric thresholds must be unique")
        return self


class EvolutionMetricComparison(DomainModel):
    metric: str = Field(min_length=1, max_length=128)
    baseline_value: float = Field(allow_inf_nan=False)
    candidate_value: float = Field(allow_inf_nan=False)


class EvolutionCheckpointResult(DomainModel):
    checkpoint_id: StableId
    use: EvolutionCheckpointUse
    basis_commit: CommitId
    candidate_artifact_id: ArtifactId
    comparisons: tuple[EvolutionMetricComparison, ...] = Field(min_length=1, max_length=32)
    hard_failure_codes: tuple[str, ...] = Field(default=(), max_length=32)
    evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_metrics(self) -> EvolutionCheckpointResult:
        metrics = tuple(item.metric for item in self.comparisons)
        if len(set(metrics)) != len(metrics):
            raise ValueError("checkpoint result metrics must be unique")
        return self


class EvolutionCampaignResult(DomainModel):
    campaign_id: StableId
    candidate_id: StableId
    decision: EvolutionDecision
    calibration_results: tuple[EvolutionCheckpointResult, ...] = Field(min_length=1)
    sealed_results: tuple[EvolutionCheckpointResult, ...] = ()
    canary_results: tuple[EvolutionCheckpointResult, ...] = ()
    decision_codes: tuple[str, ...] = Field(min_length=1, max_length=32)
    sealed_opened_once: bool
    evaluator_feedback_written_back: Literal[False] = False
    active_version_changed: Literal[False] = False

    @model_validator(mode="after")
    def validate_decision_evidence(self) -> EvolutionCampaignResult:
        if self.decision in {
            EvolutionDecision.PROMOTE,
            EvolutionDecision.HUMAN_REVIEW,
        } and (not self.sealed_opened_once or not self.sealed_results or not self.canary_results):
            raise ValueError("promotion decisions require sealed and canary evidence")
        if self.sealed_results and not self.sealed_opened_once:
            raise ValueError("sealed results require a recorded one-shot opening")
        if self.canary_results and (not self.sealed_opened_once or not self.sealed_results):
            raise ValueError("canary results require sealed evidence and a one-shot opening")
        return self


class EvolutionPromotionReceipt(DomainModel):
    receipt_id: StableId
    campaign_id: StableId
    target_id: StableId
    target_kind: EvolutionTargetKind
    previous_active_ref: ArtifactRef
    promoted_ref: ArtifactRef
    campaign_result_ref: ArtifactRef
    promoted_at: datetime
    decision: Literal[EvolutionDecision.PROMOTE] = EvolutionDecision.PROMOTE
    production_hot_swap: Literal[False] = False


class EvolutionRollbackReceipt(DomainModel):
    receipt_id: StableId
    target_id: StableId
    failed_promotion_ref: ArtifactRef
    restored_ref: ArtifactRef
    failure_evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1, max_length=32)
    rolled_back_at: datetime
    production_hot_swap: Literal[False] = False


__all__ = [
    "EVOLUTION_CAMPAIGN_RESULT_MEDIA_TYPE",
    "EVOLUTION_PROMOTION_RECEIPT_MEDIA_TYPE",
    "EVOLUTION_ROLLBACK_RECEIPT_MEDIA_TYPE",
    "EvolutionCampaignManifest",
    "EvolutionCampaignResult",
    "EvolutionCandidate",
    "EvolutionCandidateDraft",
    "EvolutionCandidateGenerationBudget",
    "EvolutionCandidateGenerationRequest",
    "EvolutionCandidateGenerationResult",
    "EvolutionCheckpointAssignment",
    "EvolutionCheckpointResult",
    "EvolutionCheckpointUse",
    "EvolutionDecision",
    "EvolutionIncident",
    "EvolutionIncidentCluster",
    "EvolutionMetricComparison",
    "EvolutionMetricThreshold",
    "EvolutionPromotionReceipt",
    "EvolutionRollbackReceipt",
    "EvolutionTargetKind",
]
