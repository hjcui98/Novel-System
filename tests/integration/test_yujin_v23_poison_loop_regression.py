"""N3 regression: the v23 poison loop is stopped by the verified-finding contract.

The frozen v23 ARC_VOLUME task reached attempt 24 and a ``poison_loop`` failure
class.  Its terminal artifacts record why: the final ``plan_review`` settled
``decision=revise`` with ``issues=[]``, carrying only a prose instruction --

    修正 vol-4 中 midpoint_reversal 与 volume_climax 的语义越界：将
    vol-4.payload.midpoint_reversal.description 中"发现黑月坠世并非实验失控的异常线索"
    改为"发现九位圣座存亡的异常线索"；…

The instruction is concrete and even names the fields, but it is not a structured
finding, so the host could neither verify nor enforce it.  Every revision therefore
rewrote the whole plan, the demand neither closed nor was refused, and the loop kept
buying attempts until it was stopped by force.

These tests replay that exact review through the fixed path.  They are deterministic
and read the real artifact, so the input is the artifact that actually caused the
loop rather than a reconstruction of it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_agent.domain.plan_composition import (
    blocking_issue_identity,
    compose_scoped_revision,
    issue_identity_seed,
    progress_against,
    revision_scope,
)
from novel_agent.domain.planning import (
    PlanReview,
    PlanReviewDraft,
    ReviewDecision,
    ReviewTargetKind,
)
from novel_agent.domain.stage2 import PlanProposal
from tests.integration.test_yujin_d0_frozen_candidate_chain import (
    FROZEN_CANDIDATE,
    _proposal_from_frozen,
)

V23_OBJECTS = Path("/home/cuihengjia/agent/novel/NS/yujin-jiuxu-v23/objects/sha256")
# The review carried by the terminal `terminal.review_revision_required` event.
POISON_LOOP_REVIEW_PREFIX = "efd59bbf2"

pytestmark = pytest.mark.skipif(
    not FROZEN_CANDIDATE.exists() or not V23_OBJECTS.exists(),
    reason="the frozen v23 development input is not present",
)


def _terminal_review_payload() -> dict:
    """The real review artifact that ended the v23 ARC_VOLUME attempt."""

    hits = [
        path
        for path in V23_OBJECTS.rglob("*")
        if path.is_file() and path.name.startswith(POISON_LOOP_REVIEW_PREFIX)
    ]
    assert hits, "the terminal v23 review artifact is missing"
    return json.loads(hits[0].read_text(encoding="utf-8"))


def test_the_v23_loop_review_is_a_revise_with_no_structured_finding() -> None:
    """The premise of the regression, read from the artifact."""

    payload = _terminal_review_payload()

    assert payload["decision"] == "revise"
    assert payload["issues"] == []
    assert payload["target_kind"] == "plan_proposal"
    # It does name the fields -- in prose.
    instruction = payload["revision_instruction"]
    assert "vol-4.payload.midpoint_reversal.description" in instruction
    assert "vol-4.payload.volume_climax.description" in instruction
    assert "lock.long-truth.vol5-advance" in instruction


def test_the_v23_loop_review_grants_no_revision_scope() -> None:
    """Under the fixed contract the demand that drove 24 attempts authorises nothing."""

    payload = _terminal_review_payload()
    review = PlanReview.model_validate_json(
        json.dumps(
            {
                "review_id": "plan-review.v23.poison-loop",
                "target_kind": payload["target_kind"],
                "target_artifact_ref": payload["target_artifact_ref"],
                "decision": payload["decision"],
                "issues": payload["issues"],
                "revision_instruction": payload["revision_instruction"],
                "receipt": payload["receipt"],
            }
        ),
        strict=False,
    )

    scope = revision_scope(review)

    assert scope.targets == ()
    assert scope.additions == ()
    assert scope.removals == ()
    assert blocking_issue_identity(review) == ()


def test_a_scope_less_revise_cannot_rewrite_the_frozen_plan() -> None:
    """The whole-plan rewrite the loop kept buying is now the parent, unchanged."""

    payload = _terminal_review_payload()
    candidate = _proposal_from_frozen()
    review = PlanReview.model_validate_json(
        json.dumps(
            {
                "review_id": "plan-review.v23.poison-loop",
                "target_kind": payload["target_kind"],
                "target_artifact_ref": payload["target_artifact_ref"],
                "decision": payload["decision"],
                "issues": payload["issues"],
                "revision_instruction": payload["revision_instruction"],
                "receipt": payload["receipt"],
            }
        ),
        strict=False,
    )
    # A model revision that "fixes" the prose demand by rewriting as it likes.
    rewritten = candidate.model_copy(
        update={
            "items": tuple(
                item.model_copy(
                    update={
                        "payload": {
                            **item.payload,
                            "midpoint_reversal": {
                                "description": "模型改写后的中点",
                                "window": "351-360",
                                "role": "hint",
                            },
                        }
                    }
                )
                for item in candidate.items
            ),
            "proposal_id": candidate.proposal_id,
        }
    )

    composed = compose_scoped_revision(candidate, rewritten, revision_scope(review))

    assert composed == candidate
    assert composed is not rewritten


def test_the_first_such_review_is_progress_and_the_second_is_not() -> None:
    """Why the loop stopped churning: the same demand no longer buys a new attempt.

    The first scope-less ``REVISE`` is not progress against an empty frontier, so
    the loop records it and asks for a usable review; a repeat of the same
    identity adds nothing.  This is the pair of answers the old guard could not
    give, because it compared free text that a model can reword indefinitely.
    """

    payload = _terminal_review_payload()
    review = PlanReview.model_validate_json(
        json.dumps(
            {
                "review_id": "plan-review.v23.poison-loop",
                "target_kind": payload["target_kind"],
                "target_artifact_ref": payload["target_artifact_ref"],
                "decision": payload["decision"],
                "issues": payload["issues"],
                "revision_instruction": payload["revision_instruction"],
                "receipt": payload["receipt"],
            }
        ),
        strict=False,
    )

    # An empty frontier has nothing to compare against.
    assert not progress_against((), review)
    # A frontier holding the same (empty) identity, with the same unmet condition,
    # is not progress either.
    assert not progress_against(issue_identity_seed(review, ()), review)


def test_the_model_could_have_expressed_the_same_demand_and_been_understood() -> None:
    """The demand is legitimate; only its shape was unusable.

    Handed the same edit as a grounded finding, the host derives exactly that
    field as the scope -- which is what the reviewer should have emitted, and what
    the fixed prompt asks for.
    """

    from novel_agent.domain.planning import PlanReviewIssue, ReviewIssueKind

    candidate = _proposal_from_frozen()
    vol4 = next(item for item in candidate.items if item.item_id.root == "vol-4")
    description = str(
        (vol4.payload.get("volume_climax") or {}).get("description") or ""  # type: ignore[union-attr]
    )
    assert description, "the frozen vol-4 climax has no description to cite"
    review = PlanReview.model_validate_json(
        json.dumps(
            {
                "review_id": "plan-review.v23.grounded",
                "target_kind": "plan_proposal",
                "target_artifact_ref": _terminal_review_payload()["target_artifact_ref"],
                "decision": "revise",
                "issues": [
                    PlanReviewIssue(
                        issue_id="issue.v23.vol4-reveal",
                        kind=ReviewIssueKind.CONTRADICTION,
                        summary="第四卷高潮暗示了属于第五卷的长程真相",
                        blocking=True,
                        affected_item_ids=("vol-4",),
                        proposed_target_item_ids=("vol-4",),
                        authorized_target_item_ids=("vol-4",),
                        field_path="volume_climax.description",
                        quote=description,
                        unmet_condition="第四卷只允许暗示九位圣座存亡，不得暗示黑月坠世真相",
                        constraint_id="lock.long-truth.vol4-hint",
                    ).model_dump(mode="json")
                ],
                "revision_instruction": "按已核验问题修订第四卷高潮的语义",
                "receipt": _terminal_review_payload()["receipt"],
            }
        ),
        strict=False,
    )

    scope = revision_scope(review)

    assert scope.targeted_item_ids == {"vol-4"}
    assert scope.target_for("vol-4") is not None
    assert scope.target_for("vol-4").field_paths == ("volume_climax.description",)  # type: ignore[union-attr]
    assert blocking_issue_identity(review) == (
        "contradiction|vol-4|volume_climax.description|lock.long-truth.vol4-hint",
    )


def test_the_host_overlay_refuses_the_scope_less_revise_before_the_planner() -> None:
    """The end the loop needed: no planner call is authorised.

    Read with the compiled author catalogue, so the only thing the overlay has to
    judge is the review itself.
    """

    from novel_agent.agents.plan_reviewer import apply_host_plan_review_constraints
    from novel_agent.domain.stage2 import AgentMode
    from tests.integration.test_yujin_d0_frozen_candidate_chain import _author_constraints

    payload = _terminal_review_payload()
    candidate: PlanProposal = _proposal_from_frozen()
    overlaid = apply_host_plan_review_constraints(
        PlanReviewDraft(
            target_kind=ReviewTargetKind.PLAN_PROPOSAL,
            decision=ReviewDecision(payload["decision"]),
            issues=(),
            revision_instruction=payload["revision_instruction"],
        ),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=candidate.model_dump_json(),
        mode=AgentMode.ARC_VOLUME,
        accepted_obligation_ids=frozenset(),
        accepted_obligation_windows={},
        author_constraints=_author_constraints(),  # type: ignore[arg-type]
    )

    assert not [issue for issue in overlaid.issues if issue.blocking]
    assert overlaid.decision is ReviewDecision.ACCEPT
    assert overlaid.revision_instruction is None
