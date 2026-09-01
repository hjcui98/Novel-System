"""Evidence contract for the real Stage 3 Writer leaf Gate."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import CommitId, ProjectId, RunId, StableId, TaskId
from novel_agent.domain.model_calls import (
    EffectiveBudgetResult,
    ModelCallLedgerAggregate,
    ModelCallLedgerEntry,
)
from novel_agent.domain.production_assembly import ResolvedProductionAssemblyAttestation
from novel_agent.domain.retrieval_routing import ProjectionAttestation
from novel_agent.domain.stage2 import SkillExecutionReceipt
from novel_agent.domain.writing_loop import WritingLoopResult


class U4L1GateStatus(StrEnum):
    PASS = "PASS"
    FAILED = "FAILED"
    RESOURCE_BLOCKED = "RESOURCE_BLOCKED"


class U4L1RubricStatus(StrEnum):
    MECHANICAL = "mechanical"
    NOT_SCORED = "not_scored"
    FAILED = "failed"


class U4L1BoundaryCheck(DomainModel):
    name: str = Field(min_length=1, max_length=128)
    passed: bool
    detail: str = Field(min_length=1, max_length=2048)
    evidence_refs: tuple[ArtifactRef, ...] = ()


class U4L1RubricItem(DomainModel):
    dimension: Literal[
        "plan_obedience",
        "evidence_use",
        "knowledge_boundary",
        "readability",
        "repair_convergence",
        "cost",
    ]
    status: U4L1RubricStatus
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    detail: str = Field(min_length=1, max_length=2048)
    evidence_refs: tuple[ArtifactRef, ...] = ()

    @model_validator(mode="after")
    def validate_score(self) -> U4L1RubricItem:
        if self.status is U4L1RubricStatus.NOT_SCORED and self.score is not None:
            raise ValueError("not-scored U4-L1 rubric item cannot carry a score")
        return self


class U4L1WriterLeafReport(DomainModel):
    """One immutable, candidate-only Stage 3 Writer leaf observation."""

    report_version: str = "u4l1-real-writer-leaf.v1"
    report_id: StableId
    generated_at: datetime
    gate_status: U4L1GateStatus
    gate_blockers: tuple[str, ...] = ()
    project_id: ProjectId
    run_id: RunId
    task_id: TaskId
    basis_commit: CommitId
    snapshot_id: StableId
    model_identity: dict[str, JsonValue]
    endpoint_url: str = Field(min_length=1)
    production_attestation: ResolvedProductionAssemblyAttestation
    projection_attestation: ProjectionAttestation
    request_artifacts: tuple[ArtifactRef, ...]
    writing_task_artifact: ArtifactRef
    accepted_plan_artifact: ArtifactRef
    project_profile_artifact: ArtifactRef
    writer_context_package_artifact: ArtifactRef
    evidence_ledger_artifact: ArtifactRef
    recent_prose_artifact: ArtifactRef
    exact_editor_context_ref: ArtifactRef | None = None
    raw_artifact_refs: tuple[ArtifactRef, ...] = ()
    parsed_artifact_refs: tuple[ArtifactRef, ...] = ()
    memory_request_refs: tuple[ArtifactRef, ...] = ()
    skill_receipts: tuple[SkillExecutionReceipt, ...] = ()
    effective_budgets: tuple[EffectiveBudgetResult, ...] = ()
    ledger_entries: tuple[ModelCallLedgerEntry, ...] = ()
    model_call_aggregates: tuple[ModelCallLedgerAggregate, ...] = ()
    api_budget_consistent: bool
    ledger_report_reconstructed: bool
    editor_verdicts: tuple[str, ...] = ()
    boundary_checks: tuple[U4L1BoundaryCheck, ...]
    rubric: tuple[U4L1RubricItem, ...]
    result: WritingLoopResult
    candidate_only: Literal[True] = True
    chapter_settlement_called: Literal[False] = False

    @model_validator(mode="after")
    def validate_report(self) -> U4L1WriterLeafReport:
        if self.result.run_id != self.run_id or self.result.task_id != self.task_id:
            raise ValueError("U4-L1 result lineage differs from report identity")
        if (
            self.projection_attestation.source_commit != self.basis_commit
            or self.projection_attestation.snapshot_id != self.snapshot_id
            or self.projection_attestation.capability.source_commit != self.basis_commit
            or self.projection_attestation.capability.snapshot_id != self.snapshot_id
        ):
            raise ValueError("U4-L1 projection attestation differs from report basis")
        if self.result.candidate_only is not True:
            raise ValueError("U4-L1 report requires candidate-only Writer output")
        expected_rubric = {
            "plan_obedience",
            "evidence_use",
            "knowledge_boundary",
            "readability",
            "repair_convergence",
            "cost",
        }
        if {item.dimension for item in self.rubric} != expected_rubric:
            raise ValueError("U4-L1 rubric must cover exactly the six Gate dimensions")
        if self.gate_status is U4L1GateStatus.PASS:
            if self.gate_blockers:
                raise ValueError("passing U4-L1 report cannot retain Gate blockers")
            if self.result.status.value != "DRAFT_CANDIDATE_READY":
                raise ValueError("passing U4-L1 report requires a ready Draft candidate")
            if not self.api_budget_consistent or not self.ledger_report_reconstructed:
                raise ValueError("passing U4-L1 report requires durable budget reconstruction")
            if any(not check.passed for check in self.boundary_checks):
                raise ValueError("passing U4-L1 report cannot retain boundary failures")
        return self


__all__ = [
    "U4L1BoundaryCheck",
    "U4L1GateStatus",
    "U4L1RubricItem",
    "U4L1RubricStatus",
    "U4L1WriterLeafReport",
]
