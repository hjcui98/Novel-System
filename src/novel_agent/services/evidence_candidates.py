"""Trusted evidence candidate generation and quote resolution (WP4)."""

from __future__ import annotations

import re
from collections.abc import Mapping

from novel_agent.domain.benchmark import ChapterDocument, TextRootDocument
from novel_agent.domain.changes import (
    EvidenceCandidate,
    EvidenceCandidateView,
    EvidenceQuoteSelection,
)
from novel_agent.domain.ids import StableId
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.curation import Stage1Curator

_SENTENCE_SPLIT = re.compile(r"(?<=[\u3002\uff01\uff1f!?\uff1b;])\s*")
_DIALOGUE_SPLIT = re.compile(r'(?<=[\u201d"\u300f])')
DEFAULT_MAX_CANDIDATE_CHARS = 240
DEFAULT_TARGET_MIN = 40
DEFAULT_TARGET_MAX = 160
DEFAULT_MAX_CHAPTER_CANDIDATES = 128


class EvidenceCandidateGenerator:
    """Split revealed chapter text into content-addressed span candidates."""

    def __init__(
        self,
        *,
        max_candidate_chars: int = DEFAULT_MAX_CANDIDATE_CHARS,
        target_min_chars: int = DEFAULT_TARGET_MIN,
        target_max_chars: int = DEFAULT_TARGET_MAX,
        max_chapter_candidates: int = DEFAULT_MAX_CHAPTER_CANDIDATES,
    ) -> None:
        if max_candidate_chars < 1:
            raise ValueError("max_candidate_chars must be positive")
        self._max_chars = max_candidate_chars
        self._target_min = target_min_chars
        self._target_max = target_max_chars
        self._max_chapter = max_chapter_candidates

    def generate(
        self,
        text_root: TextRootDocument,
        chapter_index: int,
    ) -> tuple[EvidenceCandidate, ...]:
        chapter = Stage1Curator._chapter(text_root, chapter_index)
        candidates: list[EvidenceCandidate] = []
        for scene_index, scene in enumerate(chapter.scenes):
            for block in scene.blocks:
                candidates.extend(
                    self._block_candidates(
                        block_id=block.block_id,
                        chapter_index=chapter_index,
                        scene_index=scene_index,
                        text=block.text,
                    )
                )
                if len(candidates) >= self._max_chapter:
                    return tuple(candidates[: self._max_chapter])
        return tuple(candidates)

    def model_views(
        self,
        candidates: tuple[EvidenceCandidate, ...],
    ) -> tuple[EvidenceCandidateView, ...]:
        return tuple(
            EvidenceCandidateView(
                candidate_id=item.candidate_id,
                block_id=item.block_id,
                text=item.text,
            )
            for item in candidates
        )

    def index_by_id(
        self,
        candidates: tuple[EvidenceCandidate, ...],
    ) -> dict[StableId, EvidenceCandidate]:
        indexed = {item.candidate_id: item for item in candidates}
        if len(indexed) != len(candidates):
            raise ValueError("evidence candidate ids must be unique")
        return indexed

    def resolve_quote(
        self,
        selection: EvidenceQuoteSelection,
        chapter: ChapterDocument,
    ) -> EvidenceCandidate:
        blocks = {block.block_id: block for scene in chapter.scenes for block in scene.blocks}
        block = blocks.get(selection.block_id)
        if block is None:
            raise ValueError("quote selection references unknown block")
        text = block.text
        quote = selection.exact_quote
        matches: list[int] = []
        start = 0
        while True:
            idx = text.find(quote, start)
            if idx < 0:
                break
            matches.append(idx)
            start = idx + 1
        if selection.left_context or selection.right_context:
            filtered: list[int] = []
            for idx in matches:
                left_ok = True
                right_ok = True
                if selection.left_context:
                    left = text[max(0, idx - len(selection.left_context)) : idx]
                    left_ok = left.endswith(selection.left_context)
                if selection.right_context:
                    end = idx + len(quote)
                    right = text[end : end + len(selection.right_context)]
                    right_ok = right.startswith(selection.right_context)
                if left_ok and right_ok:
                    filtered.append(idx)
            matches = filtered
        if selection.occurrence is not None:
            if selection.occurrence >= len(matches):
                raise ValueError("quote occurrence is out of range")
            matches = [matches[selection.occurrence]]
        if len(matches) != 1:
            raise ValueError("quote must resolve to exactly one span")
        start_idx = matches[0]
        end_idx = start_idx + len(quote)
        return self._make_candidate(
            block_id=selection.block_id,
            chapter_index=chapter.chapter_index,
            scene_index=0,
            text=quote,
            start=start_idx,
            end=end_idx,
        )

    def _block_candidates(
        self,
        *,
        block_id: StableId,
        chapter_index: int,
        scene_index: int,
        text: str,
    ) -> list[EvidenceCandidate]:
        if not text:
            return []
        segments = self._segment(text)
        candidates: list[EvidenceCandidate] = []
        cursor = 0
        for segment in segments:
            idx = text.find(segment, cursor)
            if idx < 0:  # pragma: no cover - defensive: segments are substrings of text
                idx = cursor
            end = idx + len(segment)
            for start, stop in self._window_spans(idx, end, text):
                span_text = text[start:stop]
                if not span_text.strip():
                    continue
                candidates.append(
                    self._make_candidate(
                        block_id=block_id,
                        chapter_index=chapter_index,
                        scene_index=scene_index,
                        text=span_text,
                        start=start,
                        end=stop,
                    )
                )
            cursor = end
        if not candidates and text.strip():  # pragma: no cover - defensive fallback
            for start, stop in self._window_spans(0, len(text), text):
                candidates.append(
                    self._make_candidate(
                        block_id=block_id,
                        chapter_index=chapter_index,
                        scene_index=scene_index,
                        text=text[start:stop],
                        start=start,
                        end=stop,
                    )
                )
        return candidates

    def _segment(self, text: str) -> list[str]:
        parts = [part for part in _SENTENCE_SPLIT.split(text) if part]
        if len(parts) <= 1:
            parts = [part for part in _DIALOGUE_SPLIT.split(text) if part]
        if not parts:  # pragma: no cover - defensive: caller rejects empty text
            return [text]
        return parts

    def _window_spans(self, start: int, end: int, text: str) -> list[tuple[int, int]]:
        length = end - start
        if length <= self._max_chars:
            return [(start, end)]
        spans: list[tuple[int, int]] = []
        window = min(self._target_max, self._max_chars)
        step = max(1, window // 2)
        pos = start
        while pos < end:  # pragma: no branch - loop always exits via break
            stop = min(pos + window, end)
            spans.append((pos, stop))
            if stop >= end:
                break
            pos += step
        return spans

    @staticmethod
    def _make_candidate(
        *,
        block_id: StableId,
        chapter_index: int,
        scene_index: int,
        text: str,
        start: int,
        end: int,
    ) -> EvidenceCandidate:
        payload = {
            "block_id": block_id.root,
            "chapter_index": chapter_index,
            "scene_index": scene_index,
            "start": start,
            "end": end,
            "text": text,
        }
        digest = sha256_id(canonical_json_bytes(payload))
        return EvidenceCandidate(
            candidate_id=StableId(f"evidence-candidate.{digest.root.removeprefix('sha256:')[:24]}"),
            block_id=block_id,
            chapter_index=chapter_index,
            scene_index=scene_index,
            text=text,
            start=start,
            end=end,
            content_hash=digest,
        )


def bind_candidate_selection(
    candidate_ids: tuple[StableId, ...],
    catalog: Mapping[StableId, EvidenceCandidate],
) -> tuple[EvidenceCandidate, ...]:
    bound: list[EvidenceCandidate] = []
    for candidate_id in candidate_ids:
        item = catalog.get(candidate_id)
        if item is None:
            raise KeyError(candidate_id.root)
        bound.append(item)
    return tuple(bound)
