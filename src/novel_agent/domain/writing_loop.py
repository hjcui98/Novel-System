"""Stage 3 Writer Context Loop terminal and evidence contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from novel_agent.domain.agent_context import (
    AgentContextView,
    ContextCompactionReceipt,
    ContextDelta,
    LoopRoundProgress,
)
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.editorial import (
    CuratorObservation,
    EditorialReport,
    ReconciliationResult,
    RepairedDraft,
)
from novel_agent.domain.generation import (
    DeclaredMemoryHint,
    DraftArtifact,
    WriterContextSnapshot,
    WriterTurnAction,
    WriterTurnOutput,
    WriterWorkPlanResult,
)
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, StableId, TaskId
from novel_agent.domain.model_calls import ModelCallRecord

WRITING_LOOP_CHECKPOINT_MEDIA_TYPE = "application/vnd.novel-agent.writing-loop-checkpoint+json"


class WritingLoopPhase(StrEnum):
    """Durable safe frontiers in the fixed Writer product loop."""

    REACTIVE_MEMORY_PENDING = "REACTIVE_MEMORY_PENDING"
    EDITOR_PENDING = "EDITOR_PENDING"
    OBSERVER_PENDING = "OBSERVER_PENDING"
    RECONCILIATION_PENDING = "RECONCILIATION_PENDING"


class WritingLoopCheckpoint(DomainModel):
    """Minimal durable state for a settled Writer turn awaiting reactive Memory."""

    checkpoint_id: StableId
    run_id: RunId
    task_id: TaskId
    phase: WritingLoopPhase
    base_commit: CommitId
    snapshot_id: StableId
    writing_task_ref: ArtifactRef
    accepted_plan_ref: ArtifactRef
    project_profile_ref: ArtifactRef
    writer_context_ref: ArtifactRef
    recent_prose_ref: ArtifactRef
    work_plan: WriterWorkPlanResult
    active_writer_turn_output: WriterTurnOutput
    active_writer_turn_artifact: ArtifactRef
    active_writer_raw_output_artifact: ArtifactRef
    active_writer_model_call: ModelCallRecord
    memory_rounds: int = Field(ge=0)
    writer_turns: int = Field(ge=1)
    seen_memory_fingerprints: tuple[ArtifactId, ...] = ()
    context_view: AgentContextView
    initial_draft: DraftArtifact | None = None
    editor_context: WriterContextSnapshot | None = None
    rewritten_draft: DraftArtifact | None = None
    repaired_draft: RepairedDraft | None = None
    editorial_reports: tuple[EditorialReport, ...] = ()
    final_candidate_id: ArtifactId | None = None
    final_text_artifact: ArtifactRef | None = None
    final_declared_memory_hints: tuple[DeclaredMemoryHint, ...] = ()
    observation: CuratorObservation | None = None
    observation_artifact: ArtifactRef | None = None
    settled_artifacts: tuple[ArtifactRef, ...] = ()
    round_progress: LoopRoundProgress | None = None

    @model_validator(mode="after")
    def validate_resume_state(self) -> WritingLoopCheckpoint:
        if self.phase is WritingLoopPhase.REACTIVE_MEMORY_PENDING:
            if self.active_writer_turn_output.action is not WriterTurnAction.REQUEST_MEMORY:
                raise ValueError("reactive Writer checkpoint requires a pending Memory request")
            if any(
                item is not None
                for item in (
                    self.initial_draft,
                    self.final_candidate_id,
                    self.final_text_artifact,
                    self.observation,
                    self.observation_artifact,
                )
            ):
                raise ValueError("reactive Writer checkpoint cannot contain a settled Draft")
        elif (
            self.active_writer_turn_output.action is not WriterTurnAction.DRAFT_READY
            or self.initial_draft is None
            or self.final_candidate_id is None
            or self.final_text_artifact is None
        ):
            raise ValueError("post-Draft checkpoint requires the settled Writer candidate")
        if self.phase is WritingLoopPhase.EDITOR_PENDING and self.editor_context is None:
            raise ValueError("Editor-pending checkpoint requires the exact Draft Context")
        if self.phase in {
            WritingLoopPhase.OBSERVER_PENDING,
            WritingLoopPhase.RECONCILIATION_PENDING,
        } and (not self.editorial_reports or self.editorial_reports[-1].verdict.value != "PASS"):
            raise ValueError("post-Editor checkpoint requires a final PASS")
        if self.phase is WritingLoopPhase.RECONCILIATION_PENDING and (
            self.observation is None or self.observation_artifact is None
        ):
            raise ValueError("reconciliation checkpoint requires settled observation")
        if (self.observation is None) != (self.observation_artifact is None):
            raise ValueError("checkpoint observation and Artifact must appear together")
        if len(self.seen_memory_fingerprints) != len(set(self.seen_memory_fingerprints)):
            raise ValueError("Writer checkpoint Memory fingerprints must be unique")
        if (
            self.context_view.run_id != self.run_id
            or self.context_view.task_id != self.task_id
            or self.context_view.base_commit != self.base_commit
            or self.context_view.snapshot_id != self.snapshot_id
            or self.context_view.plan_ref != self.accepted_plan_ref
            or self.context_view.profile_ref != self.project_profile_ref
            or self.work_plan.work_plan.writing_task_ref != self.writing_task_ref
            or self.work_plan.work_plan.accepted_plan_ref != self.accepted_plan_ref
            or self.work_plan.work_plan.writer_context_ref != self.writer_context_ref
            or self.active_writer_model_call.run_id != self.run_id
            or self.active_writer_model_call.task_id != self.task_id
        ):
            raise ValueError("Writer checkpoint lineage differs from its durable request basis")
        return self


class WritingLoopTerminalStatus(StrEnum):
    DRAFT_CANDIDATE_READY = "DRAFT_CANDIDATE_READY"
    YIELDED = "YIELDED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REVIEW_REQUIRED_LOCAL_REPAIR_EXHAUSTED = "REVIEW_REQUIRED_LOCAL_REPAIR_EXHAUSTED"
    REVIEW_REQUIRED_MAJOR_REWRITE_EXHAUSTED = "REVIEW_REQUIRED_MAJOR_REWRITE_EXHAUSTED"
    INPUT_NOT_READY = "INPUT_NOT_READY"
    MISSING_ACCEPTED_PLAN = "MISSING_ACCEPTED_PLAN"
    MEMORY_INSUFFICIENT = "MEMORY_INSUFFICIENT"
    MEMORY_BUDGET_EXHAUSTED = "MEMORY_BUDGET_EXHAUSTED"
    MEMORY_DENIED = "MEMORY_DENIED"
    CONTEXT_LIMIT = "CONTEXT_LIMIT"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    WRITER_FAILED = "WRITER_FAILED"
    EDITOR_FAILED = "EDITOR_FAILED"
    OBSERVER_FAILED = "OBSERVER_FAILED"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
    BASIS_CHANGED = "BASIS_CHANGED"


class WritingLoopResult(DomainModel):
    result_id: StableId
    run_id: RunId
    task_id: TaskId
    status: WritingLoopTerminalStatus
    work_plan: WriterWorkPlanResult | None = None
    initial_draft: DraftArtifact | None = None
    rewritten_draft: DraftArtifact | None = None
    repaired_draft: RepairedDraft | None = None
    final_candidate_id: ArtifactId | None = None
    final_text_artifact: ArtifactRef | None = None
    editorial_reports: tuple[EditorialReport, ...] = ()
    observation: CuratorObservation | None = None
    observation_artifact: ArtifactRef | None = None
    reconciliation: ReconciliationResult | None = None
    context_view: AgentContextView | None = None
    checkpoint_ref: ArtifactRef | None = None
    context_deltas: tuple[ContextDelta, ...] = ()
    compaction_receipts: tuple[ContextCompactionReceipt, ...] = ()
    model_call_records: tuple[ModelCallRecord, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    failure_detail: str | None = Field(default=None, min_length=1)
    candidate_only: Literal[True] = True
    canon_mutated: Literal[False] = False
    memory_patch_generated: Literal[False] = False
    commit_called: Literal[False] = False
    round_progress: LoopRoundProgress | None = None

    @model_validator(mode="after")
    def validate_terminal(self) -> WritingLoopResult:
        ready = self.status is WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY
        resumable = self.status in {
            WritingLoopTerminalStatus.YIELDED,
            WritingLoopTerminalStatus.MEMORY_BUDGET_EXHAUSTED,
        }
        required = (
            self.work_plan,
            self.initial_draft,
            self.final_candidate_id,
            self.final_text_artifact,
            self.observation,
            self.observation_artifact,
            self.reconciliation,
            self.context_view,
        )
        if ready and (any(item is None for item in required) or self.failure_detail is not None):
            raise ValueError("DRAFT_CANDIDATE_READY requires the complete candidate evidence chain")
        if ready and (
            not self.editorial_reports
            or self.editorial_reports[-1].verdict.value != "PASS"
            or self.observation is None
            or self.observation.draft_id != self.final_candidate_id
            or self.reconciliation is None
            or self.reconciliation.draft_id != self.final_candidate_id
        ):
            raise ValueError(
                "ready candidate must pass final review, observation, and reconciliation"
            )
        if not ready and self.failure_detail is None:
            raise ValueError("non-ready Writing loop result requires failure detail")
        if resumable and (
            self.checkpoint_ref is None or self.work_plan is None or self.context_view is None
        ):
            raise ValueError("resumable Writer result requires checkpoint state")
        if (
            self.status is WritingLoopTerminalStatus.MEMORY_BUDGET_EXHAUSTED
            and self.final_candidate_id is not None
        ):
            raise ValueError("Memory-budget yield must precede Draft settlement")
        if resumable and self.checkpoint_ref not in self.artifacts:
            raise ValueError("resumable Writer result must expose its checkpoint artifact")
        if not resumable and self.checkpoint_ref is not None:
            raise ValueError("only resumable Writer results may publish a checkpoint")
        if (self.observation is None) != (self.observation_artifact is None):
            raise ValueError("observation and its Artifact must appear together")
        request_ids = tuple(item.request_id for item in self.model_call_records)
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("Writing loop ModelCallRecords must be unique")
        return self


__all__ = [
    "WRITING_LOOP_CHECKPOINT_MEDIA_TYPE",
    "WritingLoopCheckpoint",
    "WritingLoopPhase",
    "WritingLoopResult",
    "WritingLoopTerminalStatus",
]
