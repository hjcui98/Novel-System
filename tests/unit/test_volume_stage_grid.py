"""R2.1: volume stage semantics, not just ten non-empty fields.

The v6 volumes filled every required slot with prose, so the structure check passed
while no stage was actually bound to a chapter range or a responsibility.  A
*structured* stage entry must declare both; free-text slots stay acceptable because
they are volume-scoped by definition.
"""

from __future__ import annotations

import json

from novel_agent.agents.plan_reviewer import apply_host_plan_review_constraints
from novel_agent.domain.planning import (
    VOLUME_STRUCTURE_REQUIRED_KEYS,
    PlanReviewDraft,
    ReviewDecision,
    ReviewTargetKind,
    volume_stage_grid_defects,
)
from novel_agent.domain.stage2 import AgentMode


def _volume_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {key: f"{key} prose" for key in VOLUME_STRUCTURE_REQUIRED_KEYS}
    payload.update({"plan_level": "arc_volume", "chapter_start": 1, "chapter_end": 100})
    payload["obligation_plan"] = [
        {
            "kind": "objective",
            "summary": "陆沉舟获得铜铭",
            "setup_window": "1-50",
            "progress_windows": ["51-80"],
            "payoff_window": "90-100",
        }
    ]
    payload.update(overrides)
    return payload


def _review(payload: dict[str, object]) -> PlanReviewDraft:
    target = json.dumps(
        {
            "expected_volume_count": 1,
            "target_chapters": 100,
            "items": [{"item_id": "vol_01", "kind": "arc_volume", "payload": payload}],
        }
    )
    return apply_host_plan_review_constraints(
        PlanReviewDraft(
            target_kind=ReviewTargetKind.PLAN_PROPOSAL,
            decision=ReviewDecision.ACCEPT,
            issues=(),
        ),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=target,
        mode=AgentMode.ARC_VOLUME,
    )


def test_free_text_stage_slots_remain_acceptable() -> None:
    assert volume_stage_grid_defects(_volume_payload()) == ()
    review = _review(_volume_payload())
    assert not any("VOLUME_STAGE_UNUSABLE" in issue.summary for issue in review.issues)


def test_structured_stage_entry_without_range_is_rejected() -> None:
    payload = _volume_payload(
        entry_conditions={
            "summary": "进入内府",
            "obligation_ids": ["obligation.vol_01.0.objective"],
        }
    )

    defects = volume_stage_grid_defects(payload)
    assert defects == ("entry_conditions[0] declares no usable chapter_start/chapter_end",)
    review = _review(payload)
    assert review.decision is ReviewDecision.REVISE
    assert any("VOLUME_STAGE_UNUSABLE" in issue.summary for issue in review.issues)


def test_structured_stage_entry_without_obligation_is_rejected() -> None:
    payload = _volume_payload(
        exit_conditions=[{"summary": "获得铜铭", "chapter_start": 90, "chapter_end": 100}]
    )

    assert volume_stage_grid_defects(payload) == (
        "exit_conditions[0] declares no obligation id it serves",
    )


def test_structured_stage_entry_outside_the_volume_scope_is_rejected() -> None:
    payload = _volume_payload(
        reveal_window=[
            {
                "summary": "暗示断序真相",
                "chapter_start": 90,
                "chapter_end": 150,
                "obligation_ids": ["obligation.vol_01.1.foreshadowing"],
            }
        ]
    )

    assert volume_stage_grid_defects(payload) == ("reveal_window[0] ends after its volume scope",)


def test_complete_structured_stage_entry_passes() -> None:
    payload = _volume_payload(
        entry_conditions=[
            {
                "summary": "陆沉舟位于灰垣镇",
                "chapter_start": 1,
                "chapter_end": 10,
                "obligation_ids": ["obligation.vol_01.0.objective"],
            }
        ],
        capability_ceiling=[
            {
                "summary": "最多二阶聚纹",
                "chapter_start": 1,
                "chapter_end": 100,
                "obligation_ids": ["obligation.vol_01.0.objective"],
            }
        ],
    )

    assert volume_stage_grid_defects(payload) == ()
    review = _review(payload)
    assert not any("VOLUME_STAGE_UNUSABLE" in issue.summary for issue in review.issues)
