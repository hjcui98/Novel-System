"""Read-only memory projection consistency audit contracts."""

from __future__ import annotations

from pydantic import Field

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import CommitId, ProjectId, StableId


class ProjectionChannelAudit(DomainModel):
    channel: str = Field(min_length=1)
    chapter_counts: dict[int, int] = Field(default_factory=dict)
    missing_chapters: tuple[int, ...] = ()
    extra_chapters: tuple[int, ...] = ()
    stale_commit_or_snapshot_rows: int = Field(default=0, ge=0)
    content_hash_mismatches: int = Field(default=0, ge=0)


class MemoryProjectionAuditReport(DomainModel):
    project_id: ProjectId
    commit_id: CommitId
    snapshot_id: StableId
    canonical_chapter_count: int = Field(ge=0)
    text_root_chapter_indexes: tuple[int, ...] = ()
    channels: tuple[ProjectionChannelAudit, ...] = ()
    missing_chapters: tuple[int, ...] = ()
    extra_chapters: tuple[int, ...] = ()
    stale_commit_or_snapshot_rows: int = Field(default=0, ge=0)
    content_hash_mismatches: int = Field(default=0, ge=0)
    canary_query_results: tuple[str, ...] = ()
    exact: bool = False
    diagnostics: tuple[str, ...] = ()
