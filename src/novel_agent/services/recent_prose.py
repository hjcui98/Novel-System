"""Project the deterministic near-prose seam from one accepted TextRoot."""

from __future__ import annotations

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import ChapterDocument, TextRootDocument
from novel_agent.domain.generation import RecentChapterProse, RecentProseContext
from novel_agent.domain.ids import CommitId, SchemaVersion, StableId
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes

RECENT_CHAPTER_TEXT_MEDIA_TYPE = "application/vnd.novel-agent.recent-chapter-text+plain"
RECENT_PROSE_CONTEXT_MEDIA_TYPE = "application/vnd.novel-agent.recent-prose-context+json"


class RecentProseAssemblyError(ValueError):
    """The accepted TextRoot cannot provide the required narrative seam."""


class RecentProseAssembler:
    """Create one full previous chapter plus a compact trail of earlier chapters."""

    def __init__(
        self,
        artifacts: ArtifactRepository,
        schema_version: SchemaVersion,
        *,
        earlier_chapter_count: int = 2,
        trail_characters: int = 1_200,
    ) -> None:
        if earlier_chapter_count < 0:
            raise ValueError("earlier chapter count must be non-negative")
        if trail_characters < 1:
            raise ValueError("recent prose trail length must be positive")
        self._artifacts = artifacts
        self._schema_version = schema_version
        self._earlier_count = earlier_chapter_count
        self._trail_characters = trail_characters

    def assemble(
        self,
        *,
        text_root: TextRootDocument,
        base_commit: CommitId,
        snapshot_id: StableId,
        target_chapter: int,
    ) -> tuple[RecentProseContext, ArtifactRef]:
        if target_chapter < 1:
            raise RecentProseAssemblyError("target chapter must be positive")
        checkpoint = target_chapter - 1
        latest = text_root.chapters[-1].chapter_index if text_root.chapters else 0
        if latest != checkpoint:
            raise RecentProseAssemblyError(
                f"accepted TextRoot ends at chapter {latest}, expected {checkpoint}"
            )
        if checkpoint == 0:
            context = RecentProseContext(
                context_id=self._context_id(base_commit, snapshot_id, checkpoint),
                base_commit=base_commit,
                snapshot_id=snapshot_id,
                checkpoint_chapter=0,
            )
            return context, self._store_context(context)

        selected = tuple(reversed(text_root.chapters[-(self._earlier_count + 1) :]))
        recent = tuple(self._chapter(item) for item in selected)
        context = RecentProseContext(
            context_id=self._context_id(base_commit, snapshot_id, checkpoint),
            base_commit=base_commit,
            snapshot_id=snapshot_id,
            checkpoint_chapter=checkpoint,
            previous_chapter=recent[0],
            earlier_chapters=recent[1:],
        )
        return context, self._store_context(context)

    def _chapter(self, chapter: ChapterDocument) -> RecentChapterProse:
        text = "\n\n".join(
            block.text for scene in chapter.scenes for block in scene.blocks if block.text.strip()
        )
        if not text.strip():
            raise RecentProseAssemblyError(
                f"accepted chapter {chapter.chapter_index} has no prose blocks"
            )
        artifact = self._artifacts.put(
            text.encode("utf-8"),
            RECENT_CHAPTER_TEXT_MEDIA_TYPE,
            self._schema_version,
        )
        return RecentChapterProse(
            chapter_id=chapter.chapter_id,
            chapter_index=chapter.chapter_index,
            title=chapter.title,
            full_text_artifact=artifact,
            full_text_characters=len(text),
            compact_trail=text[-self._trail_characters :],
        )

    def _store_context(self, context: RecentProseContext) -> ArtifactRef:
        return self._artifacts.put(
            canonical_json_bytes(context.model_dump(mode="json")),
            RECENT_PROSE_CONTEXT_MEDIA_TYPE,
            self._schema_version,
        )

    @staticmethod
    def _context_id(
        base_commit: CommitId,
        snapshot_id: StableId,
        checkpoint: int,
    ) -> StableId:
        commit_suffix = base_commit.root.removeprefix("sha256:")[-24:]
        snapshot_suffix = snapshot_id.root[-32:]
        return StableId(f"recent-prose.c{checkpoint}.{commit_suffix}.{snapshot_suffix}"[:128])


__all__ = [
    "RECENT_CHAPTER_TEXT_MEDIA_TYPE",
    "RECENT_PROSE_CONTEXT_MEDIA_TYPE",
    "RecentProseAssembler",
    "RecentProseAssemblyError",
]
