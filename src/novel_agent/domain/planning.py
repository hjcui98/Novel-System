"""Stage 4 Planner product-loop contracts.

These values remain candidate-only.  No contract in this module grants a
Planner or Reviewer permission to mutate canonical roots or commit state.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, JsonValue, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, RunId, StableId, TaskId
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    ContextBudget,
    PlanningTask,
    PlanProposal,
    RetrievalBudget,
)
from novel_agent.domain.text import EvidenceRef


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
    generation: int = Field(default=1, ge=1, le=2)

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
        if self.generation == 2 and self.parent_inquiry_id is None:
            raise ValueError("revised inquiry requires its parent")
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


class PlanReviewIssue(DomainModel):
    issue_id: StableId
    kind: ReviewIssueKind
    summary: str = Field(min_length=1)
    blocking: bool
    evidence_refs: tuple[EvidenceRef, ...] = ()
    affected_item_ids: tuple[StableId, ...] = ()


class PlanReviewDraft(DomainModel):
    target_kind: ReviewTargetKind
    decision: ReviewDecision
    issues: tuple[PlanReviewIssue, ...] = ()
    preserve_item_ids: tuple[StableId, ...] = ()
    revision_instruction: str | None = Field(default=None, min_length=1)
    memory_gap_questions: tuple[str, ...] = ()

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
    receipt: AgentExecutionReceipt


class PlanningBudgets(DomainModel):
    inquiry_revisions: int = Field(default=1, ge=0, le=1)
    plan_revisions: int = Field(default=1, ge=0, le=1)
    reviewer_memory_rounds: int = Field(default=1, ge=0, le=1)
    retrieval: RetrievalBudget
    context: ContextBudget
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
    dropped_item_ids: tuple[StableId, ...] = ()
    drop_reasons: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_totals(self) -> PlannerContextBudgetReport:
        if self.mandatory_tokens > self.selected_tokens:
            raise ValueError("mandatory Planner context exceeds selected total")
        if self.selected_tokens > self.token_budget:
            raise ValueError("Planner context exceeds its token budget")
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
    generation: int = Field(ge=1)
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

    @model_validator(mode="after")
    def validate_terminal(self) -> PlanningLoopResult:
        if self.terminal is PlanningLoopTerminal.PLAN_CANDIDATE_READY:
            if self.proposal is None or self.degraded:
                raise ValueError("ready terminal requires a non-degraded Plan candidate")
            if self.plan_review_ref is None:
                raise ValueError("ready terminal requires an accepted independent review")
        return self


class PlanningLoopPhase(StrEnum):
    PREFLIGHT = "preflight"
    INQUIRY_ACCEPTED = "inquiry_accepted"
    MEMORY_RESOLVED = "memory_resolved"
    CONTEXT_READY = "context_ready"
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
    memory_context_ref: ArtifactRef | None = None
    planner_context_ref: ArtifactRef | None = None
    proposal_ref: ArtifactRef | None = None
    plan_review_ref: ArtifactRef | None = None
    inquiry_revisions_used: int = Field(default=0, ge=0, le=1)
    plan_revisions_used: int = Field(default=0, ge=0, le=1)
    reviewer_memory_rounds_used: int = Field(default=0, ge=0, le=1)


class PlanningLoopEventReceipt(DomainModel):
    event_id: StableId
    request_id: StableId
    phase: PlanningLoopPhase
    event_kind: str = Field(min_length=1)
    artifact_refs: tuple[ArtifactRef, ...] = ()
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class PlanningEvaluationCase(DomainModel):
    case_id: StableId
    mode: AgentMode
    request: PlanningLoopRequest
    corpus_fingerprint: ArtifactId
    expected_issue_tags: tuple[str, ...] = ()


class PlanningEvaluationManifest(DomainModel):
    manifest_id: StableId
    schema_version: str = Field(min_length=1)
    cases: tuple[PlanningEvaluationCase, ...]
    configuration_fingerprint: ArtifactId
    corpus_fingerprint: ArtifactId
    frozen_before_evaluator: bool = True

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


class PlanningEvaluationReport(DomainModel):
    manifest_id: StableId
    results: tuple[PlanningLoopResult, ...]
    lineage_artifacts: tuple[ArtifactRef, ...]
    ablation_metrics: dict[str, JsonValue] = Field(default_factory=dict)
    reviewer_metrics: dict[str, JsonValue] = Field(default_factory=dict)
    leakage_count: int = Field(ge=0)
    provenance_error_count: int = Field(ge=0)
