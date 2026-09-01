"""Typed evidence for the U6-C long-running runtime fault matrix."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import CommitId, RunId, StableId


class U6CFaultCase(StrEnum):
    PROVIDER_BEFORE_WORKER_KILL = "provider_request_before_worker_kill"
    PROVIDER_AFTER_REQUEST_BEFORE_RAW = "provider_request_after_send_before_raw"
    STREAMING_PARTIAL_BEFORE_RAW = "streaming_partial_before_raw"
    PROVIDER_RAW_BEFORE_PARSE = "provider_raw_before_parse"
    PARSE_BEFORE_LEAF_CHECKPOINT = "parse_before_leaf_checkpoint"
    ACCEPTANCE_BEFORE_KILL = "acceptance_before_kill"
    COMMIT_BEFORE_SETTLEMENT = "commit_before_receipt_settlement"
    PROJECTION_FAILURE = "projection_failure"
    LEASE_EXPIRY = "lease_expiry"
    BASIS_FRESHNESS_CHANGE = "basis_freshness_change"
    REPEATED_FAILURE = "repeated_validation_semantic_failure"
    PLANNER_REQUEST_MEMORY = "planner_request_memory"
    EDITOR_REPAIR_OR_REWRITE = "editor_repair_or_major_rewrite"
    SUPERVISOR_FINDING = "supervisor_finding"
    CHECKPOINT_BEFORE_RELEASE = "checkpoint_before_question_plan_release"
    WRITER_RAW_BEFORE_PARSE = "writer_raw_before_parse"
    RESPONSE_FREEZE_BEFORE_JUDGE = "response_freeze_before_judge"
    EVALUATOR_BEFORE_DISCARD = "evaluator_before_discard"


class U6CFaultCaseStatus(StrEnum):
    PASS = "PASS"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class U6CFaultCaseResult(DomainModel):
    """One immutable case receipt; the harness never writes runtime state directly."""

    case: U6CFaultCase
    run_id: RunId
    injection_point: str = Field(min_length=1, max_length=240)
    expected_action: str = Field(min_length=1, max_length=240)
    observed_action: str = Field(min_length=1, max_length=240)
    status: U6CFaultCaseStatus
    safe_checkpoint_id: StableId
    old_attempt_id: StableId | None = None
    old_fence_generation: int | None = Field(default=None, ge=1)
    new_attempt_id: StableId | None = None
    new_fence_generation: int | None = Field(default=None, ge=1)
    provider_call_count: int = Field(default=0, ge=0)
    effect_identities: tuple[StableId, ...] = ()
    recovered_commit_id: CommitId | None = None
    recovered_memory_identity: StableId | None = None
    evidence_artifact_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    forbidden_results: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_status(self) -> U6CFaultCaseResult:
        if self.status is U6CFaultCaseStatus.PASS and self.forbidden_results:
            raise ValueError("passing U6-C case cannot carry forbidden results")
        if self.new_attempt_id is not None and self.new_fence_generation is None:
            raise ValueError("new Attempt requires its fence generation")
        if self.old_attempt_id is not None and self.old_fence_generation is None:
            raise ValueError("old Attempt requires its fence generation")
        return self


class U6CFaultMatrixReport(DomainModel):
    """Complete U6-C receipt with an exact, non-repeatable case set."""

    report_schema: Literal["u6c-fault-matrix.v1"] = "u6c-fault-matrix.v1"
    experiment_id: str = Field(min_length=1, max_length=160)
    database_descriptor: str = Field(min_length=1, max_length=240)
    status: U6CFaultCaseStatus
    cases: tuple[U6CFaultCaseResult, ...] = Field(min_length=18, max_length=18)
    total_provider_call_count: int = Field(ge=0)
    total_effect_count: int = Field(ge=0)
    total_commit_count: int = Field(ge=0)
    projection_rebuild_verified: bool
    duplicate_effect_count: int = Field(ge=0)
    forbidden_result_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_matrix(self) -> U6CFaultMatrixReport:
        expected = set(U6CFaultCase)
        actual = [case.case for case in self.cases]
        if set(actual) != expected or len(actual) != len(set(actual)):
            raise ValueError("U6-C report must contain each fault case exactly once")
        derived_forbidden = sum(len(case.forbidden_results) for case in self.cases)
        if self.forbidden_result_count != derived_forbidden:
            raise ValueError("U6-C forbidden result count is not case-derived")
        if self.total_provider_call_count != sum(case.provider_call_count for case in self.cases):
            raise ValueError("U6-C provider call count is not case-derived")
        if self.status is U6CFaultCaseStatus.PASS and (
            any(case.status is not U6CFaultCaseStatus.PASS for case in self.cases)
            or not self.projection_rebuild_verified
            or self.forbidden_result_count
            or self.duplicate_effect_count
        ):
            raise ValueError("U6-C PASS contradicts a failed case or invariant")
        return self


__all__ = [
    "U6CFaultCase",
    "U6CFaultCaseResult",
    "U6CFaultCaseStatus",
    "U6CFaultMatrixReport",
]
