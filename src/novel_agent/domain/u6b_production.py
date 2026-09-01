"""Durable evidence contracts for the U6-B production baseline."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import CommitId, ProjectId, RunId, StableId, TaskId


class U6BCompactionOutcome(StrEnum):
    COMPACTED = "COMPACTED"
    NO_OP = "NO_OP"
    INEFFECTIVE = "INEFFECTIVE"


class U6BCompactionEvidence(DomainModel):
    """One truthful compaction observation, including the no-pressure case."""

    receipt_id: StableId
    run_id: RunId
    task_id: TaskId
    chapter_index: int = Field(ge=1)
    outcome: U6BCompactionOutcome
    input_context_tokens: int = Field(ge=0)
    output_context_tokens: int = Field(ge=0)
    reduction_ratio: float = Field(ge=0.0, le=1.0)
    min_reduction_ratio: float = Field(ge=0.0, le=1.0)
    covered_event_range: tuple[int, int]
    protected_items_retained: bool
    pending_effects_retained: bool
    safe_cut: bool
    semantic_retention_passed: bool
    source_receipt_id: StableId | None = None

    @model_validator(mode="after")
    def validate_measurement(self) -> U6BCompactionEvidence:
        if (
            self.covered_event_range[0] < 1
            or self.covered_event_range[1] < self.covered_event_range[0]
        ):
            raise ValueError("U6-B compaction event range is invalid")
        expected = (
            0.0
            if self.input_context_tokens == 0
            else (self.input_context_tokens - self.output_context_tokens)
            / self.input_context_tokens
        )
        if abs(self.reduction_ratio - expected) > 1e-9:
            raise ValueError("U6-B compaction ratio contradicts token counts")
        if self.outcome is U6BCompactionOutcome.NO_OP and (
            self.source_receipt_id is not None
            or self.input_context_tokens != self.output_context_tokens
            or self.reduction_ratio != 0.0
        ):
            raise ValueError("U6-B NO_OP must have no source receipt and no token reduction")
        if self.outcome is U6BCompactionOutcome.COMPACTED and self.source_receipt_id is None:
            raise ValueError("U6-B COMPACTED evidence requires the runtime receipt identity")
        if self.outcome is U6BCompactionOutcome.INEFFECTIVE and (
            self.reduction_ratio >= self.min_reduction_ratio
        ):
            raise ValueError("U6-B INEFFECTIVE evidence reached the registered ratio")
        return self


class U6BPhaseUsage(DomainModel):
    chapter_index: int = Field(ge=1)
    phase: Literal["plan", "memory", "writer", "editor", "settlement", "recovery"]
    wall_clock_ms: int = Field(ge=0)
    model_call_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    attempt_count: int = Field(ge=0)


class U6BWorkerPhaseReport(DomainModel):
    phase_index: int = Field(ge=1)
    report_path: str = Field(min_length=1)
    status: str = Field(min_length=1)
    completed_chapters_before: tuple[int, ...] = ()
    completed_chapters_after: tuple[int, ...] = ()
    restarted_from_process: bool


class U6BProductionBaselineReport(DomainModel):
    """Rebuildable summary for one isolated 20-chapter production campaign."""

    report_schema: Literal["u6b-production-baseline.v1"] = "u6b-production-baseline.v1"
    status: Literal["PASS", "REVIEW_REQUIRED", "RESOURCE_BLOCKED"]
    run_id: RunId
    project_id: ProjectId
    basis_commit: CommitId
    final_commit: CommitId
    expected_chapters: tuple[int, ...]
    completed_chapters: tuple[int, ...]
    restart_boundary_chapter: int = Field(ge=1)
    worker_phases: tuple[U6BWorkerPhaseReport, ...]
    phase_usage: tuple[U6BPhaseUsage, ...]
    compaction: tuple[U6BCompactionEvidence, ...]
    model_call_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    event_count: int = Field(ge=0)
    task_count: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    commit_count: int = Field(ge=0)
    artifact_count: int = Field(ge=0)
    future_leakage_count: int = Field(ge=0)
    duplicate_effect_count: int = Field(ge=0)
    projection_rebuild_verified: bool
    semantic_findings: tuple[str, ...] = ()
    repair_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_campaign(self) -> U6BProductionBaselineReport:
        if not set(self.completed_chapters).issubset(set(self.expected_chapters)):
            raise ValueError("U6-B completed chapter lies outside expected range")
        if self.status == "PASS" and (
            tuple(self.completed_chapters) != tuple(self.expected_chapters)
            or self.future_leakage_count != 0
            or self.duplicate_effect_count != 0
            or not self.projection_rebuild_verified
        ):
            raise ValueError("U6-B PASS requires complete, clean, rebuilt evidence")
        return self


__all__ = [
    "U6BCompactionEvidence",
    "U6BCompactionOutcome",
    "U6BPhaseUsage",
    "U6BProductionBaselineReport",
    "U6BWorkerPhaseReport",
]
