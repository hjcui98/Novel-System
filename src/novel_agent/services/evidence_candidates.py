"""Trusted evidence candidate generation and quote resolution (WP4)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from difflib import SequenceMatcher
from itertools import pairwise

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
# The semantic-quote contract (Grounder principle: the model copies natural
# language fragments and the host binds them to content-addressed ids) makes
# catalog size independent of model memorization, so the catalog must cover
# the whole chapter: a small catalog leaves later spans unbindable and the
# model's legitimate quotes get rejected.  512 covers a full-length chapter
# (measured: a 19.6k-character chapter yields ~180 candidates; 128 covered
# only a quarter and dropped legitimate quotes).
DEFAULT_MAX_CHAPTER_CANDIDATES = 512


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

    @staticmethod
    def resolve_evidence_quotes(
        quotes: tuple[str, ...],
        candidates: tuple[EvidenceCandidate, ...],
    ) -> tuple[EvidenceCandidate, ...]:
        """Deterministically bind model-emitted semantic quotes to candidates.

        Grounder principle: the model copies natural-language fragments; this
        host-side resolver finds the single candidate whose text contains the
        fragment.  A quote with no match is unresolved, a quote matching
        several candidates is ambiguous; both are rejections the model can
        repair by quoting a longer fragment.
        """

        resolved: list[EvidenceCandidate] = []
        for quote in quotes:
            normalized = EvidenceCandidateGenerator._semantic_span(quote)
            if len(normalized) < 2:
                raise ValueError(f"evidence quote too short to resolve: {quote[:40]!r}")
            # A dialogue quote spans the split boundary exactly (cue + content
            # candidates joined reproduce the quote); bind to the content
            # candidate before falling back to substring matching, otherwise
            # both cue and content candidates match as substrings and the
            # quote looks ambiguous.
            joined_pairs = tuple(
                (first, second)
                for first, second in pairwise(candidates)
                if (
                    EvidenceCandidateGenerator._semantic_span(first.text)
                    + EvidenceCandidateGenerator._semantic_span(second.text)
                )
                == normalized
            )
            if len(joined_pairs) > 1:
                raise ValueError(
                    f"evidence quote ambiguous ({len(joined_pairs)} candidate pairs): "
                    f"{quote[:60]!r}"
                )
            if len(joined_pairs) == 1:
                resolved.append(joined_pairs[0][1])
                continue
            exact = tuple(
                candidate
                for candidate in candidates
                if normalized in EvidenceCandidateGenerator._semantic_span(candidate.text)
            )
            if len(exact) > 1:
                raise ValueError(
                    f"evidence quote ambiguous ({len(exact)} candidates): {quote[:60]!r}"
                )
            if len(exact) == 1:
                resolved.append(exact[0])
                continue
            # The model may quote a longer original span that contains the
            # pre-split candidate (e.g. dialogue cues that the splitter cut
            # at a quote boundary, or a trailing/leading quote mark dropped by
            # the model).  Punctuation-insensitive matching binds the quote to
            # the single candidate fully contained in it; this stays
            # fail-closed because a quote containing several candidates
            # remains ambiguous.
            contained = tuple(
                candidate
                for candidate in candidates
                if EvidenceCandidateGenerator._semantic_span(candidate.text) in normalized
            )
            if len(contained) > 1:
                raise ValueError(
                    f"evidence quote ambiguous ({len(contained)} candidates): {quote[:60]!r}"
                )
            if len(contained) == 1:
                resolved.append(contained[0])
                continue
            # Quote spans two candidates without reproducing either exactly
            # (e.g. slight model paraphrasing); bind to the single adjacent
            # pair whose joined span contains the quote.
            pair_bound = tuple(
                second
                for first, second in pairwise(candidates)
                if (
                    EvidenceCandidateGenerator._semantic_span(first.text)
                    + EvidenceCandidateGenerator._semantic_span(second.text)
                )
                in normalized
                or normalized
                in (
                    EvidenceCandidateGenerator._semantic_span(first.text)
                    + EvidenceCandidateGenerator._semantic_span(second.text)
                )
            )
            # Adjacent pairs share only the boundary span, so more than one
            # match cannot occur for a quote of at least 8 characters.
            assert len(pair_bound) <= 1
            if len(pair_bound) == 1:
                resolved.append(pair_bound[0])
                continue
            raise ValueError(
                f"evidence quote unresolved against the chapter catalog: {quote[:60]!r}"
            )
        return tuple(resolved)

    @staticmethod
    def _semantic_span(text: str) -> str:
        """Punctuation-insensitive semantic span for quote binding."""
        return "".join(char for char in text.casefold() if char.isalnum())

    @staticmethod
    def _similarity_ratio(quote: str, candidate: EvidenceCandidate) -> float:
        """Deterministic similarity between a quote and one catalog candidate."""

        normalized = EvidenceCandidateGenerator._semantic_span(quote)
        span = EvidenceCandidateGenerator._semantic_span(candidate.text)
        if not span:
            return 0.0
        return max(
            SequenceMatcher(None, normalized, span).ratio(),
            SequenceMatcher(None, normalized, span[: len(normalized)]).ratio()
            if len(span) >= len(normalized)
            else 0.0,
            SequenceMatcher(None, normalized[: len(span)], span).ratio()
            if len(normalized) >= len(span)
            else 0.0,
        )

    @staticmethod
    def closest_candidate(
        quote: str,
        candidates: tuple[EvidenceCandidate, ...],
        *,
        ratio_threshold: float = 0.6,
    ) -> EvidenceCandidate | None:
        """Best-effort nearest candidate for rejection feedback.

        The model sometimes paraphrases a dialogue cue while keeping the
        content; the bound quote then fails exact binding.  This similarity
        match is used only to tell the model which catalog text to copy
        verbatim on the repair round; it never auto-binds evidence.
        """

        best: tuple[float, EvidenceCandidate | None] = (0.0, None)
        for candidate in candidates:
            ratio = EvidenceCandidateGenerator._similarity_ratio(quote, candidate)
            if ratio > best[0]:
                best = (ratio, candidate)
        if best[0] >= ratio_threshold and best[1] is not None:
            return best[1]
        return None

    def copyable_literal_for(
        self,
        quote: str,
        candidates: tuple[EvidenceCandidate, ...],
        *,
        max_chars: int,
    ) -> str | None:
        """Best similarity-ranked catalog literal the strict resolver accepts.

        Similarity is feedback-only: the returned string is advertised to the
        model only after the exact literal, as emitted, passes
        ``resolve_evidence_quotes`` and binds to exactly one candidate.  Only
        literals no longer than ``max_chars`` are considered so callers can
        format feedback without ever truncating a validated literal.  Returns
        ``None`` when no bounded catalog literal resolves, so callers must fall
        back to truthful generic longer-fragment guidance.
        """

        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        ranked = sorted(
            candidates,
            key=lambda candidate: EvidenceCandidateGenerator._similarity_ratio(quote, candidate),
            reverse=True,
        )
        for candidate in ranked:
            literal = candidate.text
            if len(literal) > max_chars:
                continue
            try:
                # The strict resolver either raises (unresolved/ambiguous) or
                # binds the quote to exactly one candidate; a successful return
                # is the resolver-valid proof required before this literal is
                # advertised as copyable.
                self.resolve_evidence_quotes((literal,), candidates)
            except ValueError:
                continue
            return literal
        return None

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
