from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from novel_agent.adapters.postgres.models import Base, R1RecordRow
from novel_agent.domain.benchmark import (
    ChapterDocument,
    SceneDocument,
    TextRootDocument,
)
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, SchemaVersion, StableId
from novel_agent.domain.memory import DerivedBuildStatus, DerivedSnapshotLite
from novel_agent.services.memory_projection_audit import MemoryProjectionAuditor

VERSION = SchemaVersion("1.0.0")
COMMIT = CommitId("sha256:" + "a" * 64)
SNAPSHOT = StableId("snapshot.audit")
PROJECT = ProjectId("project.audit")


class _Snapshots:
    def __init__(self, *, status: DerivedBuildStatus = DerivedBuildStatus.EXACT, snapshot=SNAPSHOT):
        self._snapshot = DerivedSnapshotLite(
            snapshot_id=snapshot,
            source_commit=COMMIT,
            anchor_build_id=StableId("anchor.audit"),
            anchor_index_version="anchor-v1",
            grounded_index_version="grounded-v1",
            embedding_profile="offline-v1",
            fusion_profile="rrf-v1",
            build_status=status,
            published_at=datetime.now(UTC),
        )

    def get_for_commit(self, _commit: CommitId) -> DerivedSnapshotLite:
        return self._snapshot


class _Index:
    def __init__(self, documents: dict[str, tuple[dict[str, Any], ...]]) -> None:
        self._documents = documents

    def search(self, index: str, query: dict[str, Any], *, size: int) -> tuple[dict[str, Any], ...]:
        del query, size
        return self._documents.get(index, ())


def _text_root(indexes: tuple[int, ...]) -> TextRootDocument:
    return TextRootDocument(
        root_hash=ArtifactId("sha256:" + "b" * 64),
        schema_version=VERSION,
        chapters=tuple(
            ChapterDocument(
                chapter_id=StableId(f"chapter.{index}"),
                chapter_index=index,
                scenes=(SceneDocument(scene_id=StableId(f"scene.{index}"), scene_index=0),),
            )
            for index in indexes
        ),
    )


def _session_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session, session.begin():
        for chapter in (1, 2, 3):
            session.add(
                R1RecordRow(
                    row_id=f"r1.{chapter}",
                    project_id=PROJECT.root,
                    source_commit=COMMIT.root,
                    record_kind="state",
                    record_id=f"record.{chapter}",
                    narrative_start=chapter,
                    narrative_end=chapter,
                    access_scope="writer_safe",
                    record_json={"chapter": chapter},
                )
            )
    return factory


def test_audit_reports_missing_chapter_and_blocks_exact() -> None:
    auditor = MemoryProjectionAuditor(
        session_factory=_session_factory(),
        snapshots=_Snapshots(),  # type: ignore[arg-type]
        index=_Index(  # type: ignore[arg-type]
            {
                "grounded": tuple({"_source": {"narrative_start": chapter}} for chapter in (1, 3)),
                "anchor": tuple({"_source": {"narrative_start": chapter}} for chapter in (1, 2, 3)),
            }
        ),
        grounded_index="grounded",
        anchor_index="anchor",
    )

    report = auditor.audit(
        project_id=PROJECT,
        commit_id=COMMIT,
        snapshot_id=SNAPSHOT,
        text_root=_text_root((1, 2, 3)),
        canary_queries=(("chapter48_history", {"match_all": {}}),),
    )

    assert report.canonical_chapter_count == 3
    assert report.missing_chapters == (2,)
    assert report.exact is False
    assert "MISSING_CHAPTERS:2" in report.diagnostics
    grounded = next(channel for channel in report.channels if channel.channel == "grounded")
    assert grounded.missing_chapters == (2,)
    assert report.canary_query_results == ("chapter48_history:hit",)


def test_audit_is_exact_when_all_channels_and_snapshot_agree() -> None:
    documents = {
        name: tuple({"narrative_start": chapter} for chapter in (1, 2, 3))
        for name in ("grounded", "anchor")
    }
    auditor = MemoryProjectionAuditor(
        session_factory=_session_factory(),
        snapshots=_Snapshots(),  # type: ignore[arg-type]
        index=_Index(documents),  # type: ignore[arg-type]
        grounded_index="grounded",
        anchor_index="anchor",
    )

    report = auditor.audit(
        project_id=PROJECT,
        commit_id=COMMIT,
        snapshot_id=SNAPSHOT,
        text_root=_text_root((1, 2, 3)),
    )

    assert report.exact is True
    assert report.missing_chapters == ()
    assert report.stale_commit_or_snapshot_rows == 0


def test_audit_marks_stale_snapshot_as_not_exact() -> None:
    documents = {
        name: tuple({"narrative_start": chapter} for chapter in (1, 2, 3))
        for name in ("grounded", "anchor")
    }
    auditor = MemoryProjectionAuditor(
        session_factory=_session_factory(),
        snapshots=_Snapshots(snapshot=StableId("snapshot.other")),  # type: ignore[arg-type]
        index=_Index(documents),  # type: ignore[arg-type]
        grounded_index="grounded",
        anchor_index="anchor",
    )

    report = auditor.audit(
        project_id=PROJECT,
        commit_id=COMMIT,
        snapshot_id=SNAPSHOT,
        text_root=_text_root((1, 2, 3)),
    )

    assert report.exact is False
    assert report.stale_commit_or_snapshot_rows == 1
    assert "SNAPSHOT_ID_MISMATCH" in report.diagnostics
