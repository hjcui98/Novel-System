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


def _review(
    payload: str,
    *,
    mode: str,
    accepted_obligation_ids: frozenset[str] | None = None,
) -> PlanReviewDraft:
    return apply_host_plan_review_constraints(
        _draft(),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=payload,
        mode=AgentMode(mode),
        accepted_obligation_ids=accepted_obligation_ids,
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


def test_chapter_set_may_not_declare_a_durable_obligation_directly() -> None:
    """The declaration list is the same level rule as the legacy table."""

    review = _review(
        _payload(
            {
                "item_id": "plan-item.ch1",
                "kind": "chapter_goal",
                "payload": {
                    "chapter_index": 1,
                    "obligation_declarations": [
                        {
                            "kind": "objective",
                            "summary": "章级偷建长期义务",
                            "setup_window": "1-10",
                            "payoff_window": "80-100",
                        }
                    ],
                },
            }
        ),
        mode="chapter_set",
    )

    assert review.decision is ReviewDecision.REVISE
    assert any(
        "OBLIGATION_DECLARATION_FORBIDDEN" in issue.summary and issue.blocking
        for issue in review.issues
    )


def test_structured_action_on_an_undeclared_obligation_is_revise() -> None:
    """An action may reference the accepted catalogue, never invent an entry."""

    review = _review(
        _payload(
            {
                "item_id": "plan-item.ch1",
                "kind": "chapter_goal",
                "payload": {
                    "chapter_index": 1,
                    "obligation_actions": [
                        {
                            "obligation_id": "obligation.vol_99.7.objective",
                            "action": "SETUP",
                            "expected_delta": "不存在的义务",
                        }
                    ],
                },
            }
        ),
        mode="chapter_set",
        accepted_obligation_ids=frozenset({"obligation.vol_01.0.objective"}),
    )

    assert review.decision is ReviewDecision.REVISE
    assert any(
        "OBLIGATION_ACTION_UNDECLARED" in issue.summary
        and "obligation.vol_99.7.objective" in issue.summary
        and issue.blocking
        for issue in review.issues
    )


def test_structured_action_on_a_declared_obligation_still_passes() -> None:
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
        accepted_obligation_ids=frozenset({"obligation.vol_01.0.objective"}),
    )

    assert not any(issue.kind is ReviewIssueKind.OBLIGATION_CONTRACT for issue in review.issues)


def test_a_catalogue_is_not_required_to_flag_an_undeclared_action() -> None:
    """Without a trusted catalogue the host cannot claim an id is invented."""

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

    assert not any("UNDECLARED" in issue.summary for issue in review.issues)


def _constraint(constraint_id: str, text: str) -> object:
    from novel_agent.domain.artifacts import ArtifactRef
    from novel_agent.domain.author_constraints import (
        AuthorConstraint,
        AuthorConstraintCategory,
    )
    from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
    from novel_agent.services.content_addressing import content_id

    return AuthorConstraint(
        constraint_id=StableId(constraint_id),
        category=AuthorConstraintCategory.TIME_LOCK,
        text=text,
        source_ref=ArtifactRef(
            artifact_id=content_id({"probe": constraint_id}),
            byte_length=1,
            media_type="application/json",
            schema_version=SchemaVersion("1.0.0"),
        ),
        source_hash=ArtifactId(content_id({"probe": constraint_id}).root),
        not_before_chapter=101,
    )


def test_author_constraint_coverage_uses_the_frozen_catalogue_as_denominator() -> None:
    """A proposal that restates nothing must not score a perfect coverage."""

    constraints = (
        _constraint("author-constraint.time_lock.1", "斩星府内府资格不得早于第二卷"),
        _constraint("author-constraint.time_lock.2", "断星六号核心回收不得在第一卷完成"),
    )
    review = apply_host_plan_review_constraints(
        _draft(),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=_payload(
            {
                "item_id": "vol_01",
                "kind": "arc_volume",
                "payload": {
                    "plan_level": "arc_volume",
                    "chapter_start": 1,
                    "chapter_end": 100,
                },
            }
        ),
        mode=AgentMode.ARC_VOLUME,
        author_constraints=constraints,
    )

    assert review.coverage_evidence
    line = next(
        item for item in review.coverage_evidence if item.startswith("author_constraint_coverage")
    )
    assert line.startswith("author_constraint_coverage: 0/2")


def test_author_constraint_coverage_credits_a_restated_constraint() -> None:
    constraints = (_constraint("author-constraint.time_lock.1", "斩星府内府资格不得早于第二卷"),)
    review = apply_host_plan_review_constraints(
        _draft(),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=_payload(
            {
                "item_id": "vol_02",
                "kind": "arc_volume",
                "payload": {
                    "plan_level": "arc_volume",
                    "chapter_start": 101,
                    "chapter_end": 200,
                    "summary": "斩星府内府资格不得早于第二卷"
                    + "，"  # noqa: RUF001 - author text uses a fullwidth comma
                    + "本卷才开放内府推进",
                },
            }
        ),
        mode=AgentMode.ARC_VOLUME,
        author_constraints=constraints,
    )

    line = next(
        item for item in review.coverage_evidence if item.startswith("author_constraint_coverage")
    )
    assert line.startswith("author_constraint_coverage: 1/1")


def test_a_declaration_without_an_obligation_kind_is_revise() -> None:
    """The live STORY defect: kind=obligation with no obligation_kind.

    Host review accepted it and the commit then blocked with
    "obligation declaration has an unknown kind".
    """

    review = _review(
        _payload(
            {
                "item_id": "story.obligation.reveal_lock",
                "kind": "obligation",
                "payload": {
                    "title": "揭示义务：时间锁与进度锁",  # noqa: RUF001
                    "constraints": ["lock.copper-token.vol1-end: 铜铭须在第一卷末取得"],
                },
            }
        ),
        mode="story",
    )

    assert review.decision is ReviewDecision.REVISE
    assert any(
        "OBLIGATION_DECLARATION_UNREADABLE" in issue.summary and issue.blocking
        for issue in review.issues
    )


def test_an_unknown_obligation_kind_is_revise() -> None:
    review = _review(
        _payload(
            {
                "item_id": "story.obligation.reveal_lock",
                "kind": "obligation",
                "payload": {"obligation_kind": "reveal_lock", "summary": "揭示义务"},
            }
        ),
        mode="story",
    )

    assert any("OBLIGATION_DECLARATION_UNREADABLE" in issue.summary for issue in review.issues)


def test_a_legacy_responsibility_table_is_not_treated_as_a_direct_declaration() -> None:
    """The legacy table has its own check and must not demand an item-level kind."""

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

    assert not any("OBLIGATION_KIND" in issue.summary for issue in review.issues)


def test_review_reads_the_world_of_the_reviewed_commit_not_assembly_time() -> None:
    """A run that committed new obligations must be reviewed against them.

    The reviewer component is built once per run, so a World reference frozen at
    assembly time keeps pointing at the startup catalogue.  An STORY or ARC_VOLUME
    commit adds obligations; a lower-level review against the stale catalogue would
    then refuse a legitimately declared id.  The World is therefore resolved from
    the reviewed task's own basis commit, with the assembled root only as fallback.
    """

    from unittest.mock import Mock

    from novel_agent.agents.plan_reviewer import PlanReviewerAgent
    from novel_agent.domain.artifacts import ArtifactRef
    from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion

    version = SchemaVersion("1.0.0")

    def _ref(digest: str) -> ArtifactRef:
        return ArtifactRef(
            artifact_id=ArtifactId("sha256:" + digest * 64),
            media_type="application/vnd.novel-agent.world-root+json",
            byte_length=1,
            schema_version=version,
        )

    genesis = _ref("a")
    committed = _ref("b")
    reviewed_commit = CommitId("sha256:" + "2" * 64)

    reviewer = PlanReviewerAgent(
        Mock(),
        Mock(),
        accepted_world_ref=genesis,
        world_root_for_commit=lambda commit: committed if commit == reviewed_commit else None,
    )

    assert reviewer._world_ref_for(reviewed_commit) == committed
    # An unrelated or unknown commit keeps the assembled root instead of guessing.
    assert reviewer._world_ref_for(CommitId("sha256:" + "3" * 64)) == genesis
    assert reviewer._world_ref_for(None) == genesis
