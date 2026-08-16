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
_FEEDBACK_LITERAL_SPLIT = re.compile(
    r"(?<=[\uff0c,\u3002\uff01\uff1f!?\uff1b;\uff1a:])\s*|(?<=\u2014\u2014)"
)
_MIN_DERIVED_LITERAL_SEMANTIC_CHARS = 8
DEFAULT_MAX_CANDIDATE_CHARS = 240
DEFAULT_TARGET_MIN = 40
DEFAULT_TARGET_MAX = 160
# The active catalog covers the complete chapter. Callers may set an explicit
# bound for a deliberately bounded fixture or workflow, but the default must
# not silently make later source units unbindable.
DEFAULT_MAX_CHAPTER_CANDIDATES: int | None = None


class EvidenceCandidateGenerator:
    """Split revealed chapter text into content-addressed span candidates."""

    def __init__(
        self,
        *,
        max_candidate_chars: int = DEFAULT_MAX_CANDIDATE_CHARS,
        target_min_chars: int = DEFAULT_TARGET_MIN,
        target_max_chars: int = DEFAULT_TARGET_MAX,
        max_chapter_candidates: int | None = DEFAULT_MAX_CHAPTER_CANDIDATES,
    ) -> None:
        if max_candidate_chars < 1:
            raise ValueError("max_candidate_chars must be positive")
        self._max_chars = max_candidate_chars
        self._target_min = target_min_chars
        self._target_max = target_max_chars
        if max_chapter_candidates is not None and max_chapter_candidates < 1:
            raise ValueError("max_chapter_candidates must be positive when set")
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
                if self._max_chapter is not None and len(candidates) >= self._max_chapter:
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

    @classmethod
    def resolve_exact_evidence_quotes(
        cls,
        quotes: tuple[str, ...],
        candidates: tuple[EvidenceCandidate, ...],
        chapter: ChapterDocument,
    ) -> tuple[EvidenceCandidate, ...]:
        """Bind verbatim quotes to unique physical spans covered by this source unit."""

        blocks = {
            block.block_id: (scene.scene_index, block.text)
            for scene in chapter.scenes
            for block in scene.blocks
        }
        covered: dict[StableId, list[tuple[int, int]]] = {}
        for candidate in candidates:
            covered.setdefault(candidate.block_id, []).append((candidate.start, candidate.end))
        merged_ranges: dict[StableId, tuple[tuple[int, int], ...]] = {}
        for block_id, spans in covered.items():
            ranges: list[tuple[int, int]] = []
            for start, end in sorted(spans):
                if ranges and start <= ranges[-1][1]:
                    ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
                else:
                    ranges.append((start, end))
            merged_ranges[block_id] = tuple(ranges)

        resolved: list[EvidenceCandidate] = []
        for quote in quotes:
            if len(cls._semantic_span(quote)) < 2:
                raise ValueError(f"evidence quote too short to resolve: {quote[:40]!r}")
            matches: list[tuple[StableId, int, int, int]] = []
            for block_id, covered_ranges in merged_ranges.items():
                scene_index, text = blocks[block_id]
                start = 0
                while True:
                    offset = text.find(quote, start)
                    if offset < 0:
                        break
                    end = offset + len(quote)
                    if any(
                        range_start <= offset and end <= range_end
                        for range_start, range_end in covered_ranges
                    ):
                        matches.append((block_id, scene_index, offset, end))
                    start = offset + 1
            unique = tuple(dict.fromkeys(matches))
            if len(unique) > 1:
                raise ValueError(
                    f"evidence quote ambiguous ({len(unique)} physical spans): {quote[:60]!r}"
                )
            if not unique:
                raise ValueError(
                    f"evidence quote unresolved against the source unit: {quote[:60]!r}"
                )
            block_id, scene_index, start, end = unique[0]
            resolved.append(
                cls._make_candidate(
                    block_id=block_id,
                    chapter_index=chapter.chapter_index,
                    scene_index=scene_index,
                    text=quote,
                    start=start,
                    end=end,
                )
            )
        return tuple(resolved)

    @staticmethod
    def _semantic_span(text: str) -> str:
        """Punctuation-insensitive semantic span for quote binding."""
        return "".join(char for char in text.casefold() if char.isalnum())

    # Round-18 repair: at most one LEADING closing dialogue-boundary mark may
    # be ignored by the layout-equivalence fallback (the model dropped it in
    # one chapter-12 variant and restored it in the others).
    _CLOSING_DIALOGUE_MARKS: tuple[str, ...] = ("」", "』", "”", "\u2019", '"')

    @staticmethod
    def _layout_normalize(text: str) -> str:
        """Remove CR/LF plus their adjacent indentation (layout equivalence).

        Only whitespace immediately adjacent to a line break is removed; all
        other characters (including internal punctuation and ordinary spaces)
        are preserved, so character/punctuation/word-space changes stay
        distinct and the fallback remains narrow.
        """
        return re.sub(r"[ \t\u3000]*\r?\n[ \t\u3000]*", "", text)

    @classmethod
    def resolve_layout_equivalent_quote(
        cls,
        quote: str,
        candidates: tuple[EvidenceCandidate, ...],
        chapter: ChapterDocument,
    ) -> EvidenceCandidate | None:
        """Narrow layout-equivalence fallback used only by the ordinary Curator.

        After byte-exact physical lookup fails, the emitted quote is compared
        with the covered catalog candidates with CR/LF plus adjacent
        indentation removed and at most one leading closing dialogue-boundary
        mark ignored.  Binds only when exactly one candidate is
        layout-equivalent and returns that candidate's CANONICAL source text
        and physical span; the model-normalized string is never returned.
        Character changes, internal punctuation changes, ordinary word-space
        changes, out-of-unit spans, and multiple layout-equivalent candidates
        stay unresolved (None) — the caller then proceeds with its typed
        rejection.
        """
        blocks = {block.block_id: block.text for scene in chapter.scenes for block in scene.blocks}
        if len(cls._semantic_span(quote)) < 2:
            return None

        def variants(text: str) -> tuple[str, ...]:
            stripped = [text]
            for mark in cls._CLOSING_DIALOGUE_MARKS:
                if text.startswith(mark):
                    stripped.append(text[len(mark) :])
                    break
            return tuple(dict.fromkeys(cls._layout_normalize(item) for item in stripped))

        quote_variants = variants(quote)
        matches: list[EvidenceCandidate] = []
        for candidate in candidates:
            block_text = blocks.get(candidate.block_id)
            # Only real physical spans of the covered source unit may bind;
            # a candidate whose claimed span does not reproduce the chapter
            # text stays out of the fallback (fail-closed).
            if block_text is None or block_text[candidate.start : candidate.end] != candidate.text:
                continue
            candidate_variants = variants(candidate.text)
            if (
                any(
                    candidate_normalized and candidate_normalized in quote_variants
                    for candidate_normalized in candidate_variants
                )
                and candidate not in matches
            ):
                matches.append(candidate)
        if len(matches) == 1:
            return matches[0]
        return None

    @staticmethod
    def _similarity_ratio(quote: str, candidate: EvidenceCandidate) -> float:
        """Deterministic similarity between a quote and one catalog candidate."""

        return EvidenceCandidateGenerator._text_similarity_ratio(quote, candidate.text)

    @staticmethod
    def _text_similarity_ratio(quote: str, text: str) -> float:
        """Deterministic similarity between a quote and verbatim catalog text."""

        normalized = EvidenceCandidateGenerator._semantic_span(quote)
        span = EvidenceCandidateGenerator._semantic_span(text)
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
        literals = tuple(
            dict.fromkeys(
                literal
                for candidate in candidates
                for literal in self._copyable_literal_variants(candidate.text, max_chars=max_chars)
            )
        )
        ranked = sorted(
            literals,
            key=lambda literal: self._text_similarity_ratio(quote, literal),
            reverse=True,
        )
        for literal in ranked:
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

    @staticmethod
    def _copyable_literal_variants(text: str, *, max_chars: int) -> tuple[str, ...]:
        """Bounded verbatim strings suitable for resolver-checked feedback.

        A generated catalog candidate may be longer than the feedback budget
        even when it contains a short, highly relevant original clause.  Keep
        the complete candidate when it fits and otherwise (or additionally)
        consider only natural sentence/clause spans copied from that candidate.
        Derived spans remain feedback-only and are never evidence until the
        strict resolver accepts them against the full catalog.
        """

        variants: list[str] = []
        if len(text) <= max_chars:
            variants.append(text)
        for part in _FEEDBACK_LITERAL_SPLIT.split(text):
            literal = part.strip()
            if (
                literal
                and len(literal) <= max_chars
                and len(EvidenceCandidateGenerator._semantic_span(literal))
                >= _MIN_DERIVED_LITERAL_SEMANTIC_CHARS
            ):
                variants.append(literal)
        return tuple(dict.fromkeys(variants))

    def resolve_quote(
        self,
        selection: EvidenceQuoteSelection,
        chapter: ChapterDocument,
    ) -> EvidenceCandidate:
        blocks = {
            block.block_id: (scene.scene_index, block)
            for scene in chapter.scenes
            for block in scene.blocks
        }
        resolved_block = blocks.get(selection.block_id)
        if resolved_block is None:
            raise ValueError("quote selection references unknown block")
        scene_index, block = resolved_block
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
            scene_index=scene_index,
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
