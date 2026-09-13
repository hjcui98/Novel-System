"""N1 (2026-09-13 guidance): a review finding binds the candidate it names.

The frozen v23 review ``47f9a758`` was settled ``REVISE`` with ``issues=[]``.  It told
the planner that volume four "正式揭露" the long-range truth and therefore had to be
rewritten; the candidate's volume four never says that, and the phrase belongs to
volume five.  Because the finding carried no structured citation, the host could not
refute it and forwarded the prose to the planner, where one field-level finding
rewrote the whole eight-volume plan.

These tests pin the review-side contract that replaces that: a blocking finding
resolves against the *named item's own field value*, cross-item borrowing does not
establish it, and every host return path keeps the verified result.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_agent.agents.plan_reviewer import (
    _field_path_segments,
    _items_by_id,
    _quote_matches,
    _resolve_field_path,
    apply_host_plan_review_constraints,
)
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.author_constraints import (
    AuthorConstraint,
    AuthorConstraintCategory,
)
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.planning import (
    PlanReviewDraft,
    PlanReviewIssue,
    ReviewCitationFailure,
    ReviewDecision,
    ReviewIssueKind,
    ReviewTargetKind,
)
from novel_agent.domain.stage2 import AgentMode
from novel_agent.services.content_addressing import content_id

# The frozen v23 candidate.  Read-only development-diagnostic input: the file is the
# immutable object of the earlier run and is never rewritten by these tests.
_FROZEN_CANDIDATE = Path(
    "/home/cuihengjia/agent/novel/NS/yujin-jiuxu-v23/objects/sha256/f5/"
    "f542293743e518e05b1665e4e8f48c3a7a9bb091c653ba09b52906c96913e2ae"
)


def _issue(
    *,
    item_ids: tuple[str, ...] = ("vol-4",),
    field_path: str | None = "volume_climax.description",
    quote: str | None = "正式揭露",
    unmet_condition: str | None = "第四卷不得正式揭露长程真相",
    kind: ReviewIssueKind = ReviewIssueKind.CONTRADICTION,
    constraint_id: str | None = None,
    blocking: bool = True,
) -> PlanReviewIssue:
    return PlanReviewIssue(
        issue_id=StableId("issue.probe"),
        kind=kind,
        summary="第四卷提前正式揭露了长程真相",
        blocking=blocking,
        affected_item_ids=tuple(StableId(item) for item in item_ids),
        field_path=field_path,
        quote=quote,
        unmet_condition=unmet_condition,
        constraint_id=constraint_id,
    )


def _draft(
    *issues: PlanReviewIssue, decision: ReviewDecision = ReviewDecision.REVISE
) -> PlanReviewDraft:
    return PlanReviewDraft(
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        decision=decision,
        issues=issues,
        revision_instruction="按审校意见修改" if decision is ReviewDecision.REVISE else None,
    )


def _payload() -> str:
    """A two-volume candidate with no mechanical defect of its own.

    Every item fills the required volume slots, its narrative stage entries each
    declare a window, a role and the frozen responsibility they serve, and the
    windows sit inside both the volume scope and the served lock's boundary.  The
    only findings a test therefore observes are the ones it injected on purpose.
    """

    def stage(
        description: str, window: str, role: str, serves: str | None = None
    ) -> dict[str, object]:
        # A `setup` stage legitimately serves nothing, so `serves` is optional and is
        # omitted rather than written as an empty string.
        entry: dict[str, object] = {
            "description": description,
            "window": window,
            "role": role,
        }
        if serves is not None:
            entry["serves"] = serves
        return entry

    def volume(
        *, start: int, end: int, lock: str, unlock: int, climax: dict[str, object]
    ) -> dict[str, object]:
        # The ten narrative slots are stage entries; the remaining required slots stay
        # free text, which the contract treats as volume-scoped by definition.
        #
        # The early slots plant rather than disclose, so they may reference a locked
        # responsibility and still run before its boundary -- that is what planting is
        # for.  Only ``midpoint_reversal`` reaches the responsibility's content and so
        # only it has to sit inside the lock's window.
        early = ("opening_state", "trigger_event", "first_escalation", "first_cost")
        late = ("second_escalation", "climax_cost", "ending_state", "next_volume_hook")
        narrative = {
            key: stage(f"{key} 描述", f"{start}-{start + 5}", "setup", lock) for key in early
        }
        narrative.update(
            {key: stage(f"{key} 描述", f"{end - 9}-{end - 5}", "progression", lock) for key in late}
        )
        return {
            "plan_level": "arc_volume",
            "chapter_start": start,
            "chapter_end": end,
            "midpoint_reversal": stage("中点推进", f"{unlock}-{unlock + 9}", "progression", lock),
            "volume_climax": climax,
            "protagonist_arc": "主角弧线",
            "supporting_arc": "配角弧线",
            "faction_arc": "势力弧线",
            "capability_ceiling": "能力上限",
            "equipment_ceiling": "装备上限",
            "entry_conditions": "入卷条件",
            "exit_conditions": "出卷条件",
            "reveal_window": "本卷揭露范围",
            "obligation_plan": [
                {
                    "kind": "objective",
                    "summary": "本卷推进的责任",
                    "setup_window": f"{start}-{start + 20}",
                    "progress_windows": [f"{start + 21}-{end - 11}"],
                    "payoff_window": f"{end - 10}-{end}",
                }
            ],
            **narrative,
        }

    return json.dumps(
        {
            "expected_volume_count": 2,
            "target_chapters": 200,
            "items": [
                {
                    "item_id": "vol-4",
                    "kind": "arc_volume",
                    "payload": volume(
                        start=1,
                        end=100,
                        lock="lock.long-truth.vol4-hint",
                        unlock=51,
                        climax=stage(
                            "陆沉舟获取第四碎片，出现无法解读的星门信号。",  # noqa: RUF001
                            "91-100",
                            "hint",
                            "lock.long-truth.vol4-hint",
                        ),
                    ),
                },
                {
                    "item_id": "vol-5",
                    "kind": "arc_volume",
                    "payload": volume(
                        start=101,
                        end=200,
                        lock="lock.long-truth.vol5-advance",
                        unlock=151,
                        climax=stage(
                            "激活水晶中的真相线索，正式揭露门被从对面推开。",  # noqa: RUF001
                            "191-200",
                            "payoff",
                            "lock.long-truth.vol5-advance",
                        ),
                    ),
                },
            ],
        }
    )


def _locks() -> tuple[AuthorConstraint, ...]:
    """The two frozen long-range locks the candidate's stage entries serve."""

    def lock(constraint_id: str, key: str, *, not_before: int) -> AuthorConstraint:
        return AuthorConstraint(
            constraint_id=StableId(constraint_id),
            category=AuthorConstraintCategory.TIME_LOCK,
            text=f"{key} 不得早于第 {not_before} 章",
            constraint_key=key,
            source_ref=ArtifactRef(
                artifact_id=content_id({"lock": key}),
                byte_length=1,
                media_type="application/json",
                schema_version=SchemaVersion("1.0.0"),
            ),
            source_hash=ArtifactId(content_id({"lock": key}).root),
            not_before_chapter=not_before,
        )

    return (
        lock("author-constraint.time_lock.4", "lock.long-truth.vol4-hint", not_before=51),
        lock("author-constraint.time_lock.5", "lock.long-truth.vol5-advance", not_before=151),
    )


def _review(
    draft: PlanReviewDraft,
    payload: str | None = None,
    *,
    constraints: tuple[AuthorConstraint, ...] | None = None,
) -> PlanReviewDraft:
    """Run the host gate with a complete trusted catalogue for the fixture.

    N2 requires the accepted-obligation catalogue to reach production review, so a
    review that supplies one must supply its windows too.  The fixture's stage
    entries serve author locks, so the obligation catalogue it declares is
    deliberately empty -- a readable statement, not an unknown one.
    """

    active = _locks() if constraints is None else constraints
    return apply_host_plan_review_constraints(
        draft,
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=payload if payload is not None else _payload(),
        mode=AgentMode.ARC_VOLUME,
        accepted_obligation_ids=frozenset(),
        accepted_obligation_windows={},
        author_constraints=active,
    )


def _model_blocking(reviewed: PlanReviewDraft) -> list[PlanReviewIssue]:
    """The blocking findings that came from the review itself, not from the host."""

    return [item for item in reviewed.issues if item.blocking and not item.host_issued]


# --- V01: structured location, and cross-item borrowing does not establish a claim -


def test_a_quote_living_only_in_another_volume_does_not_establish_the_finding() -> None:
    """The frozen misreading: volume four is accused with volume five's sentence."""

    reviewed = _review(
        _draft(
            _issue(
                item_ids=("vol-4",),
                field_path="volume_climax.description",
                quote="正式揭露门被从对面推开",
                unmet_condition="第四卷不得正式揭露长程真相",
            )
        )
    )

    assert reviewed.verification_failures
    assert ReviewCitationFailure.VALUE_NOT_IN_FIELD in reviewed.verification_failures[0]
    assert _model_blocking(reviewed) == []


def test_the_same_quote_in_the_named_volume_stays_blocking() -> None:
    reviewed = _review(
        _draft(
            _issue(
                item_ids=("vol-5",),
                field_path="volume_climax.description",
                quote="正式揭露门被从对面推开",
                unmet_condition="第五卷不得在此之前推进",
            )
        )
    )

    assert reviewed.verification_failures == ()
    assert reviewed.decision is ReviewDecision.REVISE
    assert len(_model_blocking(reviewed)) == 1


def test_a_nested_field_path_is_resolved_inside_the_named_item() -> None:
    reviewed = _review(
        _draft(
            _issue(
                item_ids=("vol-4",),
                field_path="midpoint_reversal.window",
                quote="51-60",
                unmet_condition="窗口必须满足该责任的边界",
            )
        )
    )

    assert reviewed.verification_failures == ()
    assert len(_model_blocking(reviewed)) == 1


def test_a_numeric_value_is_matched_by_its_own_representation() -> None:
    """A structured field the candidate wrote as ``351-360`` is real evidence."""

    payload = json.dumps(
        {
            "expected_volume_count": 1,
            "target_chapters": 400,
            "items": [
                {
                    "item_id": "vol-4",
                    "kind": "arc_volume",
                    "payload": {
                        "plan_level": "arc_volume",
                        "chapter_start": 301,
                        "chapter_end": 400,
                        "not_before_chapter": 350,
                    },
                }
            ],
        }
    )
    reviewed = _review(
        _draft(
            _issue(
                item_ids=("vol-4",),
                field_path="not_before_chapter",
                quote="350",
                unmet_condition="边界必须与冻结作者锁一致",
            )
        ),
        payload,
    )

    assert reviewed.verification_failures == ()
    assert len(_model_blocking(reviewed)) == 1


def test_an_unknown_item_or_field_is_refused_with_its_own_reason() -> None:
    missing_item = _review(_draft(_issue(item_ids=("vol-99",))))
    missing_field = _review(_draft(_issue(field_path="volume_climax.nope", quote="x")))

    assert ReviewCitationFailure.ITEM_NOT_FOUND in missing_item.verification_failures[0]
    assert ReviewCitationFailure.FIELD_NOT_FOUND in missing_field.verification_failures[0]


def test_a_field_path_grammar_is_small_and_never_evaluated() -> None:
    """No expression from the model is ever evaluated as code or a path lookup."""

    assert _field_path_segments("midpoint_reversal.window") == (
        ("midpoint_reversal", None),
        ("window", None),
    )
    assert _field_path_segments("stages[2].window") == (("stages", 2), ("window", None))
    for rejected in ("", "__class__", "a b", "a..b", "[0]", "a[b]", "a\tb"):
        assert _field_path_segments(rejected) is None, rejected


def test_a_resolved_path_returns_the_containing_and_field_values() -> None:
    payload = {"outer": {"inner": [{"k": "v"}]}}

    parent, value = _resolve_field_path(payload, "outer.inner[0].k")  # type: ignore[misc]

    assert value == "v"
    assert parent == {"k": "v"}
    assert isinstance(_resolve_field_path(payload, "outer.nope"), str)


def test_quote_matching_treats_absent_values_as_absent() -> None:
    assert _quote_matches("a", "abc")
    assert _quote_matches("7", 7)
    assert not _quote_matches("8", 7)
    assert not _quote_matches("x", None)
    assert not _quote_matches("x", True)
    assert _quote_matches("b", ["a", "b"])
    assert _quote_matches("b", {"k": "b"})


# --- V02: every host return path keeps the verified result -------------------------


def test_an_unverified_finding_is_refused_even_with_no_host_finding() -> None:
    """The early-return defect: `if not extra: return draft` skipped verification."""

    reviewed = _review(_draft(_issue(quote="候选里没有的一句话")))

    assert reviewed.verification_failures
    assert _model_blocking(reviewed) == []
    demoted = [item for item in reviewed.issues if not item.blocking and item.affected_item_ids]
    assert demoted
    assert all(item.summary.startswith("REVIEW_EVIDENCE_UNVERIFIED") for item in demoted)


def test_an_unverified_finding_is_refused_alongside_a_host_finding() -> None:
    """With a host finding the old code did verify, so both paths must agree."""

    broken = json.dumps(
        {
            "expected_volume_count": 1,
            "target_chapters": 400,
            "items": [
                {
                    "item_id": "vol-4",
                    "kind": "arc_volume",
                    "payload": {
                        "plan_level": "arc_volume",
                        "chapter_start": 301,
                        "chapter_end": 400,
                        "volume_climax": {
                            "description": "描述",
                            "window": "381-390",
                            "role": "hint",
                            "serves": "lock.unknown",
                        },
                    },
                }
            ],
        }
    )
    reviewed = _review(_draft(_issue(quote="候选里没有的一句话")), broken)

    assert reviewed.verification_failures
    assert reviewed.decision is ReviewDecision.REVISE
    assert any(item.host_issued and item.blocking for item in reviewed.issues)
    assert not [item for item in reviewed.issues if item.blocking and not item.host_issued]


def test_coverage_only_does_not_drop_the_verified_result() -> None:
    """A host computed coverage line must not become a return path of its own."""

    reviewed = _review(_draft(_issue(quote="候选里没有的一句话")))

    assert _model_blocking(reviewed) == []
    assert reviewed.decision is ReviewDecision.ACCEPT
    assert reviewed.revision_instruction is None


def test_a_refuted_demand_never_reaches_the_revision_instruction() -> None:
    """The instruction is derived from verified findings, not from review prose."""

    reviewed = _review(
        _draft(
            _issue(quote="候选里没有的一句话"),
            _issue(
                item_ids=("vol-4",),
                field_path="midpoint_reversal.window",
                quote="51-60",
                unmet_condition="窗口与受信责任不符",
            ),
        )
    )

    assert reviewed.revision_instruction is not None
    assert "候选里没有的一句话" not in reviewed.revision_instruction
    assert "窗口与受信责任不符" in reviewed.revision_instruction
    assert "vol-4.midpoint_reversal.window" in reviewed.revision_instruction


def test_a_correct_finding_survives_next_to_a_refuted_one() -> None:
    """Requirement 7: a valid finding must not be deleted with the refuted one."""

    reviewed = _review(
        _draft(
            _issue(quote="候选里没有的一句话"),
            _issue(
                item_ids=("vol-5",),
                field_path="volume_climax.description",
                quote="正式揭露门被从对面推开",
                unmet_condition="第五卷的推进必须落在自己的责任窗口内",
            ),
        )
    )

    blocking = [item for item in reviewed.issues if item.blocking]
    assert len(blocking) == 1
    assert blocking[0].affected_item_ids[0].root == "vol-5"
    assert reviewed.decision is ReviewDecision.REVISE


# --- V03: the structured requirement, and historical readability -------------------


@pytest.mark.parametrize(
    ("field_path", "quote", "unmet_condition"),
    (
        (None, "正式揭露", "条件"),
        ("volume_climax.description", None, "条件"),
        ("volume_climax.description", "正式揭露", None),
    ),
)
def test_a_blocking_finding_without_its_full_citation_is_a_review_defect(
    field_path: str | None, quote: str | None, unmet_condition: str | None
) -> None:
    reviewed = _review(
        _draft(
            _issue(field_path=field_path, quote=quote, unmet_condition=unmet_condition),
        )
    )

    assert reviewed.verification_failures
    assert ReviewCitationFailure.EVIDENCE_FIELDS_MISSING in reviewed.verification_failures[0]
    assert _model_blocking(reviewed) == []


def test_a_blocking_finding_that_names_no_item_is_a_review_defect() -> None:
    reviewed = _review(_draft(_issue(item_ids=())))

    assert ReviewCitationFailure.EVIDENCE_FIELDS_MISSING in reviewed.verification_failures[0]


def test_a_constraint_outside_the_frozen_catalogue_is_not_applicable() -> None:
    from novel_agent.domain.artifacts import ArtifactRef
    from novel_agent.domain.author_constraints import (
        AuthorConstraint,
        AuthorConstraintCategory,
    )
    from novel_agent.domain.ids import ArtifactId, SchemaVersion
    from novel_agent.services.content_addressing import content_id

    constraint = AuthorConstraint(
        constraint_id=StableId("author-constraint.time_lock.1"),
        category=AuthorConstraintCategory.TIME_LOCK,
        text="斩星府内府资格不得早于第二卷",
        source_ref=ArtifactRef(
            artifact_id=content_id({"probe": 1}),
            byte_length=1,
            media_type="application/json",
            schema_version=SchemaVersion("1.0.0"),
        ),
        source_hash=ArtifactId(content_id({"probe": 1}).root),
        not_before_chapter=101,
    )
    reviewed = apply_host_plan_review_constraints(
        _draft(
            _issue(
                item_ids=("vol-4",),
                field_path="midpoint_reversal.window",
                quote="51-60",
                unmet_condition="必须满足该约束",
                constraint_id="author-constraint.time_lock.8",
            )
        ),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=_payload(),
        mode=AgentMode.ARC_VOLUME,
        author_constraints=(constraint,),
    )

    assert ReviewCitationFailure.CONSTRAINT_NOT_APPLICABLE in reviewed.verification_failures[0]


def test_a_finding_that_omits_the_constraint_is_not_forced_to_fabricate_one() -> None:
    """A general quality problem is not an author-constraint violation."""

    reviewed = _review(
        _draft(
            _issue(
                item_ids=("vol-5",),
                field_path="volume_climax.description",
                quote="正式揭露门被从对面推开",
                unmet_condition="第五卷与第六卷重复了同一次揭露",
            )
        )
    )

    assert reviewed.verification_failures == ()


def test_an_already_verified_review_is_not_re_verified_into_a_new_decision() -> None:
    """Replaying a verified review must be stable, not flip ACCEPT to a defect."""

    once = _review(
        _draft(
            _issue(quote="候选里没有的一句话"),
        )
    )
    twice = apply_host_plan_review_constraints(
        once,
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=_payload(),
        mode=AgentMode.ARC_VOLUME,
        accepted_obligation_ids=frozenset(),
        accepted_obligation_windows={},
        author_constraints=_locks(),
        verified_citations=True,
    )

    assert once.decision is ReviewDecision.ACCEPT
    assert twice.decision is ReviewDecision.ACCEPT
    assert [item.blocking for item in twice.issues] == [item.blocking for item in once.issues]
    assert twice.verification_failures == ()


def test_a_historical_draft_without_the_new_fields_still_reads() -> None:
    """Old artifacts must stay readable, without inheriting the new contract."""

    legacy = PlanReviewDraft.model_validate_json(
        '{"target_kind": "plan_proposal", "decision": "revise", "issues": [],'
        ' "revision_instruction": "修正 vol-4 的长程真相揭露节奏"}'
    )

    assert legacy.verification_failures == ()
    assert legacy.issues == ()


def test_a_legacy_issue_without_citation_fields_still_reads() -> None:
    legacy = PlanReviewIssue.model_validate_json(
        '{"issue_id": "issue.legacy", "kind": "contradiction", "summary": "旧审校意见",'
        ' "blocking": true, "affected_item_ids": ["vol-4"]}'
    )

    assert legacy.field_path is None
    assert legacy.quote is None
    assert legacy.host_issued is False


# --- the frozen input, read end to end --------------------------------------------


@pytest.mark.skipif(not _FROZEN_CANDIDATE.exists(), reason="frozen v23 candidate is not present")
def test_the_frozen_candidate_refutes_the_frozen_reviews_own_description() -> None:
    """The diagnostic input proves the misreading is real, not hypothetical.

    Review ``47f9a758`` described volume four as having "正式揭露" and "获取完整线索".
    The immutable candidate contains neither sentence in volume four; both the
    "正式揭露" wording and the zero-station record belong to later volumes.  A host
    that can resolve a citation refutes that demand from the artifact itself instead
    of spending a planner revision on it.
    """

    raw = _FROZEN_CANDIDATE.read_text(encoding="utf-8")
    items = _items_by_id(raw)
    assert sorted(items) == [f"vol-{index}" for index in range(1, 9)]

    vol4 = json.dumps(items["vol-4"], ensure_ascii=False)
    # "正式揭露" is the word the review attributed to volume four; it is absent there.
    assert "正式揭露" not in vol4
    assert "正式揭露" in json.dumps(items["vol-5"], ensure_ascii=False)
    # The zero-station record is volume five's stage, not volume four's.
    assert "读取到关于黑月坠世真相的初步线索" in json.dumps(items["vol-5"], ensure_ascii=False)
    assert "读取到关于黑月坠世真相的初步线索" not in vol4

    reviewed = _review(
        _draft(
            _issue(
                item_ids=("vol-4",),
                field_path="volume_climax.description",
                quote="正式揭露",
                unmet_condition="第四卷不得正式揭露长程真相",
            )
        ),
        raw,
    )

    assert reviewed.verification_failures
    assert _model_blocking(reviewed) == []


@pytest.mark.skipif(not _FROZEN_CANDIDATE.exists(), reason="frozen v23 candidate is not present")
def test_the_frozen_review_is_unverifiable_because_it_carries_no_citation() -> None:
    """The frozen review is exactly the shape the new contract must refuse.

    Its ``issues`` list is empty, so there is no structured citation to resolve and
    nothing the host can check; the prose instruction is the only content, and it is
    the prose that named the wrong volume.  Such a review cannot authorize a rewrite.
    """

    review = json.loads(
        Path(
            "/home/cuihengjia/agent/novel/NS/yujin-jiuxu-v23/objects/sha256/47/"
            "47f9a7584b6c4f744bb9064950577ad7b4d5f5d378f7b2304a0c16f4fc2a2ced"
        ).read_text(encoding="utf-8")
    )

    assert review["decision"] == "revise"
    assert review["issues"] == []
    assert "正式揭露" in review["revision_instruction"]

    draft = PlanReviewDraft.model_validate_json(
        json.dumps(
            {
                "target_kind": review["target_kind"],
                "decision": review["decision"],
                "issues": review["issues"],
                "revision_instruction": review["revision_instruction"],
            }
        )
    )
    reviewed = apply_host_plan_review_constraints(
        draft,
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=_FROZEN_CANDIDATE.read_text(encoding="utf-8"),
        mode=AgentMode.ARC_VOLUME,
    )

    # The review carried no citation, so it contributes no blocking finding at all:
    # the prose demand gains no authority it did not earn.  This candidate does have
    # its own independent mechanical defects -- its stage slots are still prose with
    # no declared window, role or served responsibility -- so the host still returns
    # REVISE, but on findings it decided and can name itself.
    assert _model_blocking(reviewed) == []
    assert reviewed.verification_failures == ()
    assert reviewed.decision is ReviewDecision.REVISE
    assert any(item.host_issued and item.blocking for item in reviewed.issues)
    assert "正式揭露" not in (reviewed.revision_instruction or "")


def test_a_reviewer_cannot_author_the_hosts_own_verdict() -> None:
    """A model-supplied ``verification_failures`` must not survive the host pass.

    The draft schema is generated from the draft model, so ``verification_failures``
    is offered to the model even though the prompt never asks for it.  A live
    ARC_VOLUME review put three notes about the planner's *questions* there, and
    because the target was an inquiry -- not a plan proposal -- every host guard
    returned early and the value survived.  ``PlanReviewer.invoke`` then raised
    ``PlanReviewerInvocationError``, parking the task as
    ``blocked / leaf_review_required`` on text the host had never verified and no
    retry could clear.  The field is a host verdict, so the host clears it first.
    """

    # The shape of the live value: the reviewer's own answer ids and prose.  The
    # original used fullwidth punctuation, which the project's lint refuses, so the
    # fixture keeps the structure and its key property -- text the host never
    # verified -- without the ambiguous characters.
    model_authored = (
        "a2 问题 '第四卷前半 201-350 章 是否仅允许埋设' 存在事实错误: "
        "第四卷前半应为 301-350 章, 而非 201-350 章.",
    )
    draft = PlanReviewDraft(
        target_kind=ReviewTargetKind.INQUIRY,
        decision=ReviewDecision.ACCEPT,
        issues=(),
        verification_failures=model_authored,
    )
    assert draft.verification_failures == model_authored

    for target_kind in (ReviewTargetKind.INQUIRY, ReviewTargetKind.PLAN_PROPOSAL):
        reviewed = apply_host_plan_review_constraints(
            draft,
            target_kind=target_kind,
            # An inquiry payload is not a plan proposal: the proposal guards must
            # not be what saves this, because the live failure took the early exit.
            target_payload='{"questions": [], "assumptions": []}',
            mode=AgentMode.ARC_VOLUME,
        )
        assert reviewed.verification_failures == (), target_kind
        # The reviewer's own decision is untouched: only the host's verdict is
        # cleared, so an ACCEPT is still an ACCEPT.
        assert reviewed.decision is ReviewDecision.ACCEPT


def test_a_plan_proposal_citation_failure_is_still_the_hosts_to_report() -> None:
    """Clearing the model's value must not clear the host's own finding."""

    ungrounded = _draft(_issue(quote="这段文字不在候选里")).model_copy(
        update={"verification_failures": ("model supplied: ignore me",)}
    )
    reviewed = apply_host_plan_review_constraints(
        ungrounded,
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=_FROZEN_CANDIDATE.read_text(encoding="utf-8"),
        mode=AgentMode.ARC_VOLUME,
    )

    assert reviewed.verification_failures, reviewed.verification_failures
    assert ReviewCitationFailure.VALUE_NOT_IN_FIELD in reviewed.verification_failures[0]
    assert "model supplied: ignore me" not in reviewed.verification_failures
