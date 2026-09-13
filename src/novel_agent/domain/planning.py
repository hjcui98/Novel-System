"""Stage 4 Planner product-loop contracts.

These values remain candidate-only.  No contract in this module grants a
Planner or Reviewer permission to mutate canonical roots or commit state.
"""

from __future__ import annotations

import json
import re
from collections.abc import Container, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pydantic import Field, JsonValue, model_validator

from novel_agent.domain.agent_context import LoopRoundProgress
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.author_constraints import AuthorConstraint
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, RunId, StableId, TaskId
from novel_agent.domain.memory import NeedFacetKind
from novel_agent.domain.model_calls import ModelCallLedgerAggregate
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    ContextBudget,
    PlannerProposalDraft,
    PlanningTask,
    PlanProposal,
    RetrievalBudget,
)
from novel_agent.domain.text import EvidenceRef, SourceBoundEvidenceRequirement

PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE = "application/vnd.novel-agent.planning-loop-checkpoint+json"


class PlanningProvenance(StrEnum):
    AUTHOR_SUPPLIED = "author_supplied"
    ACCEPTED_PLAN_DERIVED = "accepted_plan_derived"
    CANON_DERIVED = "canon_derived"
    REVIEWER_DERIVED = "reviewer_derived"
    PLANNER_PROPOSED = "planner_proposed"


class PlanningReference(DomainModel):
    provenance: PlanningProvenance
    reference_ids: tuple[StableId, ...] = ()
    artifact_refs: tuple[ArtifactRef, ...] = ()

    @model_validator(mode="after")
    def validate_reference(self) -> PlanningReference:
        if self.provenance is not PlanningProvenance.PLANNER_PROPOSED and not (
            self.reference_ids or self.artifact_refs
        ):
            raise ValueError("trusted planning provenance requires an explicit reference")
        if self.provenance is PlanningProvenance.PLANNER_PROPOSED and (
            self.reference_ids or self.artifact_refs
        ):
            raise ValueError("Planner-proposed content cannot claim a trusted source")
        return self


class GoalProposal(DomainModel):
    goal_id: StableId
    summary: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    provenance: PlanningReference
    decision_criteria: tuple[str, ...] = ()


class PlanningQuestionKind(StrEnum):
    FACT = "fact"
    RELATION_CAUSAL = "relation_causal"
    OBLIGATION_PACING = "obligation_pacing"
    STYLE_REFERENCE = "style_reference"
    HUMAN_CHOICE = "human_choice"


class PlanningQuestion(DomainModel):
    question_id: StableId
    kind: PlanningQuestionKind
    question: str = Field(min_length=1)
    provenance: PlanningReference
    goal_id: StableId
    entity_labels: tuple[str, ...] = ()
    relation_subject: str | None = Field(default=None, min_length=1)
    relation_predicate: str | None = Field(default=None, min_length=1)
    relation_object: str | None = Field(default=None, min_length=1)
    blocking: bool = False

    @model_validator(mode="after")
    def validate_relation(self) -> PlanningQuestion:
        relation = (self.relation_subject, self.relation_predicate, self.relation_object)
        if any(item is not None for item in relation) and not all(
            item is not None for item in relation
        ):
            raise ValueError("planning relation question requires subject/predicate/object")
        return self


class PlanningInquiry(DomainModel):
    inquiry_id: StableId
    project_id: ProjectId
    mode: AgentMode
    planning_scope: tuple[str, ...]
    horizon_start: int | None = Field(default=None, ge=1)
    horizon_end: int | None = Field(default=None, ge=1)
    author_intent_refs: tuple[ArtifactRef, ...]
    explicit_overrides: tuple[str, ...] = ()
    goal_proposals: tuple[GoalProposal, ...] = Field(min_length=1)
    alternatives: tuple[str, ...] = ()
    assumptions: tuple[PlanningQuestion, ...] = ()
    questions: tuple[PlanningQuestion, ...] = ()
    decision_criteria: tuple[str, ...] = ()
    expected_output_shape: str = Field(min_length=1)
    human_choices: tuple[str, ...] = ()
    parent_inquiry_id: StableId | None = None
    generation: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_inquiry(self) -> PlanningInquiry:
        planner_modes = {
            AgentMode.PROJECT_BOOTSTRAP,
            AgentMode.STORY,
            AgentMode.ARC_VOLUME,
            AgentMode.CHAPTER_SET,
            AgentMode.CHAPTER,
            AgentMode.SCENE,
            AgentMode.REPLAN,
        }
        if self.mode not in planner_modes:
            raise ValueError("PlanningInquiry requires a Stage 4 Planner mode")
        if not self.planning_scope:
            raise ValueError("PlanningInquiry requires a planning scope")
        if (self.horizon_start is None) != (self.horizon_end is None):
            raise ValueError("planning horizon bounds must appear together")
        if (
            self.horizon_start is not None
            and self.horizon_end is not None
            and self.horizon_end < self.horizon_start
        ):
            raise ValueError("planning horizon end precedes start")
        if self.mode is AgentMode.CHAPTER_SET and (
            self.horizon_start is None or self.horizon_end is None
        ):
            raise ValueError("CHAPTER_SET requires an explicit rolling horizon")
        if self.mode is AgentMode.PROJECT_BOOTSTRAP and not self.author_intent_refs:
            raise ValueError("PROJECT_BOOTSTRAP inquiry requires author-approved sources")
        if self.generation == 1 and self.parent_inquiry_id is not None:
            raise ValueError("initial inquiry cannot have a parent")
        if self.generation > 1 and self.parent_inquiry_id is None:
            raise ValueError("revised inquiry requires its parent")
        return self


class PlanningProblemIdentitySeed(DomainModel):
    """Pre-registered, source-bound identity for one Planner Memory problem.

    The seed is execution input, not a model conclusion.  ``source_commit`` and
    ``source_text_root`` identify the frozen source from which the seed was
    prepared; the destination run may have a different project Commit after
    the canonical roots are copied into an isolated object store.
    """

    need_id: StableId
    question_id: StableId
    need_query: str = Field(min_length=1, max_length=2048)
    semantic_question: str = Field(min_length=1, max_length=2048)
    facet: NeedFacetKind
    source_commit: CommitId
    source_text_root: ArtifactId
    cutoff_chapter: int = Field(ge=0)
    # Optional preflight contract for a source-bound causal problem.  When
    # present, this exact span/marker requirement must survive into the
    # maintenance finding and candidate validation path.
    source_evidence_requirement: SourceBoundEvidenceRequirement | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> PlanningProblemIdentitySeed:
        if self.need_query.strip() != self.need_query or (
            self.semantic_question.strip() != self.semantic_question
        ):
            raise ValueError("problem identity seed questions must not have surrounding whitespace")
        if not self.need_id.root.startswith("need."):
            raise ValueError("problem identity seed need_id must use the Need namespace")
        requirement = self.source_evidence_requirement
        if requirement is not None:
            if requirement.source_artifact_id != self.source_text_root:
                raise ValueError("source-bound evidence requirement must use the seed TextRoot")
            if requirement.source_chapter_index > self.cutoff_chapter:
                raise ValueError("source-bound evidence requirement exceeds the seed cutoff")
        return self


class PlanningInquiryDraft(DomainModel):
    """Untrusted structured output normalized into ``PlanningInquiry`` by the agent."""

    mode: AgentMode
    planning_scope: tuple[str, ...]
    horizon_start: int | None = Field(default=None, ge=1)
    horizon_end: int | None = Field(default=None, ge=1)
    goal_proposals: tuple[GoalProposal, ...] = Field(min_length=1)
    alternatives: tuple[str, ...] = ()
    assumptions: tuple[PlanningQuestion, ...] = ()
    questions: tuple[PlanningQuestion, ...] = ()
    decision_criteria: tuple[str, ...] = ()
    expected_output_shape: str = Field(min_length=1)
    human_choices: tuple[str, ...] = ()


class ReviewTargetKind(StrEnum):
    INQUIRY = "inquiry"
    PLAN_PROPOSAL = "plan_proposal"


class ReviewDecision(StrEnum):
    ACCEPT = "accept"
    REVISE = "revise"
    HUMAN_REQUIRED = "human_required"


class ReviewIssueKind(StrEnum):
    COVERAGE = "coverage"
    CONTRADICTION = "contradiction"
    FEASIBILITY = "feasibility"
    OBLIGATION = "obligation"
    PACING = "pacing"
    ALTERNATIVE_COMPARISON = "alternative_comparison"
    MEMORY_GAP = "memory_gap"
    PROVENANCE = "provenance"
    LONG_RANGE_PAYOFF_WITHOUT_TIME_WINDOW = "long_range_payoff_without_time_window"
    EARLY_RESOLUTION_OF_FUTURE_LOCKED_OBLIGATION = "early_resolution_of_future_locked_obligation"
    TARGET_WINDOW_OUTSIDE_PARENT_SCOPE = "target_window_outside_parent_scope"
    BLOCKING_UNRESOLVED = "blocking_unresolved"
    VOLUME_STRUCTURE_INCOMPLETE = "volume_structure_incomplete"
    OBLIGATION_CONTRACT = "obligation_contract"
    UNRESOLVED_SCOPE_MISSING = "unresolved_scope_missing"
    VOLUME_STAGE_WINDOW_VIOLATION = "volume_stage_window_violation"


# The minimum executable volume outline (2026-09-10 remediation P0-5).  A
# volume arc must carry each of these non-empty payload keys so the rolling
# CHAPTER_SET planner and the Writer inherit a real stage grid instead of a
# three-event roadmap.
VOLUME_STRUCTURE_REQUIRED_KEYS: tuple[str, ...] = (
    "opening_state",
    "trigger_event",
    "first_escalation",
    "first_cost",
    "midpoint_reversal",
    "second_escalation",
    "volume_climax",
    "climax_cost",
    "ending_state",
    "next_volume_hook",
    "protagonist_arc",
    "supporting_arc",
    "faction_arc",
    "capability_ceiling",
    "equipment_ceiling",
    "entry_conditions",
    "exit_conditions",
    "reveal_window",
    "obligation_plan",
)


def missing_volume_structure_keys(payload: Mapping[str, object]) -> tuple[str, ...]:
    """Return the required volume slots that are absent or empty."""

    missing: list[str] = []
    for key in VOLUME_STRUCTURE_REQUIRED_KEYS:
        value = payload.get(key)
        if (
            value is None
            or (isinstance(value, str) and not value.strip())
            or (isinstance(value, (list, tuple, dict)) and not value)
        ):
            missing.append(key)
    return tuple(missing)


# Volume slots that carry stage semantics: when a plan expresses one of these as a
# structured entry, the entry must declare the stage range it applies to and the
# obligation it serves.  A free-text slot is volume-scoped by definition and cannot
# produce a false stage grid, so it stays acceptable.
VOLUME_STAGE_SLOT_KEYS: tuple[str, ...] = (
    "entry_conditions",
    "exit_conditions",
    "reveal_window",
)
_VOLUME_INVARIANT_SLOT_KEYS: tuple[str, ...] = ("capability_ceiling", "equipment_ceiling")


def volume_stage_grid_defects(payload: Mapping[str, object]) -> tuple[str, ...]:
    """Return stage entries that declare no usable stage range or obligation.

    Ten non-empty fields are not a stage grid.  A structured stage entry has to say
    *when* it applies (inside the volume's own range) and *which* responsibility it
    serves, otherwise the entry cannot be checked at its boundary.
    """

    defects: list[str] = []
    chapter_start = payload.get("chapter_start")
    chapter_end = payload.get("chapter_end")
    for key in (*VOLUME_STAGE_SLOT_KEYS, *_VOLUME_INVARIANT_SLOT_KEYS):
        value = payload.get(key)
        entries: Sequence[object]
        if isinstance(value, Mapping):
            entries = (value,)
        elif isinstance(value, (list, tuple)):
            entries = value
        else:
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                continue
            label = f"{key}[{index}]"
            start = entry.get("chapter_start")
            end = entry.get("chapter_end")
            if not isinstance(start, int) or not isinstance(end, int) or end < start:
                defects.append(f"{label} declares no usable chapter_start/chapter_end")
                continue
            if isinstance(chapter_start, int) and start < chapter_start:
                defects.append(f"{label} starts before its volume scope")
            if isinstance(chapter_end, int) and end > chapter_end:
                defects.append(f"{label} ends after its volume scope")
            referenced = entry.get("obligation_ids")
            if not isinstance(referenced, (list, tuple)) or not any(
                isinstance(item, str) and item.strip() for item in referenced
            ):
                defects.append(f"{label} declares no obligation id it serves")
    return tuple(defects)


class PlanReviewIssue(DomainModel):
    issue_id: StableId
    kind: ReviewIssueKind
    summary: str = Field(min_length=1)
    blocking: bool
    evidence_refs: tuple[EvidenceRef, ...] = ()
    affected_item_ids: tuple[StableId, ...] = ()
    # Machine-checkable citation: which field of the named item violates which
    # constraint, with the candidate text that proves it.  Optional so historical
    # artifacts stay readable, required of newly generated blocking findings.
    field_path: str | None = Field(default=None, min_length=1)
    constraint_id: str | None = Field(default=None, min_length=1)
    quote: str | None = Field(default=None, min_length=1)
    unmet_condition: str | None = Field(default=None, min_length=1)
    # The host decided this finding itself from the candidate and the trusted
    # catalogue, so it needs no model citation and is exempt from citation
    # verification.  Defaulted so historical artifacts stay readable.
    host_issued: bool = False


class ReviewCitationFailure(StrEnum):
    """Why a blocking finding could not be grounded in the reviewed candidate.

    A finding whose citation does not resolve is not evidence about the candidate;
    it is a defect of the review.  The reason is kept so a re-review can be told
    what was wrong with the previous one instead of guessing.
    """

    TARGET_UNREADABLE = "target_unreadable"
    ITEM_NOT_FOUND = "item_not_found"
    FIELD_PATH_INVALID = "field_path_invalid"
    FIELD_NOT_FOUND = "field_not_found"
    VALUE_NOT_IN_FIELD = "value_not_in_field"
    EVIDENCE_FIELDS_MISSING = "evidence_fields_missing"
    CONSTRAINT_NOT_APPLICABLE = "constraint_not_applicable"


class PlanReviewDraft(DomainModel):
    target_kind: ReviewTargetKind
    decision: ReviewDecision
    issues: tuple[PlanReviewIssue, ...] = ()
    preserve_item_ids: tuple[StableId, ...] = ()
    revision_instruction: str | None = Field(default=None, min_length=1)
    memory_gap_questions: tuple[str, ...] = ()
    # Host-computed coverage evidence: one entry per coverage question, each with its
    # own trusted denominator and missing items.  Kept as text so a long-standing
    # review artifact stays schema-compatible while the numbers remain auditable.
    coverage_evidence: tuple[str, ...] = ()
    # Host verification of this review's own citations.  Empty means every blocking
    # finding resolved against the candidate; a non-empty tuple means the review is
    # not yet usable evidence and must be redone for the same candidate instead of
    # being forwarded to the planner.  Defaulted so historical drafts stay readable.
    verification_failures: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_decision(self) -> PlanReviewDraft:
        blocking = any(issue.blocking for issue in self.issues)
        if self.decision is ReviewDecision.ACCEPT and blocking:
            raise ValueError("ACCEPT cannot retain a blocking review issue")
        if self.decision is ReviewDecision.REVISE and not self.revision_instruction:
            raise ValueError("REVISE requires a bounded revision instruction")
        if self.decision is not ReviewDecision.REVISE and self.revision_instruction is not None:
            raise ValueError("only REVISE may carry a revision instruction")
        return self


class PlanReview(DomainModel):
    review_id: StableId
    target_kind: ReviewTargetKind
    target_artifact_ref: ArtifactRef
    decision: ReviewDecision
    issues: tuple[PlanReviewIssue, ...] = ()
    preserve_item_ids: tuple[StableId, ...] = ()
    revision_instruction: str | None = None
    memory_gap_questions: tuple[str, ...] = ()
    # Carried from the draft so a settled review can still be recognised as one whose
    # own citations the host refused.  Defaulted for historical artifacts.
    verification_failures: tuple[str, ...] = ()
    receipt: AgentExecutionReceipt


class PlanningBudgets(DomainModel):
    # Per-invocation work slices.  Checkpoints carry lifetime counters; a later
    # Stage 5 Attempt may grant another slice without changing physical limits.
    inquiry_revisions: int = Field(default=1, ge=0)
    plan_revisions: int = Field(default=1, ge=0)
    reviewer_memory_rounds: int = Field(default=1, ge=0)
    planner_memory_rounds: int = Field(default=1, ge=0)
    retrieval: RetrievalBudget
    context: ContextBudget
    # Stage 2 ContextBudget remains the Memory package budget.  This optional
    # Stage 4 value is only the Planner View selection target; the shared
    # Context Runtime owns the provider's physical hard window.
    planner_context_target_tokens: int | None = Field(default=None, ge=1)
    # Aggregate input/output/reasoning tokens consumed by one invocation.  A call is
    # never interrupted mid-flight; the loop yields at the next durable phase boundary.
    model_token_budget: int = Field(default=8_000, ge=1)


class PlanningLoopRequest(DomainModel):
    request_id: StableId
    run_id: RunId
    task_id: TaskId
    project_id: ProjectId
    task: PlanningTask
    author_intent_artifacts: tuple[ArtifactRef, ...]
    accepted_plan_ref: ArtifactRef | None = None
    accepted_world_ref: ArtifactRef | None = None
    accepted_text_ref: ArtifactRef | None = None
    project_profile_ref: ArtifactRef | None = None
    snapshot_id: StableId | None = None
    explicit_author_overrides: tuple[str, ...] = ()
    horizon_start: int | None = Field(default=None, ge=1)
    horizon_end: int | None = Field(default=None, ge=1)
    allowed_skill_ids: tuple[StableId, ...] = ()
    budgets: PlanningBudgets
    configuration_fingerprint: ArtifactId
    model_fingerprint: ArtifactId

    @model_validator(mode="after")
    def validate_basis(self) -> PlanningLoopRequest:
        if self.task.project_id != self.project_id:
            raise ValueError("planning task project differs from loop request")
        if len(self.author_intent_artifacts) != len(self.task.source_ids):
            raise ValueError("PlanningTask source ids require exact artifact bindings")
        if self.task.mode is AgentMode.PROJECT_BOOTSTRAP:
            if self.snapshot_id is not None or any(
                ref is not None
                for ref in (self.accepted_plan_ref, self.accepted_world_ref, self.accepted_text_ref)
            ):
                raise ValueError("PROJECT_BOOTSTRAP cannot bind commit-scoped project Memory")
            if not self.author_intent_artifacts:
                raise ValueError("PROJECT_BOOTSTRAP requires author-approved source artifacts")
        elif self.snapshot_id is None or any(
            ref is None
            for ref in (self.accepted_plan_ref, self.accepted_world_ref, self.accepted_text_ref)
        ):
            raise ValueError("post-Genesis planning requires snapshot and accepted roots")
        if (self.horizon_start is None) != (self.horizon_end is None):
            raise ValueError("planning horizon bounds must appear together")
        if (
            self.horizon_start is not None
            and self.horizon_end is not None
            and self.horizon_end < self.horizon_start
        ):
            raise ValueError("planning horizon end precedes start")
        if self.task.mode is AgentMode.CHAPTER_SET and self.horizon_start is None:
            raise ValueError("CHAPTER_SET requires a rolling horizon")
        return self


class PlannerContextSection(StrEnum):
    AUTHOR_INTENT = "author_intent"
    AUTHOR_CONSTRAINTS = "author_constraints"
    ACCEPTED_PLAN = "accepted_plan"
    CURRENT_STATE = "current_state"
    HISTORY_DEVIATION = "history_deviation"
    RELATION_CAUSAL = "relation_causal"
    STYLE_REFERENCE = "style_reference"
    WORKING_PROPOSAL = "working_proposal"
    UNRESOLVED = "unresolved"


class PlannerContextItem(DomainModel):
    context_item_id: StableId
    section: PlannerContextSection
    text: str = Field(min_length=1)
    protected: bool = False
    mandatory: bool = False
    token_count: int = Field(ge=1)
    source_artifact_refs: tuple[ArtifactRef, ...] = ()
    retrieval_unit_ids: tuple[StableId, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    graph_path_receipt_refs: tuple[ArtifactRef, ...] = ()
    compact_handle: StableId | None = None


class PlannerContextBudgetReport(DomainModel):
    token_budget: int = Field(ge=1)
    mandatory_tokens: int = Field(ge=0)
    selected_tokens: int = Field(ge=0)
    soft_overflow_tokens: int = Field(default=0, ge=0)
    dropped_item_ids: tuple[StableId, ...] = ()
    drop_reasons: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_totals(self) -> PlannerContextBudgetReport:
        if self.mandatory_tokens > self.selected_tokens:
            raise ValueError("mandatory Planner context exceeds selected total")
        if self.soft_overflow_tokens != max(0, self.selected_tokens - self.token_budget):
            raise ValueError("Planner context token budget soft overflow report is inconsistent")
        return self


class PlannerContextPackage(DomainModel):
    package_id: StableId
    contract_version: str = Field(min_length=1)
    project_id: ProjectId
    mode: AgentMode
    planning_scope: tuple[str, ...]
    horizon_start: int | None = Field(default=None, ge=1)
    horizon_end: int | None = Field(default=None, ge=1)
    base_commit: CommitId | None = None
    snapshot_id: StableId | None = None
    profile_ref: ArtifactRef | None = None
    author_constraint_root_ref: ArtifactRef | None = None
    reviewed_inquiry_ref: ArtifactRef
    stage1_context_ref: ArtifactRef | None = None
    items: tuple[PlannerContextItem, ...]
    unresolved_gaps: tuple[str, ...] = ()
    need_ids: tuple[StableId, ...] = ()
    retrieval_unit_ids: tuple[StableId, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    graph_path_receipt_refs: tuple[ArtifactRef, ...] = ()
    expansion_receipt_refs: tuple[ArtifactRef, ...] = ()
    budget_report: PlannerContextBudgetReport
    rendered_context: str

    @model_validator(mode="after")
    def validate_package(self) -> PlannerContextPackage:
        if self.mode is AgentMode.PROJECT_BOOTSTRAP:
            if self.base_commit is not None or self.snapshot_id is not None:
                raise ValueError("bootstrap Planner context cannot claim a project basis")
            if self.stage1_context_ref is not None:
                raise ValueError("bootstrap Planner context cannot bind Memory Gateway output")
        elif (
            self.base_commit is None or self.snapshot_id is None or self.stage1_context_ref is None
        ):
            raise ValueError("post-Genesis Planner context requires exact Memory basis")
        if any(
            item.section
            in {
                PlannerContextSection.AUTHOR_INTENT,
                PlannerContextSection.ACCEPTED_PLAN,
                PlannerContextSection.UNRESOLVED,
            }
            and not item.protected
            for item in self.items
        ):
            raise ValueError("intent, accepted Plan, and unresolved items must be protected")
        return self


class PlannerEvidenceExpansionReceipt(DomainModel):
    receipt_id: ArtifactId
    contract_version: str = "planner_evidence_expansion.v1"
    base_commit: CommitId
    snapshot_id: StableId
    compact_handle: StableId
    source_unit_id: StableId
    expanded_unit_ids: tuple[StableId, ...] = Field(min_length=1)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)


class PlannerContextProjection(DomainModel):
    """Narrow Stage 4 view of the Stage 3-owned shared Context Runtime."""

    run_id: RunId
    task_id: TaskId
    seed_ref: ArtifactRef
    view_ref: ArtifactRef
    generation: int = Field(ge=0)
    basis_event_position: int = Field(ge=0)
    rendered_context: str
    token_count: int = Field(ge=0)
    exposed_context_item_ids: tuple[StableId, ...]
    used_context_item_ids: tuple[StableId, ...] = ()
    compaction_receipt_ref: ArtifactRef | None = None
    suspended: bool = False
    suspension_reason: str | None = None

    @model_validator(mode="after")
    def validate_projection(self) -> PlannerContextProjection:
        if not set(self.used_context_item_ids).issubset(self.exposed_context_item_ids):
            raise ValueError("used Planner context must be a subset of exposed context")
        if self.suspended != (self.suspension_reason is not None):
            raise ValueError("Planner Context suspension flag and reason must agree")
        return self


class PlanningTurnAction(StrEnum):
    PLAN_READY = "plan_ready"
    REQUEST_MEMORY = "request_memory"


class PlanningTurnDraft(DomainModel):
    """Untrusted Planner response before trusted proposal lineage is attached."""

    action: PlanningTurnAction
    plan_proposal_draft: PlannerProposalDraft | None = None
    memory_questions: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    selected_skill_ids: tuple[StableId, ...] = ()
    used_context_item_ids: tuple[StableId, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_plan_draft(cls, value: object) -> object:
        """Treat the existing raw proposal shape as PLAN_READY during migration."""

        if not isinstance(value, dict):
            return value
        if "action" not in value and "mode" in value:
            value = {
                "action": PlanningTurnAction.PLAN_READY,
                "plan_proposal_draft": value,
            }
        nested = value.get("plan_proposal_draft")
        if isinstance(nested, dict):
            value = {
                **value,
                "plan_proposal_draft": PlannerProposalDraft.model_validate_json(json.dumps(nested)),
            }
        coerced = dict(value)
        for key in (
            "memory_questions",
            "assumptions",
            "unresolved",
            "selected_skill_ids",
            "used_context_item_ids",
        ):
            field = coerced.get(key)
            if isinstance(field, list):
                coerced[key] = tuple(field)
        memory_questions = coerced.get("memory_questions")
        if isinstance(memory_questions, (list, tuple)) and all(
            isinstance(question, str) for question in memory_questions
        ):
            unique_questions: list[str] = []
            seen_questions: set[str] = set()
            for question in memory_questions:
                if question not in seen_questions:
                    seen_questions.add(question)
                    unique_questions.append(question)
            coerced["memory_questions"] = tuple(unique_questions)
        return coerced

    @model_validator(mode="after")
    def validate_action(self) -> PlanningTurnDraft:
        if self.action is PlanningTurnAction.PLAN_READY:
            if self.plan_proposal_draft is None or self.memory_questions:
                raise ValueError("PLAN_READY requires a proposal draft and no Memory request")
        elif self.plan_proposal_draft is not None or not self.memory_questions:
            raise ValueError("REQUEST_MEMORY requires questions and no proposal draft")
        if len(self.memory_questions) != len(set(self.memory_questions)):
            raise ValueError("Planner Memory questions must be unique")
        return self


class PlanningTurnOutput(DomainModel):
    action: PlanningTurnAction
    plan_proposal: PlanProposal | None = None
    memory_questions: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    selected_skill_ids: tuple[StableId, ...] = ()
    used_context_item_ids: tuple[StableId, ...] = ()

    @model_validator(mode="after")
    def validate_action(self) -> PlanningTurnOutput:
        if self.action is PlanningTurnAction.PLAN_READY:
            if self.plan_proposal is None or self.memory_questions:
                raise ValueError("PLAN_READY requires a proposal and no Memory request")
        elif self.plan_proposal is not None or not self.memory_questions:
            raise ValueError("REQUEST_MEMORY requires questions and no proposal")
        return self


class PlanningLoopTerminal(StrEnum):
    PLAN_CANDIDATE_READY = "plan_candidate_ready"
    INQUIRY_INVALID = "inquiry_invalid"
    INQUIRY_REVIEW_REQUIRED = "inquiry_review_required"
    MEMORY_INSUFFICIENT = "memory_insufficient"
    PLAN_CONFLICT = "plan_conflict"
    REVIEW_REVISION_REQUIRED = "review_revision_required"
    HUMAN_REQUIRED = "human_required"
    CONTEXT_LIMIT = "context_limit"
    MODEL_UNAVAILABLE = "model_unavailable"
    BASIS_CHANGED = "basis_changed"
    DEGRADED_NOT_PROMOTABLE = "degraded_not_promotable"
    REVIEW_REQUIRED = "review_required"
    SUSPENDED = "suspended"
    YIELDED = "yielded"
    BLOCKED = "blocked"


class PlanningLoopResult(DomainModel):
    request_id: StableId
    terminal: PlanningLoopTerminal
    inquiry_ref: ArtifactRef | None = None
    inquiry_review_ref: ArtifactRef | None = None
    memory_context_ref: ArtifactRef | None = None
    planner_context_ref: ArtifactRef | None = None
    proposal: PlanProposal | None = None
    plan_review_ref: ArtifactRef | None = None
    event_artifacts: tuple[ArtifactRef, ...] = ()
    diagnostic_codes: tuple[str, ...] = ()
    degraded: bool = False
    round_progress: LoopRoundProgress | None = None

    @model_validator(mode="after")
    def validate_terminal(self) -> PlanningLoopResult:
        if self.terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY:
            if self.proposal is None or self.degraded:
                raise ValueError("ready terminal requires a non-degraded Plan candidate")
            if self.plan_review_ref is None:
                raise ValueError("ready terminal requires an accepted independent review")
        if self.terminal is PlanningLoopTerminal.YIELDED and not any(
            ref.media_type == PLANNING_LOOP_CHECKPOINT_MEDIA_TYPE for ref in self.event_artifacts
        ):
            raise ValueError("yielded planning loop requires a resumable checkpoint")
        return self


class PlanningLoopPhase(StrEnum):
    PREFLIGHT = "preflight"
    INQUIRY_REVIEWED = "inquiry_reviewed"
    INQUIRY_ACCEPTED = "inquiry_accepted"
    MEMORY_RESOLVED = "memory_resolved"
    CONTEXT_READY = "context_ready"
    PLANNER_MEMORY_PENDING = "planner_memory_pending"
    PLAN_PROPOSED = "plan_proposed"
    PLAN_REVIEWED = "plan_reviewed"
    TERMINAL = "terminal"


class PlanningLoopCheckpoint(DomainModel):
    checkpoint_id: StableId
    request_id: StableId
    phase: PlanningLoopPhase
    base_commit: CommitId | None
    snapshot_id: StableId | None
    configuration_fingerprint: ArtifactId
    inquiry_ref: ArtifactRef | None = None
    inquiry_review_ref: ArtifactRef | None = None
    problem_identity_seed: PlanningProblemIdentitySeed | None = None
    memory_context_ref: ArtifactRef | None = None
    planner_context_ref: ArtifactRef | None = None
    proposal_ref: ArtifactRef | None = None
    plan_review_ref: ArtifactRef | None = None
    execution_ref: ArtifactRef | None = None
    inquiry_revisions_used: int = Field(default=0, ge=0)
    plan_revisions_used: int = Field(default=0, ge=0)
    reviewer_memory_rounds_used: int = Field(default=0, ge=0)
    reviewer_memory_review_ids: tuple[StableId, ...] = ()
    reviewer_context_refs: tuple[ArtifactRef, ...] = ()
    planner_memory_rounds_used: int = Field(default=0, ge=0)
    planner_memory_context_refs: tuple[ArtifactRef, ...] = ()
    handled_memory_question_ids: tuple[StableId, ...] = ()
    deferred_memory_question_ids: tuple[StableId, ...] = ()
    pending_planner_memory_questions: tuple[str, ...] = ()
    model_calls_used: int = Field(default=0, ge=0)
    model_input_tokens_used: int = Field(default=0, ge=0)
    model_output_tokens_used: int = Field(default=0, ge=0)
    model_reasoning_tokens_used: int = Field(default=0, ge=0)
    # The blocking findings the last settled plan review raised, as normalized text.
    # A resumed slice compares the next review against them instead of starting over,
    # or the same unresolved problem would be re-revised once per slice.
    plan_blocking_signature: tuple[str, ...] = ()
    round_progress: LoopRoundProgress | None = None

    @model_validator(mode="after")
    def validate_resume_frontier(self) -> PlanningLoopCheckpoint:
        inquiry_phases = {
            PlanningLoopPhase.INQUIRY_REVIEWED,
            PlanningLoopPhase.INQUIRY_ACCEPTED,
            PlanningLoopPhase.MEMORY_RESOLVED,
            PlanningLoopPhase.CONTEXT_READY,
            PlanningLoopPhase.PLANNER_MEMORY_PENDING,
            PlanningLoopPhase.PLAN_PROPOSED,
            PlanningLoopPhase.PLAN_REVIEWED,
        }
        if self.phase in inquiry_phases and (
            self.inquiry_ref is None or self.inquiry_review_ref is None
        ):
            raise ValueError("post-inquiry checkpoint requires inquiry and review refs")
        if (
            self.phase
            in {
                PlanningLoopPhase.CONTEXT_READY,
                PlanningLoopPhase.PLANNER_MEMORY_PENDING,
                PlanningLoopPhase.PLAN_PROPOSED,
                PlanningLoopPhase.PLAN_REVIEWED,
            }
            and self.planner_context_ref is None
        ):
            raise ValueError("post-context checkpoint requires a Planner context ref")
        if self.phase is PlanningLoopPhase.PLAN_REVIEWED and any(
            ref is None for ref in (self.proposal_ref, self.plan_review_ref, self.execution_ref)
        ):
            raise ValueError("reviewed Plan checkpoint requires proposal, review, and execution")
        if len(set(self.reviewer_memory_review_ids)) != len(self.reviewer_memory_review_ids):
            raise ValueError("reviewer Memory checkpoint contains duplicate review ids")
        if self.reviewer_memory_rounds_used != len(self.reviewer_memory_review_ids):
            raise ValueError("reviewer Memory counter differs from handled reviews")
        if len(self.reviewer_context_refs) != len(self.reviewer_memory_review_ids):
            raise ValueError("reviewer Memory reviews require matching context refs")
        if len(set(self.handled_memory_question_ids)) != len(self.handled_memory_question_ids):
            raise ValueError("handled Planner Memory question ids must be unique")
        if len(set(self.deferred_memory_question_ids)) != len(self.deferred_memory_question_ids):
            raise ValueError("deferred Planner Memory question ids must be unique")
        if set(self.handled_memory_question_ids) & set(self.deferred_memory_question_ids):
            raise ValueError("handled and deferred Planner Memory questions must be disjoint")
        if len(self.pending_planner_memory_questions) != len(
            set(self.pending_planner_memory_questions)
        ):
            raise ValueError("pending Planner Memory questions must be unique")
        return self


class PlanningLoopEventReceipt(DomainModel):
    event_id: StableId
    request_id: StableId
    phase: PlanningLoopPhase
    event_kind: str = Field(min_length=1)
    artifact_refs: tuple[ArtifactRef, ...] = ()
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    round_progress: LoopRoundProgress | None = None


class PlanningEvaluationCase(DomainModel):
    case_id: StableId
    mode: AgentMode
    request: PlanningLoopRequest
    corpus_fingerprint: ArtifactId
    expected_issue_tags: tuple[str, ...] = ()


class PlanningEvaluationObservation(DomainModel):
    result: PlanningLoopResult
    configuration_fingerprint: ArtifactId
    model_fingerprint: ArtifactId
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    model_call_count: int = Field(ge=0)
    exposed_evidence_count: int = Field(ge=0)
    used_evidence_count: int = Field(ge=0)
    channel_failure_count: int = Field(ge=0)
    degraded: bool = False

    @model_validator(mode="after")
    def validate_evidence_use(self) -> PlanningEvaluationObservation:
        if self.used_evidence_count > self.exposed_evidence_count:
            raise ValueError("used evaluation evidence must have been exposed")
        return self


class PlanningEvaluationMetric(StrEnum):
    AUTHOR_INTENT_COVERAGE_RATE = "author_intent_coverage_rate"
    ACCEPTED_PLAN_CANON_CONTRADICTION_COUNT = "accepted_plan_canon_contradiction_count"
    OBLIGATION_ARC_HOOK_CONTINUITY_SCORE = "obligation_arc_hook_continuity_score"
    ROLLING_HIERARCHY_CONSISTENCY_SCORE = "rolling_hierarchy_consistency_score"
    CHAPTER_FEASIBILITY_SCORE = "chapter_feasibility_score"
    ALTERNATIVE_QUALITY_SCORE = "alternative_quality_score"
    DECISION_RATIONALE_SCORE = "decision_rationale_score"
    REVIEWER_ISSUE_RECALL = "reviewer_issue_recall"
    FUTURE_LEAKAGE_COUNT = "future_leakage_count"
    PROVENANCE_ERROR_COUNT = "provenance_error_count"
    UNSUPPORTED_FACTUALIZATION_COUNT = "unsupported_factualization_count"


class PlanningEvaluationCriterion(DomainModel):
    metric: PlanningEvaluationMetric
    description: str = Field(min_length=1)
    higher_is_better: bool


class PlanningEvaluationRubric(DomainModel):
    rubric_id: StableId
    schema_version: str = Field(min_length=1)
    criteria: tuple[PlanningEvaluationCriterion, ...]

    @model_validator(mode="after")
    def validate_criteria(self) -> PlanningEvaluationRubric:
        metrics = tuple(criterion.metric for criterion in self.criteria)
        if len(metrics) != len(set(metrics)) or set(metrics) != set(PlanningEvaluationMetric):
            raise ValueError("Stage 4 rubric requires every semantic metric exactly once")
        if any(
            criterion.higher_is_better != (not criterion.metric.value.endswith("_count"))
            for criterion in self.criteria
        ):
            raise ValueError("Stage 4 rubric metric direction differs from Gate semantics")
        return self


class PlanningEvaluationThresholds(DomainModel):
    threshold_id: StableId
    schema_version: str = Field(min_length=1)
    author_intent_coverage_rate_min: float = Field(ge=0.0, le=1.0)
    accepted_plan_canon_contradiction_count_max: int = Field(ge=0)
    obligation_arc_hook_continuity_score_min: float = Field(ge=0.0, le=1.0)
    rolling_hierarchy_consistency_score_min: float = Field(ge=0.0, le=1.0)
    chapter_feasibility_score_min: float = Field(ge=0.0, le=1.0)
    alternative_quality_score_min: float = Field(ge=0.0, le=1.0)
    decision_rationale_score_min: float = Field(ge=0.0, le=1.0)
    reviewer_issue_recall_min: float = Field(ge=0.0, le=1.0)
    human_required_rate_max: float = Field(ge=0.0, le=1.0)
    future_leakage_count_max: int = Field(default=0, ge=0)
    provenance_error_count_max: int = Field(default=0, ge=0)
    unsupported_factualization_count_max: int = Field(default=0, ge=0)


class PlanningEvaluationManifest(DomainModel):
    manifest_id: StableId
    schema_version: str = Field(min_length=1)
    cases: tuple[PlanningEvaluationCase, ...]
    configuration_fingerprint: ArtifactId
    model_fingerprint: ArtifactId
    corpus_fingerprint: ArtifactId
    pilot_fingerprint: ArtifactId
    rubric_fingerprint: ArtifactId
    threshold_fingerprint: ArtifactId
    frozen_before_evaluator: bool

    @model_validator(mode="after")
    def validate_modes(self) -> PlanningEvaluationManifest:
        expected = {
            AgentMode.PROJECT_BOOTSTRAP,
            AgentMode.STORY,
            AgentMode.ARC_VOLUME,
            AgentMode.CHAPTER_SET,
            AgentMode.CHAPTER,
            AgentMode.SCENE,
            AgentMode.REPLAN,
        }
        actual = {case.mode for case in self.cases}
        if actual != expected or len(self.cases) != len(expected):
            raise ValueError("formal Stage 4 manifest requires exactly one case per Planner mode")
        if any(case.corpus_fingerprint != self.corpus_fingerprint for case in self.cases):
            raise ValueError("Stage 4 evaluation cases must use the same frozen corpus")
        return self


class PlanningEvaluationProfile(StrEnum):
    FORMAL_CONFIGURED = "formal_configured"
    DETERMINISTIC_FAKE = "deterministic_fake"


class PlanningEvaluationReport(DomainModel):
    manifest_id: StableId
    evaluation_profile: PlanningEvaluationProfile
    gate_eligible: bool
    semantic_gate_passed: bool | None
    results: tuple[PlanningLoopResult, ...]
    lineage_artifacts: tuple[ArtifactRef, ...]
    ablation_metrics: dict[str, JsonValue] = Field(default_factory=dict)
    reviewer_metrics: dict[str, JsonValue] = Field(default_factory=dict)
    leakage_count: int = Field(ge=0)
    provenance_error_count: int = Field(ge=0)
    model_call_aggregates: tuple[ModelCallLedgerAggregate, ...] = ()

    @model_validator(mode="after")
    def validate_gate_eligibility(self) -> PlanningEvaluationReport:
        expected = self.evaluation_profile is PlanningEvaluationProfile.FORMAL_CONFIGURED
        if self.gate_eligible != expected:
            raise ValueError("only formal configured evaluation is Gate-eligible")
        if self.gate_eligible and not self.reviewer_metrics:
            raise ValueError("Gate-eligible evaluation requires post-freeze blind review metrics")
        if self.gate_eligible != (self.semantic_gate_passed is not None):
            raise ValueError("only Gate-eligible evaluation can settle the semantic Gate")
        return self


# The narrative slots of a volume arc.  They are prose today, which is why an
# author lock expressed as a chapter window ("nothing before 350") could not be
# checked against them: a real candidate wrote a reveal into trigger_event and
# midpoint_reversal of a volume whose own reveal_window said 350+.  Each of these
# keys must therefore declare the window it happens in, the action it performs and
# the host-accepted responsibility it serves, so the lock becomes a field-level
# constraint the host can verify and the planner can be told exactly which key to
# move.
VOLUME_NARRATIVE_STAGE_KEYS: tuple[str, ...] = (
    "opening_state",
    "trigger_event",
    "first_escalation",
    "first_cost",
    "midpoint_reversal",
    "second_escalation",
    "volume_climax",
    "climax_cost",
    "ending_state",
    "next_volume_hook",
)
# 埋设 / 暗示 / 正式推进 / 兑现.  ``setup`` may sit anywhere in the volume; the other
# three reach information or capability the author may have locked behind a boundary.
VOLUME_STAGE_ROLES: tuple[str, ...] = ("setup", "hint", "progression", "payoff")


@dataclass(frozen=True)
class VolumeStageWindowDefect:
    """One field-level stage-window violation inside a volume item.

    ``field`` is the path inside the volume item (``midpoint_reversal.window``), so
    the review demand can name the exact slot the planner has to change instead of a
    generic request to fix "stage windows".
    """

    field: str
    message: str


def volume_stage_window_defects(
    payload: Mapping[str, object],
    *,
    constraints: Sequence[AuthorConstraint] = (),
    accepted_obligation_ids: Container[str] = (),
    obligation_windows: Mapping[str, tuple[int | None, int | None]] | None = None,
) -> tuple[VolumeStageWindowDefect, ...]:
    """Return stage slots whose declared window violates the responsibility it serves.

    The authority is the frozen author-constraint catalogue, never the candidate's own
    ``not_before_chapter``: a stage declares which responsibility it serves through
    ``serves`` and the host resolves that handle itself.  An unknown handle is a
    defect, so a proposal cannot invent an accepted obligation identity, and a
    boundary only constrains the stages that actually serve it -- an unrelated lock
    never blocks a legal hint for another responsibility.
    """

    defects: list[VolumeStageWindowDefect] = []
    chapter_start = payload.get("chapter_start")
    chapter_end = payload.get("chapter_end")
    catalogue = _stage_constraint_catalogue(constraints)
    for key in VOLUME_NARRATIVE_STAGE_KEYS:
        value = payload.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if isinstance(value, str):
            defects.append(
                VolumeStageWindowDefect(
                    f"{key}.description",
                    f"{key} must declare the chapter window, role and served "
                    "responsibility it happens in",
                )
            )
            continue
        if not isinstance(value, Mapping):
            defects.append(
                VolumeStageWindowDefect(
                    f"{key}.description",
                    f"{key} must be a stage entry with a window and a role",
                )
            )
            continue
        description = value.get("description") or value.get("summary") or value.get("text")
        if not isinstance(description, str) or not description.strip():
            defects.append(
                VolumeStageWindowDefect(
                    f"{key}.description", f"{key} requires a non-empty description"
                )
            )
        body = f"{key}.window"
        raw_window = value.get("window")
        if isinstance(raw_window, int) and not isinstance(raw_window, bool):
            window = (raw_window, raw_window)
        elif isinstance(raw_window, str):
            try:
                window = _parse_declared_window(raw_window, field=body)
            except ValueError as error:
                defects.append(VolumeStageWindowDefect(body, str(error)))
                continue
        else:
            defects.append(
                VolumeStageWindowDefect(body, f"{body} must be a chapter number or range")
            )
            continue
        start, end = window
        if isinstance(chapter_start, int) and start < chapter_start:
            defects.append(VolumeStageWindowDefect(body, f"{body} starts before its volume scope"))
        if isinstance(chapter_end, int) and end > chapter_end:
            defects.append(VolumeStageWindowDefect(body, f"{body} ends after its volume scope"))
        role = value.get("role")
        if not isinstance(role, str) or role.strip().lower() not in VOLUME_STAGE_ROLES:
            defects.append(
                VolumeStageWindowDefect(
                    f"{key}.role",
                    f"{key}.role must be one of " + ", ".join(VOLUME_STAGE_ROLES),
                )
            )
            continue
        role = role.strip().lower()
        served = value.get("serves")
        if served is None or (isinstance(served, str) and not served.strip()):
            # Planting may reach nothing locked.  A disclosure action has to say which
            # responsibility it discloses; a progression that names nothing is checked
            # by the prose semantic review and by the obligation time locks instead.
            if role in {"hint", "payoff"}:
                defects.append(
                    VolumeStageWindowDefect(
                        f"{key}.serves",
                        f"{key}.serves must name the host-accepted responsibility a "
                        f"{role} stage discloses",
                    )
                )
            continue
        if not isinstance(served, str):
            defects.append(
                VolumeStageWindowDefect(
                    f"{key}.serves",
                    f"{key}.serves must be a host-accepted responsibility handle",
                )
            )
            continue
        handle = _stage_handle(served)
        constraint = catalogue.get(handle)
        if constraint is None:
            if handle in accepted_obligation_ids:
                # An accepted obligation is a real responsibility, so its own window
                # binds the stage too; only an obligation whose window the host does
                # not know is accepted on identity alone.
                declared_window: tuple[int | None, int | None] | None = (
                    obligation_windows or {}
                ).get(handle)
                if declared_window is None:
                    continue
                earliest, latest = declared_window
                if earliest is not None and start < earliest:
                    defects.append(
                        VolumeStageWindowDefect(
                            f"{key}.window",
                            f"{key}.window starts at {start}, before the accepted obligation "
                            f"{handle} unlocks at {earliest}",
                        )
                    )
                if latest is not None and end > latest:
                    defects.append(
                        VolumeStageWindowDefect(
                            f"{key}.window",
                            f"{key}.window ends at {end}, after the accepted obligation "
                            f"{handle} closes at {latest}",
                        )
                    )
                continue
            defects.append(
                VolumeStageWindowDefect(
                    f"{key}.serves",
                    f"{key}.serves names {handle!r}, which is neither an accepted author "
                    "constraint nor an accepted obligation; reference only the handles "
                    "the host listed",
                )
            )
            continue
        if role == "setup":
            # Planting may reference a locked responsibility and still happen earlier:
            # that is what planting is for.  Only the disclosure and advancement
            # actions answer to the boundary.
            continue
        # The cited responsibility is authoritative: whatever the role label says, the
        # stage is bound by that responsibility's own boundary.  Rejecting a role and a
        # category that disagree only taught the planner to relabel; binding the stage
        # to what it actually names cannot be gamed, because the boundary still applies.
        boundary = constraint.not_before_chapter
        if boundary is None:
            boundary = constraint.chapter_earliest
        if boundary is not None and start < boundary:
            defects.append(
                VolumeStageWindowDefect(
                    body,
                    f"{body} starts at {start}, before the not_before_chapter {boundary} of "
                    f"the {role} responsibility it serves ({handle})",
                )
            )
        if constraint.chapter_latest is not None and end > constraint.chapter_latest:
            # A lock may also close: a responsibility declared latest at 420 cannot be
            # served by a stage that runs past it.
            defects.append(
                VolumeStageWindowDefect(
                    body,
                    f"{body} ends at {end}, after the latest_chapter "
                    f"{constraint.chapter_latest} of the responsibility it serves ({handle})",
                )
            )
    return tuple(defects)


def _stage_handle(value: str) -> str:
    """Normalize the handle a stage cites.

    The planner context renders each responsibility as ``[handle]``, so a model
    naturally copies the brackets; they are formatting, not part of the identity.
    """

    return value.strip().strip("[]").strip().strip('"').strip("'").strip()


def _stage_constraint_catalogue(
    constraints: Sequence[AuthorConstraint],
) -> dict[str, AuthorConstraint]:
    """Resolve a stage's ``serves`` handle to the frozen author constraint.

    Both the compiled constraint id and the channel's own key (an author planning
    lock id) are accepted, so the handle the planner was shown is the handle it can
    cite.
    """

    catalogue: dict[str, AuthorConstraint] = {}
    for constraint in constraints:
        catalogue[constraint.constraint_id.root] = constraint
        if constraint.constraint_key:
            catalogue.setdefault(constraint.constraint_key, constraint)
    return catalogue


def _parse_declared_window(value: str, *, field: str) -> tuple[int, int]:
    match = re.match(r"^(?P<start>[1-9][0-9]*)(?:\s*-\s*(?P<end>[1-9][0-9]*))?$", value.strip())
    if match is None:
        raise ValueError(f"{field} is not a chapter number or range: {value!r}")
    start = int(match.group("start"))
    end = int(match.group("end")) if match.group("end") else start
    if end < start:
        raise ValueError(f"{field} is reversed: {value!r}")
    return start, end


def _declared_not_before_boundaries(payload: Mapping[str, object]) -> tuple[int, ...]:
    """Every ``not_before_chapter`` the volume itself declares."""

    boundaries: list[int] = []
    sources: list[object] = [payload.get("obligation_plan"), payload.get("obligation_declarations")]
    for source in sources:
        if isinstance(source, Mapping):
            source = (source,)
        if not isinstance(source, (list, tuple)):
            continue
        for entry in source:
            if not isinstance(entry, Mapping):
                continue
            value = entry.get("not_before_chapter")
            if type(value) is int and value >= 1:
                boundaries.append(value)
    return tuple(sorted(set(boundaries)))
