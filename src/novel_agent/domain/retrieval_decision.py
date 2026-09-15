"""Explicit history retrieval decisions shared by planning, memory and Writer.

These contracts live outside ``benchmark``/``writer_context`` so both modules
can consume them without a circular import.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from enum import StrEnum

from pydantic import Field, model_validator

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import StableId


class HistoryRetrievalRequirement(StrEnum):
    REQUIRED = "REQUIRED"
    NOT_REQUIRED = "NOT_REQUIRED"
    UNDECIDED = "UNDECIDED"


class HistoryRetrievalReasonCode(StrEnum):
    FIRST_CHAPTER = "first_chapter"
    NO_HISTORICAL_DEPENDENCY = "no_historical_dependency"
    REVIEW_WAIVER = "review_waiver"


class RetrievalExecutionStatus(StrEnum):
    NOT_REQUESTED = "NOT_REQUESTED"
    EXECUTED = "EXECUTED"
    NO_HIT = "NO_HIT"
    NOT_APPLICABLE = "NOT_APPLICABLE"


_HISTORY_RETRIEVAL_NEED_KINDS = frozenset(
    {
        "causal_history",
        "knowledge_origin",
        "relationship_origin",
        "setup_evidence",
        "object_origin",
    }
)

# Host-issued waiver identities.  A planning model may propose that history is not
# required, but it may not approve itself: only these references are recognised, and
# the first-chapter waiver is additionally scoped to chapter 1 with an empty
# canonical text.  Any other NOT_REQUIRED decision needs a real approval receipt.
FIRST_CHAPTER_WAIVER_REF = "waiver.history.first_chapter"
HOST_ISSUED_WAIVER_REFS = frozenset({FIRST_CHAPTER_WAIVER_REF})
_TARGET_EVENT_QUESTION = re.compile(r"如何|怎样|怎么|何时|是否会|将如何|具体触发")
_NON_WORD = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def history_need_targets_same_chapter(query: str, target_texts: tuple[str, ...]) -> bool:
    """Detect a Need that asks Memory to narrate its own not-yet-written target event."""

    if _TARGET_EVENT_QUESTION.search(query) is None:
        return False
    normalized_query = _NON_WORD.sub("", query)
    for text in target_texts:
        normalized_target = _NON_WORD.sub("", text)
        if not normalized_target:
            continue
        match = SequenceMatcher(
            None, normalized_query, normalized_target, autojunk=False
        ).find_longest_match()
        if match.size >= 6:
            return True
    return False


class HistoryRetrievalNeed(DomainModel):
    """One explicit historical retrieval need declared by an accepted plan item."""

    kind: str = Field(min_length=1)
    query: str = Field(min_length=1)
    entity_ids: tuple[StableId, ...] = ()
    predicates: tuple[str, ...] = ()
    why_needed: str | None = None
    source_chapter_end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_kind(self) -> HistoryRetrievalNeed:
        if self.kind not in _HISTORY_RETRIEVAL_NEED_KINDS:
            raise ValueError(f"history retrieval need kind is not allowed: {self.kind!r}")
        return self


class HistoryRetrievalDecision(DomainModel):
    """Explicit decision about whether the chapter needs historical retrieval.

    ``history_needs: []`` alone can never prove the question was decided; an
    empty decision is ``UNDECIDED`` and must block Writer execution.
    """

    requirement: HistoryRetrievalRequirement = HistoryRetrievalRequirement.UNDECIDED
    reason: str = ""
    reason_code: HistoryRetrievalReasonCode | None = None
    waiver_ref: str | None = Field(default=None, min_length=1)
    needs: tuple[HistoryRetrievalNeed, ...] = ()

    @model_validator(mode="after")
    def validate_decision(self) -> HistoryRetrievalDecision:
        if self.requirement is HistoryRetrievalRequirement.REQUIRED:
            if not self.needs:
                raise ValueError("REQUIRED history retrieval decision needs at least one Need")
            if len(self.needs) > 3:
                raise ValueError("REQUIRED history retrieval decision permits at most three Needs")
        elif self.requirement is HistoryRetrievalRequirement.NOT_REQUIRED:
            if self.needs:
                raise ValueError("NOT_REQUIRED history retrieval decision cannot carry Needs")
            if self.reason_code is None:
                raise ValueError("NOT_REQUIRED history retrieval decision requires a reason_code")
            if not self.waiver_ref:
                raise ValueError("NOT_REQUIRED history retrieval decision requires a waiver_ref")
        elif self.needs:
            raise ValueError("UNDECIDED history retrieval decision cannot carry Needs")
        return self

    @classmethod
    def first_chapter_waiver(cls) -> HistoryRetrievalDecision:
        return cls(
            requirement=HistoryRetrievalRequirement.NOT_REQUIRED,
            reason="chapter 1 has no canonical prose history",
            reason_code=HistoryRetrievalReasonCode.FIRST_CHAPTER,
            waiver_ref=FIRST_CHAPTER_WAIVER_REF,
        )

    @property
    def waiver_is_host_issued(self) -> bool:
        """Report whether the waiver reference is one the host actually issues."""

        return self.waiver_ref in HOST_ISSUED_WAIVER_REFS
