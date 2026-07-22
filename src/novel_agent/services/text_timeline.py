"""Pure, content-addressed TextRoot Genesis and sequential narrative evolution."""

from __future__ import annotations

from novel_agent.domain.benchmark import ChapterDocument, PreludeDocument, TextRootDocument
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.stage2 import TextRootAdvanceReceipt
from novel_agent.services.content_addressing import content_id, text_root_content_id


class TextTimelineError(ValueError):
    pass


class SequentialTextRootService:
    def empty(self, schema_version: SchemaVersion) -> TextRootDocument:
        provisional = TextRootDocument(
            root_hash=ArtifactId("sha256:" + "0" * 64),
            schema_version=schema_version,
            prelude=None,
            chapters=(),
        )
        return provisional.model_copy(update={"root_hash": text_root_content_id(provisional)})

    def append(
        self,
        current: TextRootDocument,
        source_id: StableId,
        document: PreludeDocument | ChapterDocument,
    ) -> tuple[TextRootDocument, TextRootAdvanceReceipt]:
        previous = current.root_hash
        if isinstance(document, PreludeDocument):
            if current.prelude is not None or current.chapters:
                raise TextTimelineError("Prelude can only be appended to an empty TextRoot")
            narrative_index = 0
            provisional = current.model_copy(
                update={
                    "root_hash": ArtifactId("sha256:" + "0" * 64),
                    "prelude": document,
                }
            )
        else:
            expected = current.chapters[-1].chapter_index + 1 if current.chapters else 1
            if document.chapter_index != expected:
                raise TextTimelineError(
                    f"chapter append must be continuous: expected {expected}, "
                    f"received {document.chapter_index}"
                )
            narrative_index = document.chapter_index
            provisional = current.model_copy(
                update={
                    "root_hash": ArtifactId("sha256:" + "0" * 64),
                    "chapters": (*current.chapters, document),
                }
            )
        resulting = provisional.model_copy(update={"root_hash": text_root_content_id(provisional)})
        document_hash = content_id(document.model_dump(mode="json"))
        receipt_hash = content_id(
            {
                "source_id": source_id.root,
                "narrative_index": narrative_index,
                "previous_text_root": previous.root,
                "resulting_text_root": resulting.root_hash.root,
                "document_hash": document_hash.root,
            }
        )
        receipt = TextRootAdvanceReceipt(
            receipt_id=StableId(
                f"text-root-advance.{receipt_hash.root.removeprefix('sha256:')[:24]}"
            ),
            source_id=source_id,
            narrative_index=narrative_index,
            previous_text_root=previous,
            resulting_text_root=resulting.root_hash,
            document_hash=document_hash,
        )
        return resulting, receipt
