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
    VOLUME_NARRATIVE_STAGE_KEYS,
    VOLUME_STRUCTURE_REQUIRED_KEYS,
    PlanReviewDraft,
    ReviewDecision,
    ReviewTargetKind,
    volume_stage_grid_defects,
    volume_stage_window_defects,
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


def _stage_entry(description: str, window: str, role: str) -> dict[str, object]:
    return {"description": description, "window": window, "role": role}


def _volume_with_stage_windows(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "chapter_start": 301,
        "chapter_end": 400,
        "obligation_plan": [
            {
                "kind": "foreshadowing",
                "summary": "长程真相",
                "not_before_chapter": 350,
                "setup_window": "301-349",
                "payoff_window": "351-400",
            }
        ],
    }
    for key in VOLUME_NARRATIVE_STAGE_KEYS:
        payload[key] = _stage_entry("阶段内容", "301-320", "setup")
    payload["midpoint_reversal"] = _stage_entry("中段反转", "351-370", "payoff")
    payload.update(overrides)
    return payload


def test_a_reveal_stage_before_its_lock_is_a_defect() -> None:
    payload = _volume_with_stage_windows(
        trigger_event=_stage_entry("揭示陆远曾在钧炉城开辟断星宫", "301-340", "payoff")
    )

    defects = volume_stage_window_defects(payload)

    assert defects == (
        "trigger_event is a payoff stage starting at 301, before the declared "
        "not_before_chapter 350 of the responsibility it serves",
    )


def test_free_text_narrative_slots_cannot_be_checked_and_are_refused() -> None:
    payload = _volume_with_stage_windows(opening_state="陆沉舟抵达钧炉城")

    assert volume_stage_window_defects(payload) == (
        "opening_state must declare the chapter window and role it happens in",
    )


def test_setup_stages_before_the_lock_stay_legal() -> None:
    payload = _volume_with_stage_windows(
        first_escalation=_stage_entry("不可解释的异常", "320-340", "setup"),
        second_escalation=_stage_entry("暗线推进", "320-340", "progress"),
    )

    assert volume_stage_window_defects(payload) == ()


def test_stage_windows_must_stay_inside_their_volume() -> None:
    payload = _volume_with_stage_windows(volume_climax=_stage_entry("卷高潮", "380-420", "payoff"))

    assert "volume_climax window ends after its volume scope" in volume_stage_window_defects(
        payload
    )


def test_a_stage_window_violation_reaches_host_review_with_a_targeted_demand() -> None:
    """The lock becomes a field-level finding the planner can be pointed at."""

    payload = _volume_payload(
        **{
            "chapter_start": 301,
            "chapter_end": 400,
            "obligation_plan": [
                {
                    "kind": "foreshadowing",
                    "summary": "长程真相",
                    "not_before_chapter": 350,
                    "setup_window": "301-349",
                    "payoff_window": "351-400",
                }
            ],
            "midpoint_reversal": {
                "description": "揭示陆远曾在此开辟断星宫",
                "window": "301-340",
                "role": "payoff",
            },
        }
    )

    review = _review(payload)

    assert review.decision is ReviewDecision.REVISE
    blocking = [issue for issue in review.issues if issue.blocking]
    assert any("VOLUME_STAGE_WINDOW:" in issue.summary for issue in blocking)
    assert any(issue.kind.value == "volume_stage_window_violation" for issue in blocking)
    assert review.revision_instruction is not None
    assert "stage_windows" in review.revision_instruction


def test_the_arc_volume_contract_states_the_stage_window_shape() -> None:
    from novel_agent.runtime.production_bootstrap import PACKAGE_ROOT

    for name, root in (
        ("stage4_planner_arc_volume_v1.md", "prompts"),
        ("arc_volume_planning_v1.md", "skills"),
    ):
        text = (PACKAGE_ROOT / root / name).read_text(encoding="utf-8")
        assert "卷阶段窗口契约" in text, name
        assert "forbidden_reveal" in text, name
        assert "not_before_chapter" in text, name
