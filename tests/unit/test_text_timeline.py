from __future__ import annotations

import pytest

from novel_agent.domain.benchmark import ChapterDocument, PreludeDocument
from novel_agent.domain.ids import SchemaVersion, StableId
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
