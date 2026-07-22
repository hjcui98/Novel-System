"""Rule-driven deterministic Curator with exact evidence binding."""

from __future__ import annotations

import hashlib

from novel_agent.domain.artifacts import ArtifactRef, RootKind
from novel_agent.domain.benchmark import ChapterDocument, TextRootDocument
from novel_agent.domain.changes import ChangeOperation, ExtractionRule, ObservedChangeSet
from novel_agent.domain.ids import CommitId, SchemaVersion, StableId
from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextBlock, TextSpanRef
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes, quote_hash


class Stage1Curator:
    def extract(
        self,
        text_root: TextRootDocument,
        chapter_index: int,
        base_commit: CommitId,
        rules: tuple[ExtractionRule, ...],
    ) -> ObservedChangeSet:
        chapter = self._chapter(text_root, chapter_index)
        operations: list[ChangeOperation] = []
        for rule in rules:
            match = self._first_match(chapter, rule.phrase)
            if match is None:
                continue
            block, start = match
            evidence = EvidenceRef(
                evidence_id=StableId(
                    f"evidence.curated.{self._digest(block.block_id.root, str(start), rule.phrase)}"
                ),
                root_hash=text_root.root_hash,
                object_hash=sha256_id(block.text.encode("utf-8")),
                chapter_id=block.chapter_id,
                scene_id=block.scene_id,
                span=TextSpanRef(
                    block_id=block.block_id,
                    start=start,
                    end=start + len(rule.phrase),
                ),
                quote_hash=quote_hash(rule.phrase),
                support_status=EvidenceSupportStatus.CURRENT,
                resolved_at_commit=base_commit,
            )
            record = dict(rule.record)
            if rule.record_kind.value != "entity":
                record["evidence_refs"] = [evidence.model_dump(mode="json")]
            operations.append(
                ChangeOperation(
                    operation_id=StableId(
                        f"change.{self._digest(rule.rule_id.root, evidence.evidence_id.root)}"
                    ),
                    root_kind=RootKind.WORLD,
                    operation=rule.operation,
                    target_id=rule.target_id,
                    payload={"record_type": rule.record_kind.value, "record": record},
                    evidence_refs=(evidence,),
                )
            )
        source_bytes = canonical_json_bytes(chapter.model_dump(mode="json"))
        return ObservedChangeSet(
            change_set_id=StableId(
                f"changes.{self._digest(base_commit.root, chapter.chapter_id.root)}"
            ),
            base_commit=base_commit,
            source_artifact=ArtifactRef(
                artifact_id=sha256_id(source_bytes),
                media_type="application/vnd.novel-agent.chapter+json",
                byte_length=len(source_bytes),
                schema_version=SchemaVersion("0.1.0"),
            ),
            operations=tuple(operations),
        )

    @staticmethod
    def _chapter(root: TextRootDocument, chapter_index: int) -> ChapterDocument:
        chapter = next(
            (item for item in root.chapters if item.chapter_index == chapter_index), None
        )
        if chapter is None:
            raise LookupError(f"chapter does not exist: {chapter_index}")
        return chapter

    @staticmethod
    def _first_match(chapter: ChapterDocument, phrase: str) -> tuple[TextBlock, int] | None:
        for scene in chapter.scenes:
            for block in scene.blocks:
                start = block.text.find(phrase)
                if start >= 0:
                    return block, start
        return None

    @staticmethod
    def _digest(*parts: str) -> str:
        return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:24]
