"""R1: host review must reject unreadable obligations before acceptance.

The frozen v6 five-chapter candidate was accepted by host review and only failed
later at commit, because ``obligation_actions`` free-text strings were not checked
at the review boundary.  These tests pin the review-side contract.
"""

from __future__ import annotations

import json

from novel_agent.agents.plan_reviewer import apply_host_plan_review_constraints
from novel_agent.domain.planning import (
    PlanReviewDraft,
    ReviewDecision,
    ReviewIssueKind,
    ReviewTargetKind,
)
from novel_agent.domain.stage2 import AgentMode

_VOLUME_ONE = {
    "kind": "objective",
    "not_before_chapter": 1,
    "payoff_window": "90-100",
    "progress_windows": ["51-80"],
    "setup_window": "1-50",
    "summary": "陆沉舟获得铜铭",
}
_CHAPTER_ACTION = "setup: 确认残星纹为断序星纹 (setup_window: 1-30)"


def _draft() -> PlanReviewDraft:
    return PlanReviewDraft(
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        decision=ReviewDecision.ACCEPT,
        issues=(),
    )


def _payload(*items: dict[str, object]) -> str:
    return json.dumps({"items": list(items)})


def _review(payload: str, *, mode: str) -> PlanReviewDraft:
    return apply_host_plan_review_constraints(
        _draft(),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=payload,
        mode=AgentMode(mode),
    )


def test_free_text_chapter_action_is_revise_before_acceptance() -> None:
    review = _review(
        _payload(
            {
                "item_id": "plan-item.ch1",
                "kind": "chapter_goal",
                "payload": {"chapter_index": 1, "obligation_actions": [_CHAPTER_ACTION]},
            }
        ),
        mode="chapter_set",
    )

    assert review.decision is ReviewDecision.REVISE
    assert review.revision_instruction
    assert any(
        issue.kind is ReviewIssueKind.OBLIGATION_CONTRACT
        and issue.blocking
        and "OBLIGATION_ACTION_UNREADABLE" in issue.summary
        for issue in review.issues
    )


def test_valid_structured_action_is_not_flagged() -> None:
    review = _review(
        _payload(
            {
                "item_id": "plan-item.ch1",
                "kind": "chapter_goal",
                "payload": {
                    "chapter_index": 1,
                    "obligation_actions": [
                        {
                            "obligation_id": "obligation.vol_01.0.objective",
                            "action": "SETUP",
                            "expected_delta": "铜铭首次出现但不解释来历",
                        }
                    ],
                },
            }
        ),
        mode="chapter_set",
    )

    assert not any(issue.kind is ReviewIssueKind.OBLIGATION_CONTRACT for issue in review.issues)


def test_chapter_set_may_not_declare_a_durable_responsibility_table() -> None:
    review = _review(
        _payload(
            {
                "item_id": "plan-item.ch1",
                "kind": "chapter_goal",
                "payload": {"chapter_index": 1, "obligation_plan": [dict(_VOLUME_ONE)]},
            }
        ),
        mode="chapter_set",
    )

    assert review.decision is ReviewDecision.REVISE
    assert any(
        "OBLIGATION_PLAN_FORBIDDEN" in issue.summary and issue.blocking for issue in review.issues
    )


def test_unreadable_legacy_responsibility_table_is_revise() -> None:
    review = _review(
        _payload(
            {
                "item_id": "vol_01",
                "kind": "arc_volume",
                "payload": {
                    "plan_level": "arc_volume",
                    "chapter_start": 1,
                    "chapter_end": 100,
                    "obligation_plan": [
                        dict(_VOLUME_ONE),
                        {"kind": "objective", "summary": "缺窗口", "setup_window": "1-5"},
                    ],
                },
            }
        ),
        mode="arc_volume",
    )

    assert any(
        "OBLIGATION_PLAN_UNREADABLE" in issue.summary and issue.blocking for issue in review.issues
    )


def test_readable_legacy_responsibility_table_survives_review() -> None:
    review = _review(
        _payload(
            {
                "item_id": "vol_01",
                "kind": "arc_volume",
                "payload": {
                    "plan_level": "arc_volume",
                    "chapter_start": 1,
                    "chapter_end": 100,
                    "obligation_plan": [dict(_VOLUME_ONE)],
                },
            }
        ),
        mode="arc_volume",
    )

    assert not any(issue.kind is ReviewIssueKind.OBLIGATION_CONTRACT for issue in review.issues)


def test_malformed_declaration_list_is_revise() -> None:
    review = _review(
        _payload(
            {
                "item_id": "vol_01",
                "kind": "arc_volume",
                "payload": {"obligation_declarations": "not-a-list"},
            }
        ),
        mode="arc_volume",
    )

    assert any("OBLIGATION_DECLARATION_UNREADABLE" in issue.summary for issue in review.issues)
