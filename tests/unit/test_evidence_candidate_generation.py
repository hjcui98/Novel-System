"""WP4: evidence candidate generation and quote resolve."""

from __future__ import annotations

import pytest

from novel_agent.domain.benchmark import ChapterDocument, SceneDocument, TextRootDocument
from novel_agent.domain.changes import EvidenceCandidate, EvidenceQuoteSelection
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.text import TextBlock
from novel_agent.services.evidence_candidates import EvidenceCandidateGenerator


def _text_root(text: str, *, chapter_index: int = 1) -> TextRootDocument:
    chapter_id = StableId(f"chapter.{chapter_index}")
    block = TextBlock(
        block_id=StableId(f"block.c{chapter_index}.0"),
        chapter_id=chapter_id,
        scene_id=StableId(f"scene.{chapter_index}"),
        narrative_index=0,
        text=text,
    )
    scene = SceneDocument(
        scene_id=StableId(f"scene.{chapter_index}"),
        scene_index=0,
        blocks=(block,),
    )
    chapter = ChapterDocument(
        chapter_id=chapter_id,
        chapter_index=chapter_index,
        scenes=(scene,),
    )
    return TextRootDocument(
        root_hash=ArtifactId("sha256:" + "d" * 64),
        schema_version=SchemaVersion("0.1.0"),
        chapters=(chapter,),
    )


def test_chinese_sentence_candidates_have_unique_ids_and_offsets() -> None:
    text = (
        "\u9648\u957f\u751f\u62ac\u8d77\u5934\u3002"
        "\u971c\u513f\u51b7\u7b11\u4e00\u58f0\uff01"
        "\u4ed6\u5374\u66f4\u52a0\u81ea\u4fe1\u3002"
    )
    root = _text_root(text)
    gen = EvidenceCandidateGenerator(max_chapter_candidates=32)
    candidates = gen.generate(root, 1)
    assert candidates
    ids = {item.candidate_id for item in candidates}
    assert len(ids) == len(candidates)
    for item in candidates:
        assert 0 <= item.start < item.end <= len(text)
        assert text[item.start : item.end] == item.text
    views = gen.model_views(candidates)
    assert all(set(view.model_dump()) == {"candidate_id", "block_id", "text"} for view in views)


def test_unique_quote_resolves_and_ambiguous_rejects() -> None:
    text = "他很自信。旁人并不自信。他很自信。"
    root = _text_root(text)
    chapter = root.chapters[0]
    gen = EvidenceCandidateGenerator()
    unique = gen.resolve_quote(
        EvidenceQuoteSelection(
            block_id=StableId("block.c1.0"),
            exact_quote="旁人并不自信",
        ),
        chapter,
    )
    assert unique.text == "旁人并不自信"
    try:
        gen.resolve_quote(
            EvidenceQuoteSelection(
                block_id=StableId("block.c1.0"),
                exact_quote="他很自信",
            ),
            chapter,
        )
        raise AssertionError("expected ambiguous quote rejection")
    except ValueError as exc:
        assert "exactly one" in str(exc)


def test_generator_rejects_non_positive_max_candidate_chars() -> None:
    """Line 39: ValueError on max_candidate_chars < 1."""
    try:
        EvidenceCandidateGenerator(max_candidate_chars=0)
        raise AssertionError("expected ValueError for max_candidate_chars=0")
    except ValueError as exc:
        assert "max_candidate_chars" in str(exc)


def test_index_by_id_rejects_duplicate_candidate_ids() -> None:
    """Line 85: duplicate candidate ids raise ValueError."""
    from novel_agent.domain.changes import EvidenceCandidate

    cand = EvidenceCandidate(
        candidate_id=StableId("evidence-candidate.dup"),
        block_id=StableId("block.1"),
        chapter_index=1,
        scene_index=0,
        text="x",
        start=0,
        end=1,
        content_hash=ArtifactId("sha256:" + "a" * 64),
    )
    gen = EvidenceCandidateGenerator()
    try:
        gen.index_by_id((cand, cand))
        raise AssertionError("expected ValueError for duplicate ids")
    except ValueError as exc:
        assert "unique" in str(exc)


def test_resolve_quote_rejects_unknown_block() -> None:
    """Line 96: resolve_quote with unknown block_id raises ValueError."""
    text = "some text here."
    root = _text_root(text)
    chapter = root.chapters[0]
    gen = EvidenceCandidateGenerator()
    try:
        gen.resolve_quote(
            EvidenceQuoteSelection(
                block_id=StableId("block.unknown"),
                exact_quote="some",
            ),
            chapter,
        )
        raise AssertionError("expected ValueError for unknown block")
    except ValueError as exc:
        assert "unknown block" in str(exc)


def test_resolve_quote_uses_left_and_right_context_to_disambiguate() -> None:
    """Lines 108-121: left/right context filters multiple matches to one."""
    text = "alpha beta gamma。alpha delta gamma。"
    root = _text_root(text)
    chapter = root.chapters[0]
    gen = EvidenceCandidateGenerator()
    # "gamma" appears twice; left_context "beta " picks the first occurrence
    result = gen.resolve_quote(
        EvidenceQuoteSelection(
            block_id=StableId("block.c1.0"),
            exact_quote="gamma",
            left_context="beta ",
        ),
        chapter,
    )
    assert result.text == "gamma"
    # right_context " delta" picks the second occurrence
    result2 = gen.resolve_quote(
        EvidenceQuoteSelection(
            block_id=StableId("block.c1.0"),
            exact_quote="gamma",
            right_context="。alpha",
        ),
        chapter,
    )
    assert result2.text == "gamma"


def test_resolve_quote_rejects_occurrence_out_of_range() -> None:
    """Lines 123-125: occurrence index beyond match count raises ValueError."""
    text = "word。word。word。"
    root = _text_root(text)
    chapter = root.chapters[0]
    gen = EvidenceCandidateGenerator()
    try:
        gen.resolve_quote(
            EvidenceQuoteSelection(
                block_id=StableId("block.c1.0"),
                exact_quote="word",
                occurrence=99,
            ),
            chapter,
        )
        raise AssertionError("expected ValueError for out-of-range occurrence")
    except ValueError as exc:
        assert "out of range" in str(exc)


def test_resolve_quote_with_occurrence_picks_exact_match() -> None:
    """Lines 122-125: occurrence selects the right match index."""
    text = "word。word。word。"
    root = _text_root(text)
    chapter = root.chapters[0]
    gen = EvidenceCandidateGenerator()
    result = gen.resolve_quote(
        EvidenceQuoteSelection(
            block_id=StableId("block.c1.0"),
            exact_quote="word",
            occurrence=1,
        ),
        chapter,
    )
    assert result.text == "word"
    assert result.start == text.index("word", text.index("word") + 1)


def test_block_candidates_skips_empty_text_and_dialogue_split() -> None:
    """Lines 148, 191: empty text yields no candidates; dialogue split path."""
    root = _text_root("")
    gen = EvidenceCandidateGenerator()
    # Empty text -> no candidates
    candidates = gen.generate(root, 1)
    assert candidates == ()

    # Dialogue split: text with quotes triggers _DIALOGUE_SPLIT path
    dialogue_text = "\u201c第一段话\u201d\u201c第二段话\u201d"
    root2 = _text_root(dialogue_text)
    candidates2 = gen.generate(root2, 1)
    assert len(candidates2) >= 2


def test_block_candidates_falls_back_to_full_text_window() -> None:
    """Lines 173-174: when segmentation yields no candidates, full-text window is used."""
    # A single long word with no sentence/dialogue delimiters
    text = "abcdefghijklmnopqrstuvwxyz" * 20
    root = _text_root(text)
    gen = EvidenceCandidateGenerator(max_candidate_chars=50)
    candidates = gen.generate(root, 1)
    assert len(candidates) >= 1


def test_bind_candidate_selection_raises_key_error_for_unknown_id() -> None:
    """Lines 245-251: bind_candidate_selection raises KeyError for unknown id."""
    from novel_agent.services.evidence_candidates import bind_candidate_selection

    try:
        bind_candidate_selection(
            (StableId("evidence-candidate.missing"),),
            {},
        )
        raise AssertionError("expected KeyError")
    except KeyError:
        pass


def test_model_views_and_generate_with_max_chapter_limit() -> None:
    """Cover max_chapter_candidates truncation and model_views output."""
    text = "句子一。句子二。句子三。句子四。句子五。"
    root = _text_root(text)
    gen = EvidenceCandidateGenerator(max_chapter_candidates=2)
    candidates = gen.generate(root, 1)
    assert len(candidates) <= 2


def test_whitespace_window_spans_are_skipped() -> None:
    """Line 160: window spans that are all whitespace are skipped."""
    # Text with leading/trailing whitespace and small max_chars so some
    # window spans only cover whitespace.
    text = "  alpha  "
    root = _text_root(text)
    gen = EvidenceCandidateGenerator(max_candidate_chars=2)
    candidates = gen.generate(root, 1)
    # Candidates should only include non-whitespace spans
    for cand in candidates:
        assert cand.text.strip()


def test_dialogue_split_segment_fallback() -> None:
    """Line 191: text with only closing quotes triggers _segment [text] fallback."""
    # Text that is just a closing quote: sentence split gives 1 part,
    # dialogue split gives 0 parts -> return [text]
    text = "\u201d"
    root = _text_root(text)
    gen = EvidenceCandidateGenerator()
    candidates = gen.generate(root, 1)
    assert len(candidates) >= 1
    assert candidates[0].text == "\u201d"


def test_bind_candidate_selection_succeeds_for_valid_ids() -> None:
    """Lines 250-251: bind_candidate_selection success path."""
    from novel_agent.domain.changes import EvidenceCandidate
    from novel_agent.services.evidence_candidates import bind_candidate_selection

    cand = EvidenceCandidate(
        candidate_id=StableId("evidence-candidate.valid"),
        block_id=StableId("block.1"),
        chapter_index=1,
        scene_index=0,
        text="valid text",
        start=0,
        end=10,
        content_hash=ArtifactId("sha256:" + "b" * 64),
    )
    catalog = {cand.candidate_id: cand}
    result = bind_candidate_selection((cand.candidate_id,), catalog)
    assert result == (cand,)


def _candidate(text: str, suffix: str, chapter_index: int = 1) -> EvidenceCandidate:
    return EvidenceCandidate(
        candidate_id=StableId(f"evidence-candidate.{suffix}"),
        block_id=StableId(f"block.{chapter_index}.{suffix}"),
        chapter_index=chapter_index,
        scene_index=0,
        text=text,
        start=0,
        end=len(text),
        content_hash=ArtifactId("sha256:" + "c" * 64),
    )


def test_copyable_literal_skips_ambiguous_nearest_for_resolver_valid_lower() -> None:
    """The nearest literal may be ambiguous; a lower-ranked literal wins only
    when it is accepted by the strict resolver exactly as emitted."""

    gen = EvidenceCandidateGenerator()
    title_text = (
        "\u7b2c32\u7ae0 \u5148\u751f\uff0c\u4f60\u5c31\u6536\u4e86\u6211\u5427\u3002"
        "\u4ed6\u5411\u4ed6\u89e3\u91ca\u3002"
    )
    title = _candidate(title_text, "title")
    dialogue_text = "\u201c\u5148\u751f\uff0c\u4f60\u5c31\u6536\u4e86\u6211\u5427\u3002\u201d"
    dialogue = _candidate(dialogue_text, "dialogue")
    unrelated = _candidate("\u4eca\u5929\u5929\u6c14\u6674\u6717\u3002", "weather")
    candidates = (title, dialogue, unrelated)
    # The failing quote is most similar to the dialogue, which is ambiguous
    # (its span is contained in the title candidate too), so the advertised
    # literal must be the resolvable title instead.
    literal = gen.copyable_literal_for(
        "\u5148\u751f\uff0c\u4f60\u5c31\u6536\u4e86\u6211\u5427\u3002",
        candidates,
        max_chars=120,
    )
    assert literal is not None
    resolved = gen.resolve_evidence_quotes((literal,), candidates)
    assert len(resolved) == 1
    assert resolved[0].candidate_id == title.candidate_id


def test_copyable_literal_every_advertised_literal_is_resolver_valid() -> None:
    """A literal is advertised only after the same resolver accepts it."""

    gen = EvidenceCandidateGenerator()
    first = _candidate(
        "\u5b81\u5a46\u5a46\u770b\u7740\u4ed6\u9762\u65e0\u8868\u60c5\u8bf4\u9053\uff1a"
        "\u6211\u53ea\u80fd\u8fdb\u56fd\u6559\u5b66\u9662\u3002",
        "cue-content",
    )
    second = _candidate(
        "\u53e6\u4e00\u4e2a\u5b8c\u5168\u4e0d\u540c\u7684\u539f\u6587\u7247\u6bb5\u3002",
        "other",
    )
    candidates = (first, second)
    literal = gen.copyable_literal_for(
        "\u5b81\u5a46\u5a46\u770b\u7740\u4ed6\u9762\u65e0\u8868\u60c5\u8bf4\u9053\uff1a"
        "\u4f46\u4f60\u53ea\u80fd\u8fdb\u56fd\u6559\u5b66\u9662\u3002",
        candidates,
        max_chars=160,
    )
    assert literal is not None
    assert len(gen.resolve_evidence_quotes((literal,), candidates)) == 1
    # The advertised literal must be one of the catalog literals, never a
    # similarity-invented or truncated string.
    assert any(candidate.text == literal for candidate in candidates)


def test_copyable_literal_never_truncates_validated_literal() -> None:
    """Size limiting selects a bounded resolver-valid literal; it never
    truncates a literal that was validated at a longer length."""

    gen = EvidenceCandidateGenerator()
    long_text = (
        "\u9648\u957f\u751f\u5728\u5ba2\u6808\u6574\u7406\u9053\u85cf\u7b14\u8bb0\uff0c"
        "\u628a\u6bcf\u4e00\u9875\u90fd\u8bfb\u5b8c\u3002"
    )
    long_unique = _candidate(long_text, "long")
    candidates = (long_unique,)
    # A max_chars shorter than the only resolvable literal must yield None
    # (skip), not a truncated prefix that was never validated.
    assert gen.copyable_literal_for("\u77ed", candidates, max_chars=8) is None
    full = gen.copyable_literal_for("\u77ed", candidates, max_chars=120)
    assert full == long_unique.text
    assert len(full) <= 120
    assert len(gen.resolve_evidence_quotes((full,), candidates)) == 1


def test_copyable_literal_none_when_no_literal_resolves() -> None:
    """When every bounded catalog literal fails the strict resolver, feedback
    must fall back to generic guidance instead of naming a literal."""

    gen = EvidenceCandidateGenerator()
    # Both candidates contain the same long span, so neither literal is unique.
    shared = (
        "\u540c\u4e00\u4e2a\u8db3\u591f\u957f\u7684\u539f\u6587\u7247\u6bb5\u51fa\u73b0\u4e24\u6b21"
    )
    first = _candidate(shared, "ambig-1")
    second = _candidate(shared, "ambig-2")
    candidates = (first, second)
    assert gen.copyable_literal_for(shared, candidates, max_chars=160) is None


def test_copyable_literal_rejects_non_positive_max_chars() -> None:
    gen = EvidenceCandidateGenerator()
    candidate = _candidate("\u9648\u957f\u751f\u62ac\u8d77\u5934\u3002", "c21")
    with pytest.raises(ValueError, match="max_chars"):
        gen.copyable_literal_for("\u9648\u957f\u751f", (candidate,), max_chars=0)
