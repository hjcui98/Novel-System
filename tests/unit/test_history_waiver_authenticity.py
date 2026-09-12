"""R1.4: a model may propose a history waiver but may not approve itself.

The frozen v6 chapter-set candidate marked chapters 2-5 ``NOT_REQUIRED`` with
``waiver_ref="first_chapter_waiver_extension"``, a string no host ever issued, and
host review accepted it.  These tests pin the host-side rules.
"""

from __future__ import annotations

import json

from novel_agent.agents.plan_reviewer import apply_host_plan_review_constraints
from novel_agent.domain.planning import (
    PlanReviewDraft,
    ReviewDecision,
    ReviewTargetKind,
)
from novel_agent.domain.retrieval_decision import (
    FIRST_CHAPTER_WAIVER_REF,
    HistoryRetrievalDecision,
)
from novel_agent.domain.stage2 import AgentMode

# The exact frozen v6 decision payload.
_V6_DECISION = {
    "reason_code": "no_historical_dependency",
    "requirement": "NOT_REQUIRED",
    "waiver_ref": "first_chapter_waiver_extension",
}


def _draft() -> PlanReviewDraft:
    return PlanReviewDraft(
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        decision=ReviewDecision.ACCEPT,
        issues=(),
    )


def _review(*, chapter: int, decision: dict[str, object]) -> PlanReviewDraft:
    payload = json.dumps(
        {
            "items": [
                {
                    "item_id": f"plan-item.ch{chapter}",
                    "kind": "chapter_goal",
                    "payload": {"chapter_index": chapter, "history_retrieval": decision},
                }
            ]
        }
    )
    return apply_host_plan_review_constraints(
        _draft(),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=payload,
        mode=AgentMode.CHAPTER_SET,
    )


def test_model_invented_waiver_on_later_chapter_is_revise() -> None:
    review = _review(chapter=2, decision=dict(_V6_DECISION))

    assert review.decision is ReviewDecision.REVISE
    assert any("HISTORY_WAIVER_UNVERIFIED" in issue.summary for issue in review.issues)


def test_first_chapter_waiver_string_on_chapter_five_is_rejected() -> None:
    review = _review(
        chapter=5,
        decision={
            "reason_code": "no_historical_dependency",
            "requirement": "NOT_REQUIRED",
            "waiver_ref": FIRST_CHAPTER_WAIVER_REF,
        },
    )

    assert review.decision is ReviewDecision.REVISE
    assert any("HISTORY_WAIVER_INAPPLICABLE" in issue.summary for issue in review.issues)


def test_host_first_chapter_waiver_on_chapter_one_is_accepted() -> None:
    review = _review(
        chapter=1,
        decision={
            "reason_code": "first_chapter",
            "requirement": "NOT_REQUIRED",
            "waiver_ref": FIRST_CHAPTER_WAIVER_REF,
        },
    )

    assert not any("HISTORY_WAIVER" in issue.summary for issue in review.issues)


def test_decision_helper_reports_whether_the_waiver_is_host_issued() -> None:
    assert HistoryRetrievalDecision.first_chapter_waiver().waiver_is_host_issued is True

    forged = HistoryRetrievalDecision.model_validate(
        {
            "requirement": "NOT_REQUIRED",
            "reason_code": "no_historical_dependency",
            "waiver_ref": "first_chapter_waiver_extension",
        },
        strict=False,
    )
    assert forged.waiver_is_host_issued is False
