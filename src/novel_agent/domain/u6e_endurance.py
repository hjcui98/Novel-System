"""Typed evidence contract for the U6-E fifty-chapter endurance baseline."""

from __future__ import annotations

from itertools import pairwise
from typing import Literal

from pydantic import Field, model_validator

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import CommitId, ProjectId, RunId
from novel_agent.domain.u6b_production import U6BCompactionEvidence, U6BPhaseUsage


class U6EWorkerPhaseReport(DomainModel):
    phase_index: int = Field(ge=1)
    report_path: str = Field(min_length=1)
    status: str = Field(min_length=1)
    completed_chapters_before: tuple[int, ...] = ()
    completed_chapters_after: tuple[int, ...] = ()
    restarted_from_process: bool


class U6EHistoryGrowth(DomainModel):
    chapter_index: int = Field(ge=0)
    event_count: int = Field(ge=0)
    task_count: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    effect_count: int = Field(ge=0)
    model_call_count: int = Field(ge=0)
    artifact_count: int = Field(ge=0)


class U6EHealthProbe(DomainModel):
    probe_id: str = Field(min_length=1, max_length=128)
    chapter_index: int = Field(ge=0)
    status: Literal["PASS", "REVIEW_REQUIRED"]
    event_count: int = Field(ge=0)
    task_count: int = Field(ge=0)
    model_call_count: int = Field(ge=0)
    detail: str = Field(min_length=1, max_length=512)


class U6EEnduranceReport(DomainModel):
    """Rebuildable summary for one 50-chapter PostgreSQL production baseline."""

    report_schema: Literal["u6e-endurance.v1"] = "u6e-endurance.v1"
    status: Literal["PASS", "REVIEW_REQUIRED", "RESOURCE_BLOCKED"]
    run_id: RunId
    project_id: ProjectId
    basis_commit: CommitId
    final_commit: CommitId
    expected_chapters: tuple[int, ...] = Field(min_length=1)
    completed_chapters: tuple[int, ...]
    restart_boundary_chapter: int = Field(ge=1)
    worker_phases: tuple[U6EWorkerPhaseReport, ...] = Field(min_length=2)
    history_growth: tuple[U6EHistoryGrowth, ...] = Field(min_length=2)
    health_probes: tuple[U6EHealthProbe, ...] = Field(min_length=1)
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
    duplicate_commit_count: int = Field(ge=0)
    unrecoverable_task_count: int = Field(ge=0)
    external_wait_count: int = Field(ge=0)
    repeated_failure_count: int = Field(ge=0)
    projection_rebuild_verified: bool
    cold_restart_verified: bool
    process_memory_dependency: bool
    semantic_findings: tuple[str, ...] = ()
    repair_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_endurance(self) -> U6EEnduranceReport:
        expected = tuple(self.expected_chapters)
        if len(expected) != 50 or expected != tuple(range(expected[0], expected[0] + 50)):
            raise ValueError("U6-E requires exactly fifty contiguous expected chapters")
        if not set(self.completed_chapters).issubset(set(expected)):
            raise ValueError("U6-E completed chapter lies outside expected range")
        growth = tuple(self.history_growth)
        if any(later.chapter_index <= earlier.chapter_index for earlier, later in pairwise(growth)):
            raise ValueError("U6-E history growth checkpoints must be ordered")
        for earlier, later in pairwise(growth):
            for field in (
                "event_count",
                "task_count",
                "attempt_count",
                "effect_count",
                "model_call_count",
                "artifact_count",
            ):
                if getattr(later, field) < getattr(earlier, field):
                    raise ValueError("U6-E history growth cannot decrease")
        if self.status == "PASS" and (
            tuple(self.completed_chapters) != expected
            or len(self.worker_phases) != 2
            or not self.worker_phases[-1].restarted_from_process
            or any(probe.status != "PASS" for probe in self.health_probes)
            or any(item.outcome.value == "INEFFECTIVE" for item in self.compaction)
            or self.future_leakage_count
            or self.duplicate_effect_count
            or self.duplicate_commit_count
            or self.unrecoverable_task_count
            or self.external_wait_count
            or not self.projection_rebuild_verified
            or not self.cold_restart_verified
            or self.process_memory_dependency
        ):
            raise ValueError("U6-E PASS requires complete, clean, restartable evidence")
        return self


__all__ = [
    "U6EEnduranceReport",
    "U6EHealthProbe",
    "U6EHistoryGrowth",
    "U6EWorkerPhaseReport",
]
