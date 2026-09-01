"""L0 exact evidence slice resolution for the evidence-first Writer package.

A Writer-visible evidence item must be an exact read grain: a paragraph, or a
contiguous whole-sentence window inside one paragraph, taken verbatim from the
immutable parent ``TextBlock``.  This resolver is the only entry point that
turns a selected evidence reference into Writer-visible text; it never
summarizes, rewrites or extends beyond the parent block.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.ids import CommitId, StableId
from novel_agent.domain.text import EvidenceRef, TextBlock, TextSpanRef
from novel_agent.domain.writer_context import (
    EvidenceSlice,
    EvidenceSliceKind,
    EvidenceSliceSourceRole,
)
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import quote_hash

_SENTENCE_SPLIT = re.compile(r"[^\u3002\uff01\uff1f!?;\uff1b]+[\u3002\uff01\uff1f!?;\uff1b]?")

DEFAULT_PARAGRAPH_CHAR_LIMIT = 2400
DEFAULT_SENTENCE_WINDOW_CHAR_LIMIT = 1800


@dataclass(frozen=True, slots=True)
class ResolvedSlice:
    slice_: EvidenceSlice
    parent_block_id: StableId


class EvidenceSliceResolutionError(ValueError):
    """A selected evidence reference cannot be resolved to an exact slice."""


@dataclass(frozen=True, slots=True)
class LiveEvidenceBasis:
    """Current request identity used as live equality keys.

    Callers: ``MemoryGateway.resolve`` output assembly, ``PlannerSourceExpander``
    P1, and ``EvidenceFirstWriterContextAssembler`` 回源.  Historical
    ``EvidenceRef.resolved_at_commit`` and evidence ``root_hash`` stay in
    provenance and are not compared to the current Project Commit or whole-book
    TextRoot hash.
    """

    request_commit: CommitId
    request_snapshot_id: StableId
    checkpoint_chapter: int


@dataclass(frozen=True, slots=True)
class LiveEvidenceDecision:
    live: bool
    reason: str


def text_root_indexes(
    text_root: TextRootDocument,
) -> tuple[dict[StableId, TextBlock], dict[StableId, int]]:
    """Locate blocks and chapter indexes on the current append-only TextRoot."""

    blocks: dict[StableId, TextBlock] = {}
    chapter_indexes: dict[StableId, int] = {}
    if text_root.prelude is not None:
        chapter_indexes[text_root.prelude.prelude_id] = 0
        for scene in text_root.prelude.scenes:
            for block in scene.blocks:
                blocks[block.block_id] = block
    for chapter in text_root.chapters:
        chapter_indexes[chapter.chapter_id] = chapter.chapter_index
        for scene in chapter.scenes:
            for block in scene.blocks:
                blocks[block.block_id] = block
    return blocks, chapter_indexes


class EvidenceSliceResolver:
    """Resolve verified evidence spans into exact, stable L0 slices."""

    version = "evidence_slice_resolver.v1"

    def __init__(
        self,
        *,
        paragraph_char_limit: int = DEFAULT_PARAGRAPH_CHAR_LIMIT,
        sentence_window_char_limit: int = DEFAULT_SENTENCE_WINDOW_CHAR_LIMIT,
    ) -> None:
        if paragraph_char_limit < 1 or sentence_window_char_limit < 1:
            raise ValueError("slice char limits must be positive")
        if sentence_window_char_limit > paragraph_char_limit:
            raise ValueError("sentence window limit cannot exceed the paragraph limit")
        self._paragraph_char_limit = paragraph_char_limit
        self._sentence_window_char_limit = sentence_window_char_limit

    def resolve_block(
        self,
        block: TextBlock,
        *,
        source_commit: CommitId,
        snapshot_id: StableId,
        access_scope: str,
        source_role: EvidenceSliceSourceRole = EvidenceSliceSourceRole.UNKNOWN,
        allow_heading_evidence: bool = False,
        paragraph_budget: int | None = None,
        sentence_window_budget: int | None = None,
    ) -> tuple[EvidenceSlice, ...]:
        """Slice a full block into exact paragraphs (or bounded sentence windows)."""
        budget = paragraph_budget or self._paragraph_char_limit
        sentence_budget = sentence_window_budget or self._sentence_window_char_limit
        if not allow_heading_evidence and source_role is EvidenceSliceSourceRole.HEADING:
            return ()
        slices: list[EvidenceSlice] = []
        for start, end, text in self._paragraphs(block.text):
            if len(text) <= budget:
                slices.append(
                    self._slice(
                        block=block,
                        start=start,
                        end=end,
                        text=text,
                        slice_kind=EvidenceSliceKind.PARAGRAPH,
                        source_commit=source_commit,
                        snapshot_id=snapshot_id,
                        access_scope=access_scope,
                        source_role=source_role,
                    )
                )
                continue
            window = self._sentence_window(
                block.text,
                start,
                end,
                limit=sentence_budget,
            )
            if window is None:
                continue
            window_start, window_end, window_text = window
            slices.append(
                self._slice(
                    block=block,
                    start=window_start,
                    end=window_end,
                    text=window_text,
                    slice_kind=EvidenceSliceKind.SENTENCE_WINDOW,
                    source_commit=source_commit,
                    snapshot_id=snapshot_id,
                    access_scope=access_scope,
                    source_role=source_role,
                )
            )
        return tuple(slices)

    def resolve_evidence(
        self,
        evidence: EvidenceRef,
        block: TextBlock,
        *,
        source_commit: CommitId,
        snapshot_id: StableId,
        access_scope: str,
        source_role: EvidenceSliceSourceRole = EvidenceSliceSourceRole.UNKNOWN,
        allow_heading_evidence: bool = False,
        paragraph_budget: int | None = None,
        sentence_window_budget: int | None = None,
    ) -> tuple[EvidenceSlice, ...]:
        """Resolve one verified evidence span to exact L0 slices.

        Fail-closed: the span must belong to the supplied block, the block
        object hash must match the evidence, and the quote hash must round-trip.
        The paragraph (or a contiguous sentence window inside it) covering the
        span is returned verbatim.
        """
        if not allow_heading_evidence and source_role is EvidenceSliceSourceRole.HEADING:
            return ()
        self._verify_evidence(evidence, block)
        if evidence.span is None:  # pragma: no cover - rejected by _verify_evidence first
            raise EvidenceSliceResolutionError("evidence has no precise span")
        paragraph = self._covering_paragraph(block.text, evidence.span)
        if paragraph is None:
            raise EvidenceSliceResolutionError("evidence span is outside the parent block")
        start, end, text = paragraph
        budget = paragraph_budget or self._paragraph_char_limit
        if len(text) <= budget:
            return (
                self._slice(
                    block=block,
                    start=start,
                    end=end,
                    text=text,
                    slice_kind=EvidenceSliceKind.PARAGRAPH,
                    source_commit=source_commit,
                    snapshot_id=snapshot_id,
                    access_scope=access_scope,
                    source_role=source_role,
                ),
            )
        window = self._sentence_window(
            block.text,
            start,
            end,
            limit=sentence_window_budget or self._sentence_window_char_limit,
        )
        if window is None:
            raise EvidenceSliceResolutionError(
                "evidence paragraph exceeds the sentence window budget"
            )
        window_start, window_end, window_text = window
        return (
            self._slice(
                block=block,
                start=window_start,
                end=window_end,
                text=window_text,
                slice_kind=EvidenceSliceKind.SENTENCE_WINDOW,
                source_commit=source_commit,
                snapshot_id=snapshot_id,
                access_scope=access_scope,
                source_role=source_role,
            ),
        )

    def live_decision(
        self,
        *,
        basis: LiveEvidenceBasis,
        unit_source_commit: CommitId,
        unit_snapshot_id: StableId,
        evidence: EvidenceRef | None = None,
        block: TextBlock | None = None,
        chapter_index: int | None = None,
        slice_: EvidenceSlice | None = None,
    ) -> LiveEvidenceDecision:
        """Decide current readability without using historical commit/root keys."""

        if unit_source_commit != basis.request_commit:
            return LiveEvidenceDecision(False, "source_commit_mismatch")
        if unit_snapshot_id != basis.request_snapshot_id:
            return LiveEvidenceDecision(False, "snapshot_mismatch")
        if slice_ is not None:
            if slice_.source_commit != basis.request_commit:
                return LiveEvidenceDecision(False, "source_commit_mismatch")
            if slice_.snapshot_id != basis.request_snapshot_id:
                return LiveEvidenceDecision(False, "snapshot_mismatch")
            if block is None or slice_.parent_block_id != block.block_id:
                return LiveEvidenceDecision(False, "missing_block")
            if chapter_index is None or chapter_index > basis.checkpoint_chapter:
                return LiveEvidenceDecision(False, "cutoff")
            if (
                slice_.end > len(block.text)
                or slice_.start < 0
                or slice_.chapter_id != block.chapter_id
                or slice_.object_hash != sha256_id(block.text.encode("utf-8"))
                or block.text[slice_.start : slice_.end] != slice_.text
            ):
                return LiveEvidenceDecision(False, "hash_mismatch")
            if quote_hash(slice_.text) != slice_.quote_hash:
                return LiveEvidenceDecision(False, "quote_mismatch")
            return LiveEvidenceDecision(True, "live")
        if evidence is None or evidence.span is None:
            return LiveEvidenceDecision(False, "no_span")
        if block is None or evidence.span.block_id != block.block_id:
            return LiveEvidenceDecision(False, "missing_block")
        if chapter_index is None or chapter_index > basis.checkpoint_chapter:
            return LiveEvidenceDecision(False, "cutoff")
        try:
            self._verify_evidence(evidence, block)
        except EvidenceSliceResolutionError as error:
            message = str(error)
            if "object hash" in message:
                return LiveEvidenceDecision(False, "hash_mismatch")
            if "quote hash" in message:
                return LiveEvidenceDecision(False, "quote_mismatch")
            return LiveEvidenceDecision(False, "span_mismatch")
        return LiveEvidenceDecision(True, "live")

    def resolve_live_evidence(
        self,
        *,
        basis: LiveEvidenceBasis,
        unit_source_commit: CommitId,
        unit_snapshot_id: StableId,
        evidence: EvidenceRef,
        block: TextBlock | None,
        chapter_index: int | None,
        access_scope: str,
    ) -> tuple[EvidenceSlice, ...] | None:
        decision = self.live_decision(
            basis=basis,
            unit_source_commit=unit_source_commit,
            unit_snapshot_id=unit_snapshot_id,
            evidence=evidence,
            block=block,
            chapter_index=chapter_index,
        )
        if not decision.live or block is None:
            return None
        try:
            return self.resolve_evidence(
                evidence,
                block,
                source_commit=basis.request_commit,
                snapshot_id=basis.request_snapshot_id,
                access_scope=access_scope,
            )
        except EvidenceSliceResolutionError:
            return None

    @staticmethod
    def _verify_evidence(evidence: EvidenceRef, block: TextBlock) -> None:
        if evidence.span is None:
            raise EvidenceSliceResolutionError("evidence has no precise span")
        if evidence.chapter_id != block.chapter_id:
            raise EvidenceSliceResolutionError("evidence chapter does not match its block")
        if (
            evidence.scene_id is not None
            and block.scene_id is not None
            and evidence.scene_id != block.scene_id
        ):
            raise EvidenceSliceResolutionError("evidence scene does not match its block")
        if evidence.span.end > len(block.text):
            raise EvidenceSliceResolutionError("evidence span exceeds the parent block")
        if evidence.object_hash != sha256_id(block.text.encode("utf-8")):
            raise EvidenceSliceResolutionError(
                "evidence object hash does not match the parent block"
            )
        selected = block.text[evidence.span.start : evidence.span.end]
        if evidence.quote_hash is not None and evidence.quote_hash != quote_hash(selected):
            raise EvidenceSliceResolutionError("evidence quote hash does not round-trip")

    @classmethod
    def _paragraphs(cls, text: str) -> tuple[tuple[int, int, str], ...]:
        """Split block text into trimmed paragraphs with exact source offsets.

        Paragraphs are newline-separated; leading/trailing whitespace of each
        paragraph is trimmed by adjusting the offsets so the slice text is an
        exact substring of the parent block.
        """
        paragraphs: list[tuple[int, int, str]] = []
        for start, end in _line_spans(text):
            leading = _leading_ws(text[start:end])
            trailing = _trailing_ws(text[start:end])
            content_start = start + leading
            content_end = end - trailing
            content = text[content_start:content_end]
            if content:
                paragraphs.append((content_start, content_end, content))
        return tuple(paragraphs)

    @classmethod
    def _covering_paragraph(
        cls,
        text: str,
        span: TextSpanRef,
    ) -> tuple[int, int, str] | None:
        for start, end, content in cls._paragraphs(text):
            if span.start < end and start < span.end:
                return (start, end, content)
        return None

    @classmethod
    def _sentence_window(
        cls,
        text: str,
        paragraph_start: int,
        paragraph_end: int,
        *,
        limit: int,
    ) -> tuple[int, int, str] | None:
        """Contiguous whole-sentence window over the paragraph, bounded to ``limit``.

        The window keeps sentences in original order and expands symmetrically
        from the middle so short passages stay coherent; if the single longest
        sentence already exceeds ``limit`` the paragraph cannot be represented
        and ``None`` is returned (typed budget gap at the caller).
        """
        sentences: list[tuple[int, int]] = []
        for match in _SENTENCE_SPLIT.finditer(text, paragraph_start, paragraph_end):
            raw_start, raw_end = match.start(), match.end()
            leading = _leading_ws(text[raw_start:raw_end])
            trailing = _trailing_ws(text[raw_start:raw_end])
            start = raw_start + leading
            end = raw_end - trailing
            if end > start:  # pragma: no branch - whitespace-only spans are trimmed away
                sentences.append((start, end))
        if not sentences:
            return None
        longest = max((end - start for start, end in sentences), default=0)
        if longest > limit:
            return None
        center = len(sentences) // 2
        selected = [sentences[center]]
        chars = sentences[center][1] - sentences[center][0]
        left = center - 1
        right = center + 1
        while chars < limit and (left >= 0 or right < len(sentences)):
            candidates: list[tuple[int, tuple[int, int]]] = []
            if left >= 0:
                candidates.append((sentences[left][1] - sentences[left][0], sentences[left]))
            if right < len(sentences):
                candidates.append((sentences[right][1] - sentences[right][0], sentences[right]))
            candidates.sort()
            _cost, (s_start, s_end) = candidates[0]
            if chars + _cost > limit:
                break
            if s_end <= sentences[center][1]:
                left -= 1
            else:
                right += 1
            selected.append((s_start, s_end))
            chars += _cost
        selected.sort()
        window_start = selected[0][0]
        window_end = selected[-1][1]
        window_text = text[window_start:window_end]
        if not window_text:  # pragma: no cover - non-empty sentences imply a non-empty window
            return None  # pragma: no cover
        return (window_start, window_end, window_text)

    @classmethod
    def _slice(
        cls,
        *,
        block: TextBlock,
        start: int,
        end: int,
        text: str,
        slice_kind: EvidenceSliceKind,
        source_commit: CommitId,
        snapshot_id: StableId,
        access_scope: str,
        source_role: EvidenceSliceSourceRole,
    ) -> EvidenceSlice:
        if text != block.text[start:end]:  # pragma: no cover - offsets are exact by construction
            raise EvidenceSliceResolutionError("slice text does not round-trip to its parent block")
        digest = hashlib.sha256(
            (f"{block.block_id.root}\0{start}\0{end}\0{quote_hash(text).root}").encode()
        ).hexdigest()
        return EvidenceSlice(
            slice_id=StableId(f"slice.{digest}"[:128]),
            parent_block_id=block.block_id,
            chapter_id=block.chapter_id,
            scene_id=block.scene_id,
            start=start,
            end=end,
            text=text,
            object_hash=sha256_id(block.text.encode("utf-8")),
            quote_hash=quote_hash(text),
            source_commit=source_commit,
            snapshot_id=snapshot_id,
            access_scope=access_scope,
            slice_kind=slice_kind,
            source_role=source_role,
        )


def _line_spans(text: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    start = 0
    for match in re.finditer(r"\n", text):
        spans.append((start, match.start()))
        start = match.end()
    spans.append((start, len(text)))
    return tuple(spans)


def _leading_ws(value: str) -> int:
    return len(value) - len(value.lstrip())


def _trailing_ws(value: str) -> int:
    return len(value) - len(value.rstrip())


__all__ = [
    "DEFAULT_PARAGRAPH_CHAR_LIMIT",
    "DEFAULT_SENTENCE_WINDOW_CHAR_LIMIT",
    "EvidenceSliceResolutionError",
    "EvidenceSliceResolver",
    "LiveEvidenceBasis",
    "LiveEvidenceDecision",
    "ResolvedSlice",
    "text_root_indexes",
]
