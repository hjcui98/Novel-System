"""Stage 3 Writer input, candidate artifact, and execution contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    StableId,
    TaskId,
)
from novel_agent.domain.model_calls import ModelCallRecord
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentType,
    ExecutionStatus,
    FutureIsolationAttestation,
    SkillExecutionReceipt,
    Stage2GateReport,
    Stage2GateVerdict,
)
from novel_agent.domain.writer_context import WriterContextPackage

_NonEmptyText = Annotated[str, StringConstraints(min_length=1)]


class WriterContextItem(DomainModel):
    """A single writer-safe context item in the Stage 3 frozen handoff."""

    item_id: StableId
    category: _NonEmptyText
    text: _NonEmptyText
    source_commit: CommitId
    snapshot_id: StableId
    access_scope: Literal["writer_safe"] = "writer_safe"
    information_label: _NonEmptyText = "writer_safe"
    derivation_taint: tuple[_NonEmptyText, ...] = ()
    entity_ids: tuple[StableId, ...] = ()
    predicate: _NonEmptyText | None = None
    narrative_start: int | None = Field(default=None, ge=0)
    narrative_end: int | None = Field(default=None, ge=0)
    story_time_start: _NonEmptyText | None = None
    story_time_end: _NonEmptyText | None = None
    truth_class: _NonEmptyText | None = None
    support_status: _NonEmptyText | None = None
    mandatory: bool = False

    @model_validator(mode="after")
    def validate_span_order(self) -> WriterContextItem:
        if (
            self.narrative_start is not None
            and self.narrative_end is not None
            and self.narrative_end < self.narrative_start
        ):
            raise ValueError("Writer context item narrative end precedes start")
        return self


class WriterContextSnapshot(DomainModel):
    """Frozen Writer-visible context used by Stage 3 Writing Core before formal handoff."""

    context_id: StableId
    base_commit: CommitId
    snapshot_id: StableId
    task_contract: _NonEmptyText
    items: tuple[WriterContextItem, ...] = ()
    unresolved_gaps: tuple[_NonEmptyText, ...] = ()
    budget_report: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_item_identity(self) -> WriterContextSnapshot:
        item_ids = tuple(item.item_id for item in self.items)
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("Writer context item ids must be unique")
        return self


_WRITER_MODES = frozenset(
    {
        AgentMode.DRAFT,
        AgentMode.CONTINUE,
        AgentMode.MAJOR_REWRITE,
    }
)


class WriterInputTaint(StrEnum):
    """Input labels that are forbidden at the Writer information boundary."""

    FUTURE = "future"
    EVALUATOR = "evaluator"
    GOLD = "gold"


class WriterSourceBinding(DomainModel):
    """Bind a trusted source identity to its immutable artifact."""

    source_id: StableId
    source_artifact: ArtifactRef
    taints: tuple[WriterInputTaint, ...] = ()

    @model_validator(mode="after")
    def reject_writer_unsafe_taints(self) -> WriterSourceBinding:
        if self.taints:
            raise ValueError("Writer source binding contains future/evaluator/gold taint")
        return self


class WriterArtifactBasis(DomainModel):
    """Immutable lineage shared by every input and output of one Writer run.

    ``configuration_fingerprint`` is specifically the canonical JSON fingerprint
    of the Writer AgentSpec, matching ``StructuredAgentRunner.prepare``.  The
    embedded future-isolation attestation retains its independent Memory and
    information-boundary configuration fingerprint; the two are intentionally
    not required to match.
    """

    project_id: ProjectId
    base_commit: CommitId
    snapshot_id: StableId
    context_id: StableId
    context_artifact: ArtifactRef
    context_fingerprint: ArtifactId
    writing_contract_artifact: ArtifactRef
    plan_artifact: ArtifactRef
    project_profile_artifact: ArtifactRef
    configuration_fingerprint: ArtifactId
    model_configuration_fingerprint: ArtifactId
    future_isolation_attestation: FutureIsolationAttestation
    memory_gate_report: Stage2GateReport | None = None
    memory_gate_artifact: ArtifactRef | None = None
    source_artifacts: tuple[WriterSourceBinding, ...] = ()

    @model_validator(mode="after")
    def validate_basis(self) -> WriterArtifactBasis:
        if self.context_artifact.artifact_id != self.context_fingerprint:
            raise ValueError("Writer context artifact does not match context fingerprint")
        attestation = self.future_isolation_attestation
        if not attestation.passed or attestation.overlap_source_ids:
            raise ValueError("Writer basis requires a passing future-isolation attestation")
        source_ids = tuple(binding.source_id for binding in self.source_artifacts)
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("Writer source bindings require unique source ids")
        artifact_ids = tuple(
            binding.source_artifact.artifact_id for binding in self.source_artifacts
        )
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("Writer source bindings require unique artifacts")
        if set(source_ids) & set(attestation.evaluator_only_source_ids):
            raise ValueError("Writer source binding references an evaluator-only source")
        canonical_source_ids = set(attestation.canonical_source_ids)
        if canonical_source_ids and not set(source_ids).issubset(canonical_source_ids):
            raise ValueError("Writer source binding is absent from canonical attested sources")
        if (self.memory_gate_report is None) != (self.memory_gate_artifact is None):
            raise ValueError("Writer Memory Gate report and artifact must be supplied together")
        if self.memory_gate_report is not None:
            report = self.memory_gate_report
            if (
                report.verdict
                not in {
                    Stage2GateVerdict.PASS,
                    Stage2GateVerdict.CONDITIONAL_PASS,
                }
                or not report.memory_gateway_frozen
            ):
                raise ValueError("Writer basis requires a frozen passing Memory Gate")
        return self


class WritingLengthPolicy(DomainModel):
    minimum_characters: int = Field(ge=1)
    target_characters: int = Field(ge=1)
    maximum_characters: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_order(self) -> WritingLengthPolicy:
        if not (self.minimum_characters <= self.target_characters <= self.maximum_characters):
            raise ValueError("Writer length policy requires minimum <= target <= maximum")
        return self


class WritingTaskContract(DomainModel):
    contract_id: StableId
    target_chapter: int = Field(ge=1)
    target_scenes: tuple[StableId, ...] = Field(min_length=1)
    pov: _NonEmptyText
    narrative_person: _NonEmptyText
    chapter_goal: _NonEmptyText
    scene_goals: tuple[_NonEmptyText, ...] = ()
    required_beats: tuple[_NonEmptyText, ...] = ()
    active_plan_obligations: tuple[StableId, ...] = ()
    mandatory_constraints: tuple[_NonEmptyText, ...] = ()
    forbidden_reveals: tuple[_NonEmptyText, ...] = ()
    preserve_requirements: tuple[_NonEmptyText, ...] = ()
    style_requirements: tuple[_NonEmptyText, ...] = ()
    length_policy: WritingLengthPolicy
    blocking_gaps: tuple[_NonEmptyText, ...] = ()

    @model_validator(mode="after")
    def validate_targets(self) -> WritingTaskContract:
        if len(self.target_scenes) != len(set(self.target_scenes)):
            raise ValueError("Writer target scene ids must be unique")
        if len(self.active_plan_obligations) != len(set(self.active_plan_obligations)):
            raise ValueError("Writer active plan obligation ids must be unique")
        return self


class AcceptedPlanBinding(DomainModel):
    artifact: ArtifactRef
    revision: _NonEmptyText
    accepted: Literal[True] = True
    task_contract_id: StableId
    base_commit: CommitId
    snapshot_id: StableId


class WritingLoopBudgets(DomainModel):
    max_reactive_memory_rounds: int = Field(default=1, ge=0, le=1)
    max_memory_questions: int = Field(default=3, ge=1, le=8)
    max_local_repairs: int = Field(default=1, ge=0, le=1)
    max_major_rewrites: int = Field(default=1, ge=0, le=1)
    max_writer_turns: int = Field(default=2, ge=1, le=2)
    context_sequence_limit: int = Field(ge=1)
    reserved_output_tokens: int = Field(ge=0)
    context_safety_allowance_tokens: int = Field(ge=0)
    context_soft_limit_tokens: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_context_capacity(self) -> WritingLoopBudgets:
        hard = (
            self.context_sequence_limit
            - self.reserved_output_tokens
            - self.context_safety_allowance_tokens
        )
        if hard < 1 or self.context_soft_limit_tokens > hard:
            raise ValueError("Writing loop Context limits leave no valid input window")
        return self


class WritingLoopRequest(DomainModel):
    run_id: RunId
    task_id: TaskId
    project_id: ProjectId
    base_commit: CommitId
    snapshot_id: StableId
    writing_task: WritingTaskContract
    writing_task_artifact: ArtifactRef
    accepted_plan: AcceptedPlanBinding
    project_profile_artifact: ArtifactRef
    project_profile_revision: _NonEmptyText
    writer_context_package: WriterContextPackage
    writer_context_package_artifact: ArtifactRef
    future_isolation_attestation: FutureIsolationAttestation
    information_scope: Literal["writer_safe"] = "writer_safe"
    mode: AgentMode = AgentMode.DRAFT
    allowed_skills: tuple[StableId, ...] = Field(min_length=1)
    budgets: WritingLoopBudgets
    writer_configuration_fingerprint: ArtifactId
    model_configuration_fingerprint: ArtifactId

    @model_validator(mode="after")
    def validate_basis(self) -> WritingLoopRequest:
        if self.mode not in _WRITER_MODES:
            raise ValueError("Writing loop requires a Writer mode")
        if len(self.allowed_skills) != len(set(self.allowed_skills)):
            raise ValueError("Writing loop allowed Skill ids must be unique")
        if self.accepted_plan.task_contract_id != self.writing_task.contract_id:
            raise ValueError("accepted Plan belongs to another WritingTask")
        if (
            self.accepted_plan.base_commit != self.base_commit
            or self.accepted_plan.snapshot_id != self.snapshot_id
            or self.writer_context_package.basis_commit_id != self.base_commit
            or self.writer_context_package.basis_snapshot_id != self.snapshot_id
        ):
            raise ValueError("WritingTask, accepted Plan, and Writer Context must share a basis")
        target = self.writer_context_package.task_contract
        if not (
            target.target_chapter_start
            <= self.writing_task.target_chapter
            <= target.target_chapter_end
        ):
            raise ValueError("Writer Context target range excludes the WritingTask chapter")
        return self


class WriterWorkPlan(DomainModel):
    work_plan_id: StableId
    writing_task_ref: ArtifactRef
    accepted_plan_ref: ArtifactRef
    writer_context_ref: ArtifactRef
    scene_beat_order: tuple[_NonEmptyText, ...] = Field(min_length=1)
    participating_characters: tuple[_NonEmptyText, ...] = ()
    character_current_states: tuple[_NonEmptyText, ...] = ()
    pov_boundary: _NonEmptyText
    reader_disclosure_boundary: _NonEmptyText
    dialogue_intents: tuple[_NonEmptyText, ...] = ()
    voice_checkpoints: tuple[_NonEmptyText, ...] = ()
    pacing_transitions: tuple[_NonEmptyText, ...] = ()
    emotional_movement: tuple[_NonEmptyText, ...] = ()
    hook_actions: tuple[_NonEmptyText, ...] = ()
    must_keep: tuple[_NonEmptyText, ...] = ()
    must_avoid: tuple[_NonEmptyText, ...] = ()
    unresolved_risks: tuple[_NonEmptyText, ...] = ()
    selected_skill_ids: tuple[StableId, ...] = Field(min_length=1)
    expected_skill_checkpoints: dict[str, tuple[_NonEmptyText, ...]] = Field(default_factory=dict)
    creative_proposals: tuple[_NonEmptyText, ...] = ()

    @model_validator(mode="after")
    def validate_skill_plan(self) -> WriterWorkPlan:
        if len(self.selected_skill_ids) != len(set(self.selected_skill_ids)):
            raise ValueError("WriterWorkPlan selected Skill ids must be unique")
        if not set(self.expected_skill_checkpoints).issubset(
            {item.root for item in self.selected_skill_ids}
        ):
            raise ValueError("WriterWorkPlan checkpoint refers to an unselected Skill")
        return self


class WriterMemoryRequest(DomainModel):
    request_id: StableId
    question: _NonEmptyText
    purpose: _NonEmptyText
    blocked_action: _NonEmptyText
    known_context_item_ids: tuple[StableId, ...] = ()
    requested_evidence_type: _NonEmptyText
    scene_or_draft_checkpoint: _NonEmptyText
    risk: _NonEmptyText
    mandatory_suggestion: bool = False
    anchor_labels: tuple[_NonEmptyText, ...] = ()


class WriterTurnAction(StrEnum):
    DRAFT_READY = "DRAFT_READY"
    REQUEST_MEMORY = "REQUEST_MEMORY"


class WriterTurnOutput(DomainModel):
    action: WriterTurnAction
    draft_text: _NonEmptyText | None = None
    memory_requests: tuple[WriterMemoryRequest, ...] = ()
    declared_memory_hints: tuple[DeclaredMemoryHint, ...] = ()
    unresolved_questions: tuple[_NonEmptyText, ...] = ()
    self_observations: tuple[_NonEmptyText, ...] = ()
    work_plan_checkpoint: _NonEmptyText

    @model_validator(mode="after")
    def validate_action(self) -> WriterTurnOutput:
        if self.action is WriterTurnAction.DRAFT_READY:
            if self.draft_text is None or self.memory_requests:
                raise ValueError("DRAFT_READY requires only draft_text")
        elif self.draft_text is not None or not self.memory_requests:
            raise ValueError("REQUEST_MEMORY requires only memory_requests")
        if len({item.request_id for item in self.memory_requests}) != len(self.memory_requests):
            raise ValueError("Writer memory request ids must be unique")
        return self


class WriterWorkPlanResult(DomainModel):
    work_plan: WriterWorkPlan
    work_plan_artifact: ArtifactRef
    skill_receipts: tuple[SkillExecutionReceipt, ...]
    model_call_record: ModelCallRecord

    @model_validator(mode="after")
    def validate_receipts(self) -> WriterWorkPlanResult:
        selected = set(self.work_plan.selected_skill_ids)
        receipt_skills = {item.skill.contract_id for item in self.skill_receipts}
        if selected != receipt_skills:
            raise ValueError("Skill receipts must exactly cover selected Writer Skills")
        return self


class ContinuationBoundary(DomainModel):
    parent_draft_id: ArtifactId
    frozen_prefix_artifact: ArtifactRef
    frozen_prefix_characters: int = Field(ge=1)


class RewriteScope(StrEnum):
    MAJOR_REWRITE = "major_rewrite"
    LOCAL_REPAIR = "local_repair"


class RewriteDirective(DomainModel):
    directive_id: StableId
    parent_draft_id: ArtifactId
    scope: RewriteScope
    directive_artifact: ArtifactRef
    instructions: tuple[_NonEmptyText, ...] = Field(min_length=1)
    preserve_requirements: tuple[_NonEmptyText, ...] = ()


class WriterBudget(DomainModel):
    max_model_calls: int = Field(default=1, ge=0, le=1)
    max_tool_calls: int = Field(default=0, ge=0, le=0)
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=600.0)
    input_token_limit: int = Field(ge=1)
    output_token_limit: int = Field(ge=1)


class MemoryHintChangeKind(StrEnum):
    ADD = "ADD"
    CHANGE = "CHANGE"
    END = "END"
    UNCERTAIN = "UNCERTAIN"


class DeclaredMemoryHint(DomainModel):
    subject_hint: _NonEmptyText
    change_kind: MemoryHintChangeKind
    predicate_hint: _NonEmptyText | None = None
    value_hint: _NonEmptyText | None = None
    evidence_quote: _NonEmptyText
    confidence: float = Field(ge=0.0, le=1.0)


class WriterDraftPayload(DomainModel):
    """Untrusted model output; it intentionally contains no canonical identities."""

    draft_text: _NonEmptyText
    declared_memory_hints: tuple[DeclaredMemoryHint, ...] = ()
    unresolved_questions: tuple[_NonEmptyText, ...] = ()
    self_observations: tuple[_NonEmptyText, ...] = ()

    @model_validator(mode="after")
    def reject_blank_draft(self) -> WriterDraftPayload:
        if not self.draft_text.strip():
            raise ValueError("Writer draft text must not be blank")
        return self


class WriterAdvisoryFinding(DomainModel):
    hint_index: int = Field(ge=0)
    evidence_quote: _NonEmptyText
    occurrence_count: int = Field(ge=0)
    code: _NonEmptyText
    message: _NonEmptyText


class WriterSidecar(DomainModel):
    declared_memory_hints: tuple[DeclaredMemoryHint, ...] = ()
    unresolved_questions: tuple[_NonEmptyText, ...] = ()
    self_observations: tuple[_NonEmptyText, ...] = ()
    advisory_findings: tuple[WriterAdvisoryFinding, ...] = ()


class DraftArtifact(DomainModel):
    """Content-addressed Writer candidate; never a canonical TextRoot."""

    draft_id: ArtifactId
    mode: AgentMode
    basis: WriterArtifactBasis
    text_artifact: ArtifactRef
    sidecar_artifact: ArtifactRef
    raw_output_artifact: ArtifactRef
    parent_draft_id: ArtifactId | None = None
    writer_receipt: AgentExecutionReceipt
    model_call_ids: tuple[StableId, ...]
    model_call_record: ModelCallRecord | None = None
    created_at: datetime
    candidate_only: Literal[True] = True

    @model_validator(mode="after")
    def validate_candidate_lineage(self) -> DraftArtifact:
        if self.mode not in _WRITER_MODES:
            raise ValueError("DraftArtifact requires a Writer mode")
        if self.mode is AgentMode.DRAFT and self.parent_draft_id is not None:
            raise ValueError("DRAFT artifact cannot have a parent draft")
        if self.mode is not AgentMode.DRAFT and self.parent_draft_id is None:
            raise ValueError("continuation and rewrite artifacts require a parent draft")
        receipt = self.writer_receipt
        if receipt.agent_type is not AgentType.WRITER or receipt.agent_mode is not self.mode:
            raise ValueError("DraftArtifact receipt does not identify this Writer mode")
        if receipt.status is not ExecutionStatus.SUCCEEDED:
            raise ValueError("DraftArtifact requires a successful Writer receipt")
        if receipt.base_commit != self.basis.base_commit:
            raise ValueError("DraftArtifact receipt base commit differs from its basis")
        if receipt.configuration_fingerprint != self.basis.configuration_fingerprint:
            raise ValueError("DraftArtifact receipt Writer configuration fingerprint mismatch")
        if receipt.model_call_ids != self.model_call_ids:
            raise ValueError("DraftArtifact model call ids differ from its receipt")
        outputs = set(receipt.output_artifacts)
        if not {
            self.text_artifact,
            self.sidecar_artifact,
            self.raw_output_artifact,
        }.issubset(outputs):
            raise ValueError("DraftArtifact receipt does not bind every candidate artifact")
        return self


class WriterInvocation(DomainModel):
    invocation_id: StableId
    run_id: RunId
    task_id: TaskId
    mode: AgentMode
    basis: WriterArtifactBasis
    writing_task: WritingTaskContract
    context_package: WriterContextSnapshot
    input_artifacts: tuple[ArtifactRef, ...]
    prior_draft: DraftArtifact | None = None
    continuation_boundary: ContinuationBoundary | None = None
    rewrite_directive: RewriteDirective | None = None
    budget: WriterBudget

    @model_validator(mode="after")
    def validate_invocation(self) -> WriterInvocation:
        if self.mode not in _WRITER_MODES:
            raise ValueError("WriterInvocation requires a Writer mode")
        if self.basis.base_commit != self.context_package.base_commit:
            raise ValueError("Writer basis base commit differs from ContextPackage")
        if self.basis.snapshot_id != self.context_package.snapshot_id:
            raise ValueError("Writer basis snapshot differs from ContextPackage")
        if self.basis.context_id != self.context_package.context_id:
            raise ValueError("Writer basis context id differs from ContextPackage")
        artifact_ids = tuple(artifact.artifact_id for artifact in self.input_artifacts)
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("WriterInvocation input artifacts must be unique")
        if self.mode is AgentMode.DRAFT:
            if any(
                value is not None
                for value in (
                    self.prior_draft,
                    self.continuation_boundary,
                    self.rewrite_directive,
                )
            ):
                raise ValueError("DRAFT forbids prior draft, continuation, and rewrite inputs")
            return self
        if self.prior_draft is None:
            raise ValueError("continuation and rewrite modes require a prior draft")
        if self.mode is AgentMode.CONTINUE:
            if self.continuation_boundary is None or self.rewrite_directive is not None:
                raise ValueError("CONTINUE requires only a continuation boundary")
            if self.continuation_boundary.parent_draft_id != self.prior_draft.draft_id:
                raise ValueError("CONTINUE boundary does not match the prior draft")
            return self
        if self.continuation_boundary is not None or self.rewrite_directive is None:
            raise ValueError("MAJOR_REWRITE requires only a rewrite directive")
        if self.rewrite_directive.parent_draft_id != self.prior_draft.draft_id:
            raise ValueError("MAJOR_REWRITE directive does not match the prior draft")
        if self.rewrite_directive.scope is RewriteScope.LOCAL_REPAIR:
            raise ValueError("Writer MAJOR_REWRITE rejects Editor LOCAL_REPAIR scope")
        return self


class WriterContextHandoffRequest(DomainModel):
    """Formal Stage 3 handoff from the writer-facing Memory product.

    The request deliberately carries the Stage 2M ``WriterContextPackage`` rather than the
    legacy ``Stage1ContextPackage``. Artifact materialization, snapshot conversion, and all
    readiness checks are owned by the handoff adapter; this contract only describes the trusted
    inputs and the requested Writer mode.
    """

    integration_id: StableId
    run_id: RunId
    task_id: TaskId
    project_id: ProjectId
    context_package: WriterContextPackage
    writing_task: WritingTaskContract
    plan_artifact: ArtifactRef
    project_profile_artifact: ArtifactRef
    future_isolation_attestation: FutureIsolationAttestation
    writer_configuration_fingerprint: ArtifactId
    model_configuration_fingerprint: ArtifactId
    budget: WriterBudget
    source_artifacts: tuple[WriterSourceBinding, ...] = ()
    memory_gate_report: Stage2GateReport | None = None
    memory_gate_artifact: ArtifactRef | None = None
    mode: AgentMode = AgentMode.DRAFT
    prior_draft: DraftArtifact | None = None
    continuation_boundary: ContinuationBoundary | None = None
    rewrite_directive: RewriteDirective | None = None

    @model_validator(mode="after")
    def validate_gate_pair(self) -> WriterContextHandoffRequest:
        if (self.memory_gate_report is None) != (self.memory_gate_artifact is None):
            raise ValueError(
                "Writer Context handoff Gate report and artifact must be supplied together"
            )
        return self


class WriterTerminalStatus(StrEnum):
    COMPLETED = "completed"
    NEEDS_CONTEXT = "needs_context"
    CONTRACT_REJECTED = "contract_rejected"
    MODEL_UNAVAILABLE = "model_unavailable"
    MODEL_OUTPUT_REJECTED = "model_output_rejected"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ARTIFACT_WRITE_FAILED = "artifact_write_failed"
    CANCELLED = "cancelled"
    FATAL = "fatal"


class WriterFailureCode(StrEnum):
    NEEDS_CONTEXT = "needs_context"
    CONTRACT_REJECTED = "contract_rejected"
    MODEL_UNAVAILABLE = "model_unavailable"
    MODEL_OUTPUT_REJECTED = "model_output_rejected"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ARTIFACT_WRITE_FAILED = "artifact_write_failed"
    CANCELLED = "cancelled"
    FATAL = "fatal"


class WriterRuntimeFingerprints(DomainModel):
    agent_spec_fingerprint: ArtifactId
    prompt_fingerprint: ArtifactId
    skill_fingerprints: tuple[ArtifactId, ...] = Field(min_length=1)
    tool_policy_fingerprint: ArtifactId
    configuration_fingerprint: ArtifactId
    model_configuration_fingerprint: ArtifactId


class WriterExecutionMetrics(DomainModel):
    model_called: bool
    model_call_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: Decimal = Field(ge=Decimal("0"))
    latency_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_model_call_count(self) -> WriterExecutionMetrics:
        if self.model_called != (self.model_call_count > 0):
            raise ValueError("Writer model_called contradicts model_call_count")
        return self


class WriterExecutionResult(DomainModel):
    result_id: StableId
    invocation_id: StableId
    run_id: RunId
    task_id: TaskId
    status: WriterTerminalStatus
    basis: WriterArtifactBasis
    draft: DraftArtifact | None = None
    receipt: AgentExecutionReceipt | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    fingerprints: WriterRuntimeFingerprints
    metrics: WriterExecutionMetrics
    retry_safe: bool
    failure_code: WriterFailureCode | None = None
    failure_detail: _NonEmptyText | None = None

    @model_validator(mode="after")
    def validate_terminal(self) -> WriterExecutionResult:
        if self.fingerprints.configuration_fingerprint != self.basis.configuration_fingerprint:
            raise ValueError("Writer result configuration fingerprint differs from its basis")
        if (
            self.fingerprints.model_configuration_fingerprint
            != self.basis.model_configuration_fingerprint
        ):
            raise ValueError("Writer result model fingerprint differs from its basis")
        if self.status is WriterTerminalStatus.COMPLETED:
            if self.draft is None or self.receipt is None:
                raise ValueError("COMPLETED Writer result requires DraftArtifact and receipt")
            if self.failure_code is not None:
                raise ValueError("COMPLETED Writer result cannot carry a failure code")
            if self.receipt != self.draft.writer_receipt:
                raise ValueError("Writer result receipt differs from DraftArtifact receipt")
            if (
                self.receipt.run_id != self.run_id
                or self.receipt.task_id != self.task_id
                or self.receipt.base_commit != self.basis.base_commit
            ):
                raise ValueError("Writer result receipt identity differs from the invocation")
            return self
        if self.draft is not None or self.receipt is not None:
            raise ValueError("non-completed Writer result cannot contain DraftArtifact or receipt")
        if self.failure_code is None:
            raise ValueError("failed Writer terminal requires a failure code")
        if self.failure_code.value != self.status.value:
            raise ValueError("Writer failure code contradicts terminal status")
        return self


class WriterShadowManifest(DomainModel):
    manifest_id: StableId
    run_id: RunId
    result: WriterExecutionResult
    artifacts: tuple[ArtifactRef, ...] = ()
    created_at: datetime
    engineering_only: Literal[True] = True
    semantic_quality_not_evaluated: Literal[True] = True
    evaluation_ledger_eligible: Literal[False] = False

    @model_validator(mode="after")
    def validate_run(self) -> WriterShadowManifest:
        if self.result.run_id != self.run_id:
            raise ValueError("Writer shadow manifest result belongs to another run")
        return self


__all__ = [
    "AcceptedPlanBinding",
    "ContinuationBoundary",
    "DeclaredMemoryHint",
    "DraftArtifact",
    "MemoryHintChangeKind",
    "RewriteDirective",
    "RewriteScope",
    "WriterAdvisoryFinding",
    "WriterArtifactBasis",
    "WriterBudget",
    "WriterContextHandoffRequest",
    "WriterContextItem",
    "WriterContextSnapshot",
    "WriterDraftPayload",
    "WriterExecutionMetrics",
    "WriterExecutionResult",
    "WriterFailureCode",
    "WriterInputTaint",
    "WriterInvocation",
    "WriterMemoryRequest",
    "WriterRuntimeFingerprints",
    "WriterShadowManifest",
    "WriterSidecar",
    "WriterSourceBinding",
    "WriterTerminalStatus",
    "WriterTurnAction",
    "WriterTurnOutput",
    "WriterWorkPlan",
    "WriterWorkPlanResult",
    "WritingLengthPolicy",
    "WritingLoopBudgets",
    "WritingLoopRequest",
    "WritingTaskContract",
]
