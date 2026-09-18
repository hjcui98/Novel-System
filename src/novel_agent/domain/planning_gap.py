"""Pure disposition logic for a reviewed Planner question that did not close.

The Planner used to treat one situation as another: whenever a mandatory facet
stayed unsupported and some candidate had been retrieved, the run emitted a
Canon extraction repair.  That conflates four different things.

* The reviewed question was not a question about history at all; it designed a
  future event and therefore belongs to plan generation.
* The current projection is not exact, so nothing can be concluded yet.
* The frozen source really does contain the requested fact while the typed
  projection lacks it.  Only this case is a Canon extraction gap.
* No trusted source supports the requested proposition.  That is an unresolved
  historical dependency or, when the plan insisted the fact already held, an
  unsupported plan precondition.  It is never evidence of omission.

This module owns the classification.  It performs no IO, calls no model, and
stores no state.  Every boolean input is derived by the host from verified
receipts, and ``unknown`` is never promoted to a negative answer.

The classifier deliberately does not decide policy.  ``HISTORICAL_UNRESOLVED``
is not a pass: whether the plan may be revised to stop depending on the fact is
a source, authority, constraint and independent-review question owned by the
planning loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class QuestionPurpose(StrEnum):
    """Whether a reviewed question looks backwards, at now, or forwards."""

    VERIFY_HISTORY = "verify_history"
    CHECK_CURRENT_STATE = "check_current_state"
    DESIGN_FUTURE = "design_future"


class DependencyExpectation(StrEnum):
    """What the reviewed plan asserts about the question's answer."""

    MUST_ESTABLISH_EXISTING_FACT = "must_establish_existing_fact"
    CHECK_STATUS = "check_status"
    NO_HISTORICAL_PRECONDITION = "no_historical_precondition"


class GapDisposition(StrEnum):
    """The single handling a reviewed, unresolved question is routed to."""

    PLAN_GENERATION = "plan_generation"
    REFRESH_PROJECTION = "refresh_projection"
    SUPPORTED_ANSWER = "supported_answer"
    SUPPORTED_NEGATION = "supported_negation"
    CANON_EXTRACTION_GAP = "canon_extraction_gap"
    PRECONDITION_CONFLICT = "precondition_conflict"
    HISTORICAL_UNRESOLVED = "historical_unresolved"
    EVIDENCE_CONFLICT = "evidence_conflict"


@dataclass(frozen=True, slots=True)
class VerifiedGapEvidence:
    """Host-verified, cutoff-safe receipts about one reviewed question.

    ``projection_exact`` means the frozen projection matches the frozen source
    for the relevant root.  ``positive_source_support`` means the frozen text
    contains content that supports the requested proposition, not merely that a
    retrieval call was permitted or that a paragraph mentioned a participant.
    """

    projection_exact: bool
    positive_source_support: bool
    positive_projection_support: bool
    explicit_negative_support: bool
    projection_expected: bool


def _require_bool(name: str, value: bool) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean, not {type(value).__name__}")


def classify_gap(
    *,
    purpose: QuestionPurpose,
    dependency_expectation: DependencyExpectation,
    evidence: VerifiedGapEvidence,
) -> GapDisposition:
    """Route one reviewed question to exactly one disposition.

    The caller must already have checked the reviewed origin/authority binding.
    A model may not call this function's inputs into existence: every field of
    :class:`VerifiedGapEvidence` comes from host-verified receipts.
    """

    if not isinstance(purpose, QuestionPurpose):
        raise ValueError("gap classification requires a reviewed question purpose")
    if not isinstance(dependency_expectation, DependencyExpectation):
        raise ValueError("gap classification requires a reviewed dependency expectation")
    if not isinstance(evidence, VerifiedGapEvidence):
        raise ValueError("gap classification requires host-verified evidence")
    for name in (
        "projection_exact",
        "positive_source_support",
        "positive_projection_support",
        "explicit_negative_support",
        "projection_expected",
    ):
        _require_bool(name, getattr(evidence, name))

    must_be_existing = dependency_expectation is DependencyExpectation.MUST_ESTABLISH_EXISTING_FACT
    if purpose is QuestionPurpose.DESIGN_FUTURE:
        if must_be_existing:
            # A future design step cannot smuggle in a historical prerequisite
            # that must already be true.  Refuse instead of searching for an
            # answer that the reviewed material never asserted.
            raise ValueError(
                "future design cannot disguise a historical prerequisite as an existing fact"
            )
        return GapDisposition.PLAN_GENERATION

    if not evidence.projection_exact:
        return GapDisposition.REFRESH_PROJECTION

    has_positive = evidence.positive_source_support or evidence.positive_projection_support
    if has_positive and evidence.explicit_negative_support:
        return GapDisposition.EVIDENCE_CONFLICT
    if evidence.positive_projection_support:
        return GapDisposition.SUPPORTED_ANSWER
    if evidence.positive_source_support:
        if evidence.projection_expected:
            return GapDisposition.CANON_EXTRACTION_GAP
        return GapDisposition.SUPPORTED_ANSWER
    if evidence.explicit_negative_support:
        if must_be_existing:
            return GapDisposition.PRECONDITION_CONFLICT
        return GapDisposition.SUPPORTED_NEGATION
    return GapDisposition.HISTORICAL_UNRESOLVED


# Terminal diagnostics that replace the old unconditional extraction handoff.
HISTORICAL_DEPENDENCY_UNRESOLVED = "historical_dependency_unresolved"
UNSUPPORTED_PLAN_PRECONDITION = "unsupported_plan_precondition"


class TrustedPlanningMode(StrEnum):
    """The reviewed planning level a question was created at.

    Mirrors the Stage 4 Planner modes without importing them, so this module
    stays a leaf.  :func:`resolve_question_semantics` receives the host-bound
    mode, never a value read from model output.
    """

    PROJECT_BOOTSTRAP = "project_bootstrap"
    STORY = "story"
    ARC_VOLUME = "arc_volume"
    CHAPTER_SET = "chapter_set"
    CHAPTER = "chapter"
    SCENE = "scene"
    REPLAN = "replan"


class TrustedQuestionIntent(StrEnum):
    """What the host knows about why the question was asked."""

    #: The reviewed inquiry proposed the question during its own planning turn.
    REVIEWED_INQUIRY_QUESTION = "reviewed_inquiry_question"
    #: The model explicitly asked Memory for evidence before committing a plan.
    REQUESTED_MEMORY_EVIDENCE = "requested_memory_evidence"
    #: The independent plan reviewer raised the question against a candidate.
    REVIEWER_GAP_QUESTION = "reviewer_gap_question"


#: Planning levels whose questions design content that does not exist yet.
#: A question created here asks how the story should go, not what already
#: happened, so it can never become a historical Memory retrieval or a Canon
#: extraction repair.
_FUTURE_DESIGN_LEVELS = frozenset(
    {
        TrustedPlanningMode.CHAPTER_SET,
        TrustedPlanningMode.CHAPTER,
        TrustedPlanningMode.SCENE,
        TrustedPlanningMode.REPLAN,
    }
)


def resolve_question_semantics(
    *,
    mode: TrustedPlanningMode,
    intent: TrustedQuestionIntent,
    kind_is_fact: bool,
    blocking: bool,
) -> tuple[QuestionPurpose, DependencyExpectation]:
    """Derive the two boundary dimensions from host-bound facts only.

    ``kind_is_fact`` is whether the model declared a ``fact`` or
    ``relation_causal`` question.  That declaration is the one part of the
    classification a model legitimately supplies: it says what it is asking
    about, not what the answer must be.  The mode, the intent and the blocking
    flag all come from the reviewed task, so a model cannot relabel a real
    historical prerequisite as ``design_future`` to escape evidence checks.
    """

    if intent is TrustedQuestionIntent.REQUESTED_MEMORY_EVIDENCE:
        # The model spent a planning turn asking Memory for prior evidence.
        # That request is a history question at any planning level, and its
        # non-answer is an unresolved dependency rather than missing canon.
        purpose = QuestionPurpose.VERIFY_HISTORY
        expectation = DependencyExpectation.CHECK_STATUS
        return purpose, expectation
    if intent is TrustedQuestionIntent.REVIEWER_GAP_QUESTION:
        # An independent reviewer asserted that the candidate needs a fact.
        # It stays a hard prerequisite so the loop either resolves it or
        # revises the plan; it is never silently downgraded.
        purpose = QuestionPurpose.VERIFY_HISTORY
        return purpose, DependencyExpectation.MUST_ESTABLISH_EXISTING_FACT
    if mode in _FUTURE_DESIGN_LEVELS:
        return QuestionPurpose.DESIGN_FUTURE, DependencyExpectation.NO_HISTORICAL_PRECONDITION
    if mode is TrustedPlanningMode.PROJECT_BOOTSTRAP:
        # Bootstrap has no committed canon to verify against; every question is
        # a design input and must not be routed to commit-scoped Memory.
        return QuestionPurpose.DESIGN_FUTURE, DependencyExpectation.NO_HISTORICAL_PRECONDITION
    purpose = (
        QuestionPurpose.CHECK_CURRENT_STATE if kind_is_fact else QuestionPurpose.VERIFY_HISTORY
    )
    expectation = (
        DependencyExpectation.MUST_ESTABLISH_EXISTING_FACT
        if blocking
        else DependencyExpectation.CHECK_STATUS
    )
    return purpose, expectation


def disposition_diagnostic(disposition: GapDisposition) -> str:
    """Return the typed diagnostic a terminal must report for a disposition."""

    if not isinstance(disposition, GapDisposition):
        raise ValueError("disposition diagnostic requires a gap disposition")
    if disposition is GapDisposition.PRECONDITION_CONFLICT:
        return UNSUPPORTED_PLAN_PRECONDITION
    if disposition is GapDisposition.CANON_EXTRACTION_GAP:
        return GapDisposition.CANON_EXTRACTION_GAP.value
    return HISTORICAL_DEPENDENCY_UNRESOLVED


__all__ = [
    "HISTORICAL_DEPENDENCY_UNRESOLVED",
    "UNSUPPORTED_PLAN_PRECONDITION",
    "DependencyExpectation",
    "GapDisposition",
    "QuestionPurpose",
    "TrustedPlanningMode",
    "TrustedQuestionIntent",
    "VerifiedGapEvidence",
    "classify_gap",
    "disposition_diagnostic",
    "resolve_question_semantics",
]
