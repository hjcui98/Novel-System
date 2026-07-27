from __future__ import annotations

import pytest

from novel_agent.domain.benchmark import ChapterDocument, PreludeDocument, TextRootDocument
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.services.text_timeline import SequentialTextRootService, TextTimelineError

VERSION = SchemaVersion("2.0.0")


def test_text_timeline_accepts_one_prelude_then_contiguous_chapters() -> None:
    service = SequentialTextRootService()
    empty = service.empty(VERSION)
    prelude = PreludeDocument(prelude_id=StableId("prelude.one"), scenes=())
    with_prelude, prelude_receipt = service.append(
        empty,
        StableId("source.prelude"),
        prelude,
    )
    assert prelude_receipt.narrative_index == 0

    chapter = ChapterDocument(
        chapter_id=StableId("chapter.1"),
        chapter_index=1,
        scenes=(),
    )
    advanced, chapter_receipt = service.append(
        with_prelude,
        StableId("source.chapter.1"),
        chapter,
    )
    assert chapter_receipt.narrative_index == 1
    assert advanced.chapters == (chapter,)

    with pytest.raises(TextTimelineError, match="Prelude can only"):
        service.append(advanced, StableId("source.prelude.2"), prelude)
    with pytest.raises(TextTimelineError, match="must be continuous"):
        service.append(
            advanced,
            StableId("source.chapter.3"),
            chapter.model_copy(
                update={
                    "chapter_id": StableId("chapter.3"),
                    "chapter_index": 3,
                }
            ),
        )


def test_text_timeline_backfills_missing_prelude_without_rewriting_chapters() -> None:
    service = SequentialTextRootService()
    chapter = ChapterDocument(
        chapter_id=StableId("chapter.1"),
        chapter_index=1,
        scenes=(),
    )
    current = TextRootDocument(
        root_hash=ArtifactId("sha256:" + "1" * 64),
        schema_version=VERSION,
        chapters=(chapter,),
    )
    prelude = PreludeDocument(prelude_id=StableId("prelude.one"), scenes=())

    repaired, receipt = service.backfill_missing_prelude(
        current,
        StableId("source.prelude"),
        prelude,
    )

    assert repaired.prelude == prelude
    assert repaired.chapters == current.chapters
    assert repaired.root_hash == receipt.resulting_text_root
    assert receipt.previous_text_root == current.root_hash
    assert receipt.narrative_index == 0

    with pytest.raises(TextTimelineError, match="requires a missing prelude"):
        service.backfill_missing_prelude(
            repaired,
            StableId("source.prelude"),
            prelude,
        )
    with pytest.raises(TextTimelineError, match="existing chapter history"):
        service.backfill_missing_prelude(
            service.empty(VERSION),
            StableId("source.prelude"),
            prelude,
        )
