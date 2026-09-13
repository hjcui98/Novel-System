"""R2.1: volume stage semantics, not just ten non-empty fields.

The v6 volumes filled every required slot with prose, so the structure check passed
while no stage was actually bound to a chapter range or a responsibility.  A
*structured* stage entry must declare both; free-text slots stay acceptable because
they are volume-scoped by definition.

A narrative stage also has to say which host-accepted responsibility it serves and
whether it plants, hints, advances or pays off, because that is what decides which
author time lock applies to it.  The authority is the frozen constraint catalogue,
never the candidate's own ``not_before_chapter``.
"""

from __future__ import annotations

import json

from novel_agent.agents.plan_reviewer import apply_host_plan_review_constraints
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.author_constraints import AuthorConstraint, AuthorConstraintCategory
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
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


def _stage_entry(
    description: str,
    window: str,
    role: str,
    serves: str | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {"description": description, "window": window, "role": role}
    if serves is not None:
        entry["serves"] = serves
    return entry


def _constraint(
    constraint_key: str,
    category: AuthorConstraintCategory,
    not_before: int,
    *,
    constraint_id: str,
) -> AuthorConstraint:
    source = ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "b" * 64),
        media_type="application/vnd.novel-agent.project-profile-root+json",
        schema_version=SchemaVersion("1.0.0"),
        byte_length=1,
    )
    return AuthorConstraint(
        constraint_id=StableId(constraint_id),
        constraint_key=constraint_key,
        category=category,
        text=f"{constraint_key} 不得早于 {not_before}",
        source_ref=source,
        source_hash=ArtifactId("sha256:" + "b" * 64),
        not_before_chapter=not_before,
    )


_HINT_LOCK = _constraint(
    "lock.long-truth.vol4-hint",
    AuthorConstraintCategory.REVEAL_WINDOW,
    350,
    constraint_id="author-constraint.reveal_window.3",
)
_ADVANCE_LOCK = _constraint(
    "lock.long-truth.vol5-advance",
    AuthorConstraintCategory.ABILITY_MILESTONE,
    401,
    constraint_id="author-constraint.ability_milestone.5",
)
_LOCKS = (_HINT_LOCK, _ADVANCE_LOCK)


def _volume_with_stage_windows(**overrides: object) -> dict[str, object]:
    """Volume 4 (301-400): the truth may be hinted from 350, advanced from 401."""

    payload: dict[str, object] = {
        "chapter_start": 301,
        "chapter_end": 400,
        "obligation_plan": [
            {
                "kind": "foreshadowing",
                "summary": "长程真相",
                "setup_window": "301-349",
                "payoff_window": "351-400",
            }
        ],
    }
    for key in VOLUME_NARRATIVE_STAGE_KEYS:
        payload[key] = _stage_entry("阶段内容", "301-320", "setup")
    payload["midpoint_reversal"] = _stage_entry(
        "陆远旧日行迹的暗示", "350-370", "hint", "lock.long-truth.vol4-hint"
    )
    payload.update(overrides)
    return payload


def _defects(payload: dict[str, object], **kwargs: object) -> tuple[object, ...]:
    return volume_stage_window_defects(payload, **kwargs)  # type: ignore[arg-type]


def _messages(payload: dict[str, object], **kwargs: object) -> tuple[str, ...]:
    return tuple(defect.message for defect in _defects(payload, **kwargs))  # type: ignore[attr-defined]


def _fields(payload: dict[str, object], **kwargs: object) -> tuple[str, ...]:
    return tuple(defect.field for defect in _defects(payload, **kwargs))  # type: ignore[attr-defined]


def test_a_hint_at_349_violates_its_own_locks_boundary() -> None:
    payload = _volume_with_stage_windows(
        midpoint_reversal=_stage_entry(
            "提前暗示长程真相", "349-370", "hint", "lock.long-truth.vol4-hint"
        )
    )

    assert _fields(payload, constraints=_LOCKS) == ("midpoint_reversal.window",)
    assert _messages(payload, constraints=_LOCKS) == (
        "midpoint_reversal.window starts at 349, before the not_before_chapter 350 of the "
        "hint responsibility it serves (lock.long-truth.vol4-hint)",
    )


def test_a_hint_at_350_is_legal() -> None:
    payload = _volume_with_stage_windows()

    assert volume_stage_window_defects(payload, constraints=_LOCKS) == ()


def test_a_progression_at_400_violates_the_advance_lock() -> None:
    payload = _volume_with_stage_windows(
        volume_climax=_stage_entry(
            "正式推进断序星纹", "400-400", "progression", "lock.long-truth.vol5-advance"
        )
    )

    assert _fields(payload, constraints=_LOCKS) == ("volume_climax.window",)
    assert "not_before_chapter 401" in _messages(payload, constraints=_LOCKS)[0]


def _volume_five(**overrides: object) -> dict[str, object]:
    """Volume 5 (401-500), where the advance lock's own boundary is in scope."""

    payload: dict[str, object] = {"chapter_start": 401, "chapter_end": 500}
    for key in VOLUME_NARRATIVE_STAGE_KEYS:
        payload[key] = _stage_entry("阶段内容", "401-420", "setup")
    payload.update(overrides)
    return payload


def test_a_progression_at_401_is_legal() -> None:
    payload = _volume_five(
        volume_climax=_stage_entry(
            "正式推进断序星纹", "401-410", "progression", "lock.long-truth.vol5-advance"
        )
    )

    assert volume_stage_window_defects(payload, constraints=_LOCKS) == ()


def test_an_unrelated_later_boundary_does_not_block_a_legal_hint() -> None:
    """Only the responsibility a stage names governs that stage's window."""

    payload = _volume_with_stage_windows(
        midpoint_reversal=_stage_entry(
            "350 章合法暗示", "350-360", "hint", "lock.long-truth.vol4-hint"
        )
    )

    assert volume_stage_window_defects(payload, constraints=_LOCKS) == ()


def test_an_invented_responsibility_handle_is_refused() -> None:
    payload = _volume_with_stage_windows(
        trigger_event=_stage_entry(
            "编造的责任", "301-320", "hint", "obligation.invented.by.the.model"
        )
    )

    assert _fields(payload, constraints=_LOCKS) == ("trigger_event.serves",)
    assert "neither an accepted author constraint nor an accepted obligation" in (
        _messages(payload, constraints=_LOCKS)[0]
    )


def test_a_host_accepted_obligation_id_is_a_legal_handle() -> None:
    payload = _volume_with_stage_windows(
        trigger_event=_stage_entry(
            "已接纳义务的推进", "301-320", "progression", "obligation.vol4.1"
        )
    )

    assert (
        volume_stage_window_defects(
            payload, constraints=_LOCKS, accepted_obligation_ids=frozenset({"obligation.vol4.1"})
        )
        == ()
    )


def test_a_role_that_does_not_serve_that_constraint_kind_is_refused() -> None:
    payload = _volume_with_stage_windows(
        first_cost=_stage_entry("推进", "301-320", "progression", "lock.long-truth.vol4-hint")
    )

    assert _fields(payload, constraints=_LOCKS) == ("first_cost.serves",)
    assert "reveal_window responsibility but a progression stage" in (
        _messages(payload, constraints=_LOCKS)[0]
    )


def test_the_candidates_own_declaration_is_not_the_authority() -> None:
    """A volume cannot legalise an early hint by declaring a boundary itself."""

    payload = _volume_with_stage_windows(
        midpoint_reversal=_stage_entry(
            "349 章暗示", "349-360", "hint", "lock.long-truth.vol4-hint"
        ),
        reveal_window=[{"summary": "自述锁", "not_before_chapter": 300}],
    )

    assert _fields(payload, constraints=_LOCKS) == ("midpoint_reversal.window",)
    # Without the trusted catalogue the handle cannot be resolved at all, and that is
    # refused too instead of being silently accepted.
    assert _fields(payload) == ("midpoint_reversal.serves",)


def test_free_text_narrative_slots_cannot_be_checked_and_are_refused() -> None:
    payload = _volume_with_stage_windows(opening_state="陆沉舟抵达钧炉城")

    assert _fields(payload, constraints=_LOCKS) == ("opening_state.description",)


def test_setup_stages_before_the_lock_stay_legal() -> None:
    payload = _volume_with_stage_windows(
        first_escalation=_stage_entry("不可解释的异常", "320-340", "setup"),
        second_escalation=_stage_entry("暗线推进", "320-340", "setup"),
    )

    assert volume_stage_window_defects(payload, constraints=_LOCKS) == ()


def test_a_setup_stage_may_name_the_responsibility_without_a_boundary_check() -> None:
    payload = _volume_with_stage_windows(
        first_cost=_stage_entry("埋下代价的种子", "301-320", "setup", "lock.long-truth.vol4-hint")
    )

    assert volume_stage_window_defects(payload, constraints=_LOCKS) == ()


def test_the_rendered_bracket_form_of_a_handle_resolves() -> None:
    """The planner context prints ``[handle]``; brackets are formatting, not identity."""

    payload = _volume_with_stage_windows(
        midpoint_reversal=_stage_entry(
            "陆远旧日行迹的暗示", "350-370", "hint", "[lock.long-truth.vol4-hint]"
        )
    )

    assert volume_stage_window_defects(payload, constraints=_LOCKS) == ()


def test_a_progression_without_a_served_responsibility_is_allowed() -> None:
    """A stage that reaches nothing locked must not be forced to cite a constraint."""

    payload = _volume_with_stage_windows(
        first_escalation=_stage_entry("常规推进", "320-340", "progression")
    )

    assert volume_stage_window_defects(payload, constraints=_LOCKS) == ()


def test_a_disclosure_without_a_served_responsibility_is_refused() -> None:
    payload = _volume_with_stage_windows(
        second_escalation=_stage_entry("暗示长程真相", "350-360", "hint")
    )

    assert _fields(payload, constraints=_LOCKS) == ("second_escalation.serves",)
    assert "must name the host-accepted responsibility" in _messages(
        payload, constraints=_LOCKS
    )[0]


def test_stage_windows_must_stay_inside_their_volume() -> None:
    payload = _volume_with_stage_windows(
        volume_climax=_stage_entry("卷高潮", "380-420", "payoff", "lock.long-truth.vol4-hint")
    )

    assert "volume_climax.window ends after its volume scope" in _messages(
        payload, constraints=_LOCKS
    )


def test_the_window_role_is_validated_against_the_four_actions() -> None:
    payload = _volume_with_stage_windows(
        ending_state=_stage_entry("结束状态", "380-400", "forbidden_reveal")
    )

    assert _fields(payload, constraints=_LOCKS) == ("ending_state.role",)


def test_a_stage_window_violation_reaches_host_review_with_a_targeted_demand() -> None:
    """The lock becomes a field-level finding the planner can be pointed at."""

    payload = _volume_payload(
        **{
            "chapter_start": 301,
            "chapter_end": 400,
            "midpoint_reversal": {
                "description": "揭示陆远曾在此开辟断星宫",
                "window": "301-340",
                "role": "payoff",
                "serves": "lock.long-truth.vol4-hint",
            },
        }
    )
    target = json.dumps(
        {
            "expected_volume_count": 1,
            "target_chapters": 400,
            "items": [{"item_id": "vol_04", "kind": "arc_volume", "payload": payload}],
        }
    )

    review = apply_host_plan_review_constraints(
        PlanReviewDraft(
            target_kind=ReviewTargetKind.PLAN_PROPOSAL,
            decision=ReviewDecision.ACCEPT,
            issues=(),
        ),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=target,
        mode=AgentMode.ARC_VOLUME,
        author_constraints=_LOCKS,
    )

    assert review.decision is ReviewDecision.REVISE
    blocking = [issue for issue in review.issues if issue.blocking]
    assert any(issue.kind.value == "volume_stage_window_violation" for issue in blocking)
    assert review.revision_instruction is not None
    assert "vol_04.midpoint_reversal.window" in review.revision_instruction


def test_the_arc_volume_contract_states_the_stage_window_shape() -> None:
    from novel_agent.runtime.production_bootstrap import PACKAGE_ROOT

    for name, root in (
        ("stage4_planner_arc_volume_v1.md", "prompts"),
        ("arc_volume_planning_v1.md", "skills"),
    ):
        text = (PACKAGE_ROOT / root / name).read_text(encoding="utf-8")
        assert "卷阶段窗口契约" in text, name
        assert "setup|hint|progression|payoff" in text, name
        assert "serves" in text, name
        assert "不得早于该卷声明的任何" not in text, name


def test_the_reviewer_prompt_keeps_the_prose_semantic_rule() -> None:
    from novel_agent.runtime.production_bootstrap import PACKAGE_ROOT

    text = (PACKAGE_ROOT / "prompts" / "plan_reviewer_v1.md").read_text(encoding="utf-8")

    assert "VOLUME_STAGE_WINDOW_VIOLATION" in text
    assert "把实质揭露改标为 `setup`" in text
