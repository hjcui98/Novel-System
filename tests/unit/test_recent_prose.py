"""Focused checks for the deterministic recent-prose projection."""

from __future__ import annotations

from pathlib import Path

import pytest

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.generation import RecentProseContext
from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.recent_prose import (
    RecentProseAssembler,
    RecentProseAssemblyError,
)
from tests.fixtures.stage1_synthetic import make_synthetic_bundle

VERSION = SchemaVersion("1.0.0")
BASE = CommitId("sha256:" + "1" * 64)
SNAPSHOT = StableId("snapshot.recent-prose")


def test_recent_prose_keeps_previous_chapter_full_and_earlier_trails(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    text_root = next(
        item for item in make_synthetic_bundle().text_roots if len(item.chapters) == 20
    )

    context, context_ref = RecentProseAssembler(
        artifacts,
        VERSION,
        earlier_chapter_count=2,
        trail_characters=40,
    ).assemble(
        text_root=text_root,
        base_commit=BASE,
        snapshot_id=SNAPSHOT,
        target_chapter=21,
    )

    assert context.previous_chapter is not None
    assert context.previous_chapter.chapter_index == 20
    assert tuple(item.chapter_index for item in context.earlier_chapters) == (19, 18)
    previous_text = artifacts.read_verified(context.previous_chapter.full_text_artifact).decode()
    expected_text = "\n\n".join(
        block.text
        for scene in text_root.chapters[-1].scenes
        for block in scene.blocks
        if block.text.strip()
    )
    assert previous_text == expected_text
    assert context.previous_chapter.full_text_characters == len(expected_text)
    assert all(len(item.compact_trail) <= 40 for item in context.earlier_chapters)
    assert RecentProseContext.model_validate_json(artifacts.read_verified(context_ref)) == context


def test_recent_prose_requires_text_root_to_end_at_target_predecessor(tmp_path: Path) -> None:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    empty = TextRootDocument(
        root_hash=ArtifactId("sha256:" + "2" * 64),
        schema_version=VERSION,
        chapters=(),
    )

    with pytest.raises(RecentProseAssemblyError, match="ends at chapter 0, expected 20"):
        RecentProseAssembler(artifacts, VERSION).assemble(
            text_root=empty,
            base_commit=BASE,
            snapshot_id=SNAPSHOT,
            target_chapter=21,
        )
