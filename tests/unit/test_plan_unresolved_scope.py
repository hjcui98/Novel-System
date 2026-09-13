"""R1.5: an advisory that questions a window must state that window.

The frozen v6 eight-volume candidate carried three ``UNSPECIFIED`` advisories that
asked whether a reveal boundary was respected (301-350 / 351-400, 401-430, 501-600)
while declaring ``affected_chapters: []``.  Nothing could then check the uncertainty
at the affected chapter, and the questions were effectively collapsed into a single
plan-wide "unspecified" advisory.
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

# The exact frozen v6 advisory payloads.
_V6_ADVISORIES = [
    {
        "affected_chapters": [],
        "allowed_assumptions": [],
        "blocking": False,
        "forbidden_assumptions": ["不得把该未决记忆缺口当作已证实事实"],
        "issue_id": "plan-issue.memory-gap.0",
        "kind": "UNSPECIFIED",
        "resolution_owner": "PLANNER",
        "summary": (
            "作者约束规定第四卷前半（301-350）不得揭露，后半（351-400）可暗示，"
            "需确认 vol_04 的 reveal_window 是否明确区分。"
        ),
    },
    {
        "affected_chapters": [],
        "allowed_assumptions": [],
        "blocking": False,
        "forbidden_assumptions": ["不得把该未决记忆缺口当作已证实事实"],
        "issue_id": "plan-issue.memory-gap.1",
        "kind": "UNSPECIFIED",
        "resolution_owner": "PLANNER",
        "summary": "作者约束规定第五卷起正式推进，但 vol_05 的 setup_window 为 401-430。",
    },
    {
        "affected_chapters": [],
        "allowed_assumptions": [],
        "blocking": False,
        "forbidden_assumptions": ["不得把该未决记忆缺口当作已证实事实"],
        "issue_id": "plan-issue.memory-gap.2",
        "kind": "UNSPECIFIED",
        "resolution_owner": "PLANNER",
        "summary": "vol_06（501-600章）是否过早完成完整揭露，导致 vol_07/08 缺乏推进空间？",
    },
]


def _review(unresolved: list[dict[str, object]]) -> PlanReviewDraft:
    payload = json.dumps(
        {
            "items": [
                {
                    "item_id": "vol_01",
                    "kind": "arc_volume",
                    "payload": {"plan_level": "arc_volume", "chapter_start": 1, "chapter_end": 100},
                }
            ],
            "unresolved": unresolved,
        }
    )
    return apply_host_plan_review_constraints(
        PlanReviewDraft(
            target_kind=ReviewTargetKind.PLAN_PROPOSAL,
            decision=ReviewDecision.ACCEPT,
            issues=(),
        ),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=payload,
        mode=AgentMode.ARC_VOLUME,
    )


def test_frozen_v6_advisories_without_scope_are_revise() -> None:
    review = _review([dict(item) for item in _V6_ADVISORIES])

    assert review.decision is ReviewDecision.REVISE
    scoped = [issue for issue in review.issues if issue.kind.value == "unresolved_scope_missing"]
    assert len(scoped) == 3
    assert all("questions chapters" in issue.summary for issue in scoped)
    assert {issue.affected_item_ids[0].root for issue in scoped} == {
        "plan-issue.memory-gap.0",
        "plan-issue.memory-gap.1",
        "plan-issue.memory-gap.2",
    }


def test_advisory_with_precise_scope_stays_advisory() -> None:
    scoped = dict(_V6_ADVISORIES[0])
    scoped["affected_chapters"] = [301, 350, 351, 400]
    review = _review([scoped])

    assert not any(issue.kind.value == "unresolved_scope_missing" for issue in review.issues)


def test_advisory_without_any_chapter_question_needs_no_scope() -> None:
    review = _review(
        [
            {
                "affected_chapters": [],
                "blocking": False,
                "forbidden_assumptions": ["不得把该未决记忆缺口当作已证实事实"],
                "issue_id": "plan-issue.style.0",
                "kind": "UNSPECIFIED",
                "summary": "叙述人称偏好尚未最终确认，但不影响本章义务。",
            }
        ]
    )

    assert not any(issue.kind.value == "unresolved_scope_missing" for issue in review.issues)


def test_blocking_advisory_still_blocks_regardless_of_scope() -> None:
    blocking = dict(_V6_ADVISORIES[1])
    blocking["blocking"] = True
    review = _review([blocking])

    assert review.decision is ReviewDecision.REVISE
    assert any(issue.kind.value == "blocking_unresolved" for issue in review.issues)


def test_a_revision_is_told_the_structured_fields_the_host_requires() -> None:
    """Prose cannot fill a field: the revision instruction names the exact fields."""

    payload = json.dumps(
        {
            "items": [],
            "unresolved": [
                {
                    "issue_id": "plan-issue.draft.scope.0",
                    "kind": "UNSPECIFIED",
                    "blocking": False,
                    "affected_chapters": [],
                    "summary": "第四卷（第301-400章）的伏笔窗口是否合规？",
                }
            ],
            "coverage": 1.0,
        },
        ensure_ascii=False,
    )
    draft = PlanReviewDraft(
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        decision=ReviewDecision.ACCEPT,
        issues=(),
    )

    reviewed = apply_host_plan_review_constraints(
        draft,
        mode=AgentMode.ARC_VOLUME,
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=payload,
        expected_volume_count=None,
        expected_target_chapters=None,
        accepted_obligation_ids=frozenset(),
        author_constraints=(),
    )

    assert reviewed.decision is ReviewDecision.REVISE
    assert any(issue.kind is ReviewIssueKind.UNRESOLVED_SCOPE_MISSING for issue in reviewed.issues)
    assert reviewed.revision_instruction is not None
    assert "HOST_REQUIRED_FIELDS:" in reviewed.revision_instruction
    assert "affected_chapters" in reviewed.revision_instruction


def test_every_planner_contract_states_the_advisory_scope_rule() -> None:
    """The gate demands a field, so every planning mode must be told to declare it.

    A real ARC_VOLUME attempt revised four times against the same three
    UNRESOLVED_SCOPE_MISSING findings: the prompts never mentioned
    ``affected_chapters``, so the model could not know the host would check it.
    """

    from novel_agent.runtime.production_bootstrap import PACKAGE_ROOT

    for name in (
        "stage4_planner_arc_volume_v1.md",
        "planner_story_v1.md",
        "planner_chapter_set_v1.md",
    ):
        text = (PACKAGE_ROOT / "prompts" / name).read_text(encoding="utf-8")
        assert "affected_chapters" in text, name
        assert "UNRESOLVED_SCOPE_MISSING" in text, name
    skill = (PACKAGE_ROOT / "skills" / "arc_volume_planning_v1.md").read_text(encoding="utf-8")
    assert "affected_chapters" in skill


def test_the_planner_output_budget_covers_the_mandated_volume_grid() -> None:
    """Eight volumes with 19 non-empty keys each must fit one planner response.

    A real ARC_VOLUME attempt was truncated at exactly the 8 000-token cap with a
    complete-but-unfinished eight-volume proposal, so the run could not commit G0.
    """

    from novel_agent.runtime.production_bootstrap import load_production_assembly_spec

    spec = load_production_assembly_spec()
    # A volume now declares a window and a role for each of its ten narrative
    # slots, so the eight-volume grid needs headroom beyond the ~9.5k tokens a free
    # text grid measured.
    assert spec.model_policy.default_output_limit >= 16_000
