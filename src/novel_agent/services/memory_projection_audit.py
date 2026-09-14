"""Read-only TextRoot/R1/anchor/grounded projection consistency audit.

Default action is read-back audit, never a rebuild; only a failing channel is
rebuilt by the operator afterwards.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.postgres.models import R1RecordRow
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.ids import CommitId, ProjectId, StableId
from novel_agent.domain.memory import DerivedBuildStatus
from novel_agent.domain.projection_audit import (
    MemoryProjectionAuditReport,
    ProjectionChannelAudit,
)
from novel_agent.ports.search_index import SearchIndexPort
from novel_agent.services.projection import DerivedSnapshotRepository


class MemoryProjectionAuditor:
    """Audit the existing projection without mutating any channel."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        snapshots: DerivedSnapshotRepository,
        index: SearchIndexPort | None = None,
        grounded_index: str | None = None,
        anchor_index: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._snapshots = snapshots
        self._index = index
        self._grounded_index = grounded_index
        self._anchor_index = anchor_index

    def audit(
        self,
        *,
        project_id: ProjectId,
        commit_id: CommitId,
        snapshot_id: StableId,
        text_root: TextRootDocument,
        canary_queries: Sequence[tuple[str, Mapping[str, Any]]] = (),
    ) -> MemoryProjectionAuditReport:
        canonical = tuple(chapter.chapter_index for chapter in text_root.chapters)
        diagnostics: list[str] = []
        channels: list[ProjectionChannelAudit] = []

        snapshot = self._snapshots.get_for_commit(commit_id)
        stale_rows = 0
        if snapshot is None:
            stale_rows += 1
            diagnostics.append("MISSING_DERIVED_SNAPSHOT")
        else:
            if snapshot.snapshot_id != snapshot_id:
                stale_rows += 1
                diagnostics.append("SNAPSHOT_ID_MISMATCH")
            if snapshot.build_status is not DerivedBuildStatus.EXACT:
                stale_rows += 1
                diagnostics.append("BUILD_STATUS_NOT_EXACT")

        r1_channel = self._r1_channel(project_id, commit_id, canonical)
        channels.append(r1_channel)

        for channel_name, index_name in (
            ("anchor", self._anchor_index),
            ("grounded", self._grounded_index),
        ):
            if self._index is None or index_name is None:
                continue
            channels.append(self._search_channel(channel_name, index_name, snapshot_id, canonical))

        missing = tuple(
            chapter
            for chapter in canonical
            if any(chapter in channel.missing_chapters for channel in channels)
        )
        extra = tuple(
            sorted({chapter for channel in channels for chapter in channel.extra_chapters})
        )
        canary_results = self._canary(canary_queries)
        content_hash_mismatches = sum(channel.content_hash_mismatches for channel in channels)
        exact = (
            not missing
            and not extra
            and stale_rows == 0
            and content_hash_mismatches == 0
            and bool(channels)
        )
        if missing:
            diagnostics.append("MISSING_CHAPTERS:" + ",".join(str(item) for item in missing))
        if extra:
            diagnostics.append("EXTRA_CHAPTERS:" + ",".join(str(item) for item in extra))
        return MemoryProjectionAuditReport(
            project_id=project_id,
            commit_id=commit_id,
            snapshot_id=snapshot_id,
            canonical_chapter_count=len(canonical),
            text_root_chapter_indexes=canonical,
            channels=tuple(channels),
            missing_chapters=missing,
            extra_chapters=extra,
            stale_commit_or_snapshot_rows=stale_rows,
            content_hash_mismatches=content_hash_mismatches,
            canary_query_results=canary_results,
            exact=exact,
            diagnostics=tuple(diagnostics),
        )

    def _r1_channel(
        self,
        project_id: ProjectId,
        commit_id: CommitId,
        canonical: tuple[int, ...],
    ) -> ProjectionChannelAudit:
        with self._session_factory() as session:
            rows = session.execute(
                select(R1RecordRow.narrative_start, func.count())
                .where(
                    R1RecordRow.project_id == project_id.root,
                    R1RecordRow.source_commit == commit_id.root,
                    R1RecordRow.narrative_start.is_not(None),
                )
                .group_by(R1RecordRow.narrative_start)
            ).all()
        counts = {int(chapter): int(count) for chapter, count in rows if chapter is not None}
        canonical_set = set(canonical)
        return ProjectionChannelAudit(
            channel="r1",
            chapter_counts=counts,
            missing_chapters=tuple(chapter for chapter in canonical if chapter not in counts),
            extra_chapters=tuple(
                sorted(chapter for chapter in counts if chapter not in canonical_set)
            ),
        )

    def _search_channel(
        self,
        channel: str,
        index_name: str,
        snapshot_id: StableId,
        canonical: tuple[int, ...],
    ) -> ProjectionChannelAudit:
        assert self._index is not None
        hits = self._index.search(
            index_name,
            {
                "bool": {
                    "filter": [
                        {"term": {"snapshot_id": snapshot_id.root}},
                        {"exists": {"field": "narrative_start"}},
                    ]
                }
            },
            size=10_000,
        )
        counts: dict[int, int] = {}
        for hit in hits:
            raw_source = hit.get("_source")
            source: dict[str, Any] = raw_source if isinstance(raw_source, dict) else hit
            chapter = source.get("narrative_start")
            if type(chapter) is int:
                counts[chapter] = counts.get(chapter, 0) + 1
        canonical_set = set(canonical)
        return ProjectionChannelAudit(
            channel=channel,
            chapter_counts=counts,
            missing_chapters=tuple(chapter for chapter in canonical if chapter not in counts),
            extra_chapters=tuple(
                sorted(chapter for chapter in counts if chapter not in canonical_set)
            ),
        )

    def _canary(
        self,
        canary_queries: Sequence[tuple[str, Mapping[str, Any]]],
    ) -> tuple[str, ...]:
        if not canary_queries or self._index is None:
            return ()
        results: list[str] = []
        for label, query in canary_queries:
            index_name = self._grounded_index or self._anchor_index
            if index_name is None:
                break
            hits = self._index.search(index_name, dict(query), size=1)
            results.append(f"{label}:{'hit' if hits else 'miss'}")
        return tuple(results)
