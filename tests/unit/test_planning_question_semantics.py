"""Regression coverage for the Planner question / memory boundary.

These cases reconstruct the reported G3 deadlock deterministically.  They are
rebuilt regression fixtures, not a replay of the original run: no production
artifact, Canon object or model response is asserted here.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from novel_agent.domain.ids import StableId
from novel_agent.domain.planning import (
    PlanningProvenance,
    PlanningQuestion,
    PlanningQuestionKind,
    PlanningReference,
)
from novel_agent.domain.planning_gap import (
    HISTORICAL_DEPENDENCY_UNRESOLVED,
    UNSUPPORTED_PLAN_PRECONDITION,
    DependencyExpectation,
    GapDisposition,
    QuestionPurpose,
    TrustedPlanningMode,
    TrustedQuestionIntent,
    VerifiedGapEvidence,
    classify_gap,
    disposition_diagnostic,
    resolve_question_semantics,
)


def _evidence(
    *,
    projection_exact: bool = True,
    positive_source_support: bool = False,
    positive_projection_support: bool = False,
    explicit_negative_support: bool = False,
    projection_expected: bool = True,
) -> VerifiedGapEvidence:
    return VerifiedGapEvidence(
        projection_exact=projection_exact,
        positive_source_support=positive_source_support,
        positive_projection_support=positive_projection_support,
        explicit_negative_support=explicit_negative_support,
        projection_expected=projection_expected,
    )


def _question(
    *,
    kind: PlanningQuestionKind = PlanningQuestionKind.FACT,
    blocking: bool = True,
    purpose: QuestionPurpose | None = None,
    expectation: DependencyExpectation | None = None,
) -> PlanningQuestion:
    return PlanningQuestion(
        question_id=StableId("question.g3.vanguard"),
        kind=kind,
        question="陆沉舟是否已经获得先遣队指派文书?",
        provenance=PlanningReference(provenance=PlanningProvenance.PLANNER_PROPOSED),
        goal_id=StableId("goal.g3.primary"),
        blocking=blocking,
        question_purpose=purpose,
        dependency_expectation=expectation,
    )


def test_planned_future_relation_is_not_retroactive_historical_evidence() -> None:
    """Q01: plan text describing a future recruitment is not a past world fact."""

    purpose, expectation = resolve_question_semantics(
        mode=TrustedPlanningMode.CHAPTER_SET,
        intent=TrustedQuestionIntent.REVIEWED_INQUIRY_QUESTION,
        kind_is_fact=True,
        blocking=True,
    )

    assert purpose is QuestionPurpose.DESIGN_FUTURE
    assert expectation is DependencyExpectation.NO_HISTORICAL_PRECONDITION
    assert (
        classify_gap(
            purpose=purpose,
            dependency_expectation=expectation,
            evidence=_evidence(),
        )
        is GapDisposition.PLAN_GENERATION
    )


def test_future_design_cannot_require_an_existing_fact() -> None:
    """Q07: a model may not relabel a real prerequisite as future design."""

    with pytest.raises(ValueError, match="historical prerequisite"):
        classify_gap(
            purpose=QuestionPurpose.DESIGN_FUTURE,
            dependency_expectation=DependencyExpectation.MUST_ESTABLISH_EXISTING_FACT,
            evidence=_evidence(),
        )
    with pytest.raises(ValidationError, match="designs the future"):
        _question(
            purpose=QuestionPurpose.DESIGN_FUTURE,
            expectation=DependencyExpectation.MUST_ESTABLISH_EXISTING_FACT,
        )


def test_name_mention_without_proposition_support_is_not_an_extraction_gap() -> None:
    """Q02: a paragraph mentioning the protagonist proves nothing on its own."""

    disposition = classify_gap(
        purpose=QuestionPurpose.VERIFY_HISTORY,
        dependency_expectation=DependencyExpectation.MUST_ESTABLISH_EXISTING_FACT,
        evidence=_evidence(),
    )

    assert disposition is GapDisposition.HISTORICAL_UNRESOLVED
    assert disposition_diagnostic(disposition) == HISTORICAL_DEPENDENCY_UNRESOLVED
    assert disposition is not GapDisposition.CANON_EXTRACTION_GAP


def test_sourced_fact_missing_from_projection_is_an_extraction_gap() -> None:
    """Q03: a real source-bound omission is still repairable."""

    disposition = classify_gap(
        purpose=QuestionPurpose.VERIFY_HISTORY,
        dependency_expectation=DependencyExpectation.MUST_ESTABLISH_EXISTING_FACT,
        evidence=_evidence(positive_source_support=True),
    )

    assert disposition is GapDisposition.CANON_EXTRACTION_GAP
    assert disposition_diagnostic(disposition) == "canon_extraction_gap"


def test_explicit_denial_is_a_sourced_answer_not_a_gap() -> None:
    """Q04: a sourced negative answer is evidence, never a fabricated relation."""

    disposition = classify_gap(
        purpose=QuestionPurpose.CHECK_CURRENT_STATE,
        dependency_expectation=DependencyExpectation.CHECK_STATUS,
        evidence=_evidence(explicit_negative_support=True),
    )

    assert disposition is GapDisposition.SUPPORTED_NEGATION


def test_explicit_denial_conflicts_with_a_required_positive_prerequisite() -> None:
    """Q06: when the plan insisted the fact already held, denial revises the plan."""

    disposition = classify_gap(
        purpose=QuestionPurpose.VERIFY_HISTORY,
        dependency_expectation=DependencyExpectation.MUST_ESTABLISH_EXISTING_FACT,
        evidence=_evidence(explicit_negative_support=True),
    )

    assert disposition is GapDisposition.PRECONDITION_CONFLICT
    assert disposition_diagnostic(disposition) == UNSUPPORTED_PLAN_PRECONDITION


def test_positive_and_negative_evidence_at_one_cutoff_is_a_conflict() -> None:
    """Q05: convenience must not pick a side when both sides are sourced."""

    disposition = classify_gap(
        purpose=QuestionPurpose.VERIFY_HISTORY,
        dependency_expectation=DependencyExpectation.CHECK_STATUS,
        evidence=_evidence(
            positive_source_support=True,
            explicit_negative_support=True,
        ),
    )

    assert disposition is GapDisposition.EVIDENCE_CONFLICT


def test_inexact_projection_is_refreshed_before_any_gap_conclusion() -> None:
    """A stale projection must be repaired before the source is blamed."""

    disposition = classify_gap(
        purpose=QuestionPurpose.VERIFY_HISTORY,
        dependency_expectation=DependencyExpectation.CHECK_STATUS,
        evidence=_evidence(
            projection_exact=False,
            positive_source_support=True,
        ),
    )

    assert disposition is GapDisposition.REFRESH_PROJECTION


def test_projection_answer_closes_the_question_without_source_repair() -> None:
    disposition = classify_gap(
        purpose=QuestionPurpose.CHECK_CURRENT_STATE,
        dependency_expectation=DependencyExpectation.CHECK_STATUS,
        evidence=_evidence(positive_projection_support=True),
    )

    assert disposition is GapDisposition.SUPPORTED_ANSWER


def test_unknown_is_never_a_negative_answer() -> None:
    """``unknown`` closes nothing: it stays an unresolved dependency."""

    disposition = classify_gap(
        purpose=QuestionPurpose.VERIFY_HISTORY,
        dependency_expectation=DependencyExpectation.CHECK_STATUS,
        evidence=_evidence(),
    )

    assert disposition is GapDisposition.HISTORICAL_UNRESOLVED


def test_classification_rejects_untyped_evidence_inputs() -> None:
    with pytest.raises(ValueError, match="reviewed question purpose"):
        classify_gap(
            purpose="verify_history",  # type: ignore[arg-type]
            dependency_expectation=DependencyExpectation.CHECK_STATUS,
            evidence=_evidence(),
        )
    with pytest.raises(ValueError, match="host-verified evidence"):
        classify_gap(
            purpose=QuestionPurpose.VERIFY_HISTORY,
            dependency_expectation=DependencyExpectation.CHECK_STATUS,
            evidence={"projection_exact": True},  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="must be a boolean"):
        classify_gap(
            purpose=QuestionPurpose.VERIFY_HISTORY,
            dependency_expectation=DependencyExpectation.CHECK_STATUS,
            evidence=VerifiedGapEvidence(
                projection_exact=True,
                positive_source_support=1,  # type: ignore[arg-type]
                positive_projection_support=False,
                explicit_negative_support=False,
                projection_expected=True,
            ),
        )


def test_reviewed_question_semantics_are_host_derived_per_mode() -> None:
    """The planning level, not the model, decides what a question can be."""

    assert resolve_question_semantics(
        mode=TrustedPlanningMode.STORY,
        intent=TrustedQuestionIntent.REVIEWED_INQUIRY_QUESTION,
        kind_is_fact=True,
        blocking=True,
    ) == (QuestionPurpose.CHECK_CURRENT_STATE, DependencyExpectation.MUST_ESTABLISH_EXISTING_FACT)
    assert resolve_question_semantics(
        mode=TrustedPlanningMode.ARC_VOLUME,
        intent=TrustedQuestionIntent.REVIEWED_INQUIRY_QUESTION,
        kind_is_fact=False,
        blocking=True,
    ) == (QuestionPurpose.VERIFY_HISTORY, DependencyExpectation.MUST_ESTABLISH_EXISTING_FACT)
    # An explicit Memory request is a history question at every level, because
    # the model already asked for prior evidence instead of designing a beat.
    assert resolve_question_semantics(
        mode=TrustedPlanningMode.CHAPTER,
        intent=TrustedQuestionIntent.REQUESTED_MEMORY_EVIDENCE,
        kind_is_fact=True,
        blocking=True,
    ) == (QuestionPurpose.VERIFY_HISTORY, DependencyExpectation.CHECK_STATUS)
    # A reviewer-asserted fact stays a hard prerequisite.
    assert resolve_question_semantics(
        mode=TrustedPlanningMode.CHAPTER_SET,
        intent=TrustedQuestionIntent.REVIEWER_GAP_QUESTION,
        kind_is_fact=True,
        blocking=True,
    ) == (QuestionPurpose.VERIFY_HISTORY, DependencyExpectation.MUST_ESTABLISH_EXISTING_FACT)
    assert resolve_question_semantics(
        mode=TrustedPlanningMode.PROJECT_BOOTSTRAP,
        intent=TrustedQuestionIntent.REVIEWED_INQUIRY_QUESTION,
        kind_is_fact=True,
        blocking=True,
    ) == (QuestionPurpose.DESIGN_FUTURE, DependencyExpectation.NO_HISTORICAL_PRECONDITION)


def test_non_blocking_question_checks_status_instead_of_asserting_a_fact() -> None:
    purpose, expectation = resolve_question_semantics(
        mode=TrustedPlanningMode.STORY,
        intent=TrustedQuestionIntent.REVIEWED_INQUIRY_QUESTION,
        kind_is_fact=False,
        blocking=False,
    )

    assert purpose is QuestionPurpose.VERIFY_HISTORY
    assert expectation is DependencyExpectation.CHECK_STATUS


def test_legacy_question_without_host_semantics_stays_readable() -> None:
    legacy = _question()

    assert legacy.question_purpose is None
    assert legacy.dependency_expectation is None
    assert legacy.origin_plan_item_id is None
    assert legacy.origin_field_path is None


def test_design_future_question_cannot_become_a_history_need() -> None:
    """The Need contract is the second place the boundary is enforced."""

    from novel_agent.domain.ids import CommitId, RunId, TaskId
    from novel_agent.domain.memory import (
        CandidatePool,
        NeedRisk,
        RequirementLevel,
        ResolutionPath,
        Stage1MemoryNeed,
        Stage1QueryIntent,
    )

    with pytest.raises(ValidationError, match="designs future content"):
        Stage1MemoryNeed(
            need_id=StableId("need.g3.future"),
            run_id=RunId("run.g3.future"),
            task_id=TaskId("task.g3.future"),
            base_commit=CommitId("sha256:" + "1" * 64),
            horizon_target=(6, 10),
            need_type="planner_fact",
            query_intent=Stage1QueryIntent.CURRENT_STATE,
            query_text="先遣队将怎样组建?",
            why_needed="design future content",
            risk_level=NeedRisk.MEDIUM,
            requirement=RequirementLevel.OPTIONAL,
            preferred_resolution_path=ResolutionPath.ANCHOR_FIRST,
            allowed_candidate_pools=(CandidatePool.ANCHOR,),
            stop_condition="never",
            question_purpose=QuestionPurpose.DESIGN_FUTURE.value,
        )
