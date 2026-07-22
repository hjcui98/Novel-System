"""Stage 1B teacher-forced replay result contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, JsonValue, model_validator

from novel_agent.domain.base import DomainModel
from novel_agent.domain.changes import ObservedChangeSet, ValidationReport, WorldRecordKind
from novel_agent.domain.ids import CommitId, ProjectId, StableId
from novel_agent.domain.memory import FreshnessDecision
from novel_agent.domain.model_calls import ModelCallRecord


class ReplayChapterStatus(StrEnum):
    COMMITTED = "committed"
    BLOCKED_BY_VALIDATION = "blocked_by_validation"
    BLOCKED_BY_FRESHNESS = "blocked_by_freshness"


class ReplayMaterializedRecord(DomainModel):
    record_kind: WorldRecordKind
    target_id: StableId
    record: dict[str, JsonValue]


class ReplayChapterResult(DomainModel):
    chapter_index: int = Field(ge=1)
    base_commit: CommitId
    status: ReplayChapterStatus
    validation_report: ValidationReport
    observed_changes: ObservedChangeSet
    commit_id: CommitId | None = None
    snapshot_id: StableId | None = None
    freshness: FreshnessDecision | None = None
    materialized_records: tuple[ReplayMaterializedRecord, ...] = ()
    manual_repair: bool = False

    @model_validator(mode="after")
    def validate_result(self) -> ReplayChapterResult:
        if self.status in (
            ReplayChapterStatus.COMMITTED,
            ReplayChapterStatus.BLOCKED_BY_FRESHNESS,
        ):
            if self.commit_id is None or self.snapshot_id is None or self.freshness is None:
                raise ValueError(
                    "post-commit replay result requires commit, snapshot, and freshness"
                )
        elif self.commit_id is not None:
            raise ValueError("validation-blocked replay chapter cannot publish a commit")
        return self


class ContinuousReplayResult(DomainModel):
    replay_id: StableId
    project_id: ProjectId
    chapter_results: tuple[ReplayChapterResult, ...]
    committed_chapters: int = Field(ge=0)
    blocked_chapters: int = Field(ge=0)
    silent_canonical_pollution_count: int = Field(ge=0)
    silent_stale_snapshot_reads: int = Field(ge=0)
    first_pollution_chapter: int | None = Field(default=None, ge=1)
    model_calls: tuple[ModelCallRecord, ...] = ()
