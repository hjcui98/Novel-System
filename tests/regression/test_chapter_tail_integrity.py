"""Regression coverage for chapter-tail integrity and ending reuse.

A committed chapter was exactly 5,000 characters long and stopped mid-sentence.
Length compliance alone had let it through, and the existing near-copy gate
compares whole chapters, so it could not see a repeated closing paragraph.  These
cases rebuild that failure deterministically.

This module is entirely Chinese prose fixtures, so the ambiguous-fullwidth
punctuation lint does not apply.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import pytest

from novel_agent.domain.generation import WritingLengthPolicy
from novel_agent.services.writer_cognition import (
    TextIntegrityKind,
    candidate_surface_error,
    draft_surface_error,
    tail_repeats_recent_prose,
    text_integrity_verdict,
)

POLICY = WritingLengthPolicy(
    minimum_characters=100,
    target_characters=5_000,
    maximum_characters=5_000,
)

FINISHED_CHAPTER = (
    "雨落在塔檐上，声音像一层薄薄的铁皮。" * 20 + "他把手从刀柄上移开，转身走进雨里。"
)


def test_length_compliant_but_unfinished_candidate_is_not_submittable() -> None:
    """T01: exactly at the limit, still mid-sentence, must not pass the gate."""

    truncated = ("雨落在塔檐上，声音像一层薄薄的铁皮。" * 20) + "他把手从刀柄上移开，"
    assert len(truncated) <= POLICY.maximum_characters

    error = candidate_surface_error(truncated, length_policy=POLICY)

    assert error is not None
    assert "incomplete" in error
    verdict = text_integrity_verdict(truncated)
    assert verdict.kind is TextIntegrityKind.TRUNCATED


def test_provider_output_limit_is_transport_evidence() -> None:
    """T02: a provider length stop is a hard truncation whatever the last glyph is."""

    verdict = text_integrity_verdict(
        "他把手从刀柄上移开，转身走进雨里。",
        provider_finished_by_length=True,
    )

    assert verdict.kind is TextIntegrityKind.TRUNCATED
    error = candidate_surface_error(
        FINISHED_CHAPTER,
        length_policy=POLICY,
        provider_finished_by_length=True,
    )
    assert error is not None and "incomplete" in error


def test_deliberate_suspension_is_not_killed() -> None:
    """T03: a finished sentence may end on an ellipsis and stays submittable."""

    for ending in ("他忽然停住了……", "门在身后合上——", "“你终于来了。”"):
        verdict = text_integrity_verdict(FINISHED_CHAPTER + ending)
        assert verdict.kind is TextIntegrityKind.COMPLETE, ending
        assert candidate_surface_error(FINISHED_CHAPTER + ending, length_policy=POLICY) is None


def test_open_quote_is_a_truncation() -> None:
    verdict = text_integrity_verdict(FINISHED_CHAPTER + "他说：“我不会")

    assert verdict.kind is TextIntegrityKind.TRUNCATED
    assert "open" in verdict.reason


def test_missing_final_punctuation_is_only_a_suspicion() -> None:
    """A suspicion goes to review; it is not a hard refusal on its own."""

    verdict = text_integrity_verdict(FINISHED_CHAPTER + "他走进雨里")

    assert verdict.kind is TextIntegrityKind.SEMANTIC_SUSPICION
    assert verdict.is_definitely_incomplete is False
    assert candidate_surface_error(FINISHED_CHAPTER + "他走进雨里", length_policy=POLICY) is None


def test_tail_reuse_is_detected_locally() -> None:
    """T05: an identical ending is found even when the rest differs."""

    shared_ending = (
        "雨水顺着他的下颌线滑下去，他看着远处的灯火，忽然想起很多年前的那个夜晚，"
        "那时候他还不知道门后有谁在等，也不知道自己会在第二天清晨把整件事说出口，"
        "他只是站在那里，觉得一切才刚刚开始。"
    )
    previous = ("前面完全不同的情节推进。" * 40) + shared_ending
    current = ("这一章说的是别的事情，人物和场景都换了。" * 40) + shared_ending

    assert tail_repeats_recent_prose(previous, current) is True
    error = candidate_surface_error(
        current,
        length_policy=POLICY,
        recent_prose=((previous, False),),
    )
    assert error is not None
    assert "ending" in error


def test_distinct_endings_are_not_flagged() -> None:
    previous = ("前面完全不同的情节推进。" * 40) + "他在雨里站了很久，然后转身离开。"
    current = ("这一章说的是别的事情，人物和场景都换了。" * 40) + (
        "她合上账本，吹灭了灯，把钥匙放进袖袋。"
    )

    assert tail_repeats_recent_prose(previous, current) is False
    assert (
        candidate_surface_error(
            current,
            length_policy=POLICY,
            recent_prose=((previous, False),),
        )
        is None
    )


def test_a_short_shared_phrase_is_only_a_weak_signal() -> None:
    """T06: a stock phrase alone must not trigger a mechanical deletion."""

    previous = ("前面完全不同的情节推进。" * 40) + "一切才刚刚开始。"
    current = ("这一章说的是别的事情，人物和场景都换了。" * 40) + "一切才刚刚开始。"

    assert tail_repeats_recent_prose(previous, current) is False
    assert (
        candidate_surface_error(
            current,
            length_policy=POLICY,
            recent_prose=((previous, False),),
        )
        is None
    )


def test_short_previous_prose_cannot_support_a_tail_claim() -> None:
    assert tail_repeats_recent_prose("雨水落下。", "雨水落下。" * 200) is False


def test_empty_candidate_is_a_hard_truncation() -> None:
    verdict = text_integrity_verdict("   ")

    assert verdict.kind is TextIntegrityKind.TRUNCATED
    assert candidate_surface_error("   ", length_policy=POLICY) is not None


def test_integrity_verdict_is_not_a_quality_claim() -> None:
    """A completed sentence is not proof the chapter discharged its duties."""

    verdict = text_integrity_verdict("他走了。")

    assert verdict.kind is TextIntegrityKind.COMPLETE
    # The length policy still refuses it, which is the separate check.
    assert candidate_surface_error("他走了。", length_policy=POLICY) is not None


def test_draft_surface_gate_applies_the_same_integrity_rule() -> None:
    truncated = ("雨落在塔檐上，声音像一层薄薄的铁皮。" * 20) + "他把手从刀柄上移开，"

    assert draft_surface_error(truncated) is not None
    assert draft_surface_error(FINISHED_CHAPTER) is None


@pytest.mark.parametrize(
    "ending",
    ["他说：“我不会", "（他停住了", "【注", "『未完"],
)
def test_every_unbalanced_delimiter_family_is_detected(ending: str) -> None:
    assert text_integrity_verdict(FINISHED_CHAPTER + ending).kind is TextIntegrityKind.TRUNCATED
