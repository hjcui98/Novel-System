"""Stage 3 Writer Context Loop terminal and evidence contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from novel_agent.domain.agent_context import (
    AgentContextView,
    ContextCompactionReceipt,
    ContextDelta,
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
    DraftArtifact,
    WriterWorkPlanResult,
)
from novel_agent.domain.ids import ArtifactId, RunId, StableId, TaskId
from novel_agent.domain.model_calls import ModelCallRecord


class WritingLoopTerminalStatus(StrEnum):
    DRAFT_CANDIDATE_READY = "DRAFT_CANDIDATE_READY"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REVIEW_REQUIRED_LOCAL_REPAIR_EXHAUSTED = "REVIEW_REQUIRED_LOCAL_REPAIR_EXHAUSTED"
    REVIEW_REQUIRED_MAJOR_REWRITE_EXHAUSTED = "REVIEW_REQUIRED_MAJOR_REWRITE_EXHAUSTED"
    INPUT_NOT_READY = "INPUT_NOT_READY"
    MISSING_ACCEPTED_PLAN = "MISSING_ACCEPTED_PLAN"
    MEMORY_INSUFFICIENT = "MEMORY_INSUFFICIENT"
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
    context_deltas: tuple[ContextDelta, ...] = ()
    compaction_receipts: tuple[ContextCompactionReceipt, ...] = ()
    model_call_records: tuple[ModelCallRecord, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    failure_detail: str | None = Field(default=None, min_length=1)
    candidate_only: Literal[True] = True
    canon_mutated: Literal[False] = False
    memory_patch_generated: Literal[False] = False
    commit_called: Literal[False] = False

    @model_validator(mode="after")
    def validate_terminal(self) -> WritingLoopResult:
        ready = self.status is WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY
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
        if (self.observation is None) != (self.observation_artifact is None):
            raise ValueError("observation and its Artifact must appear together")
        request_ids = tuple(item.request_id for item in self.model_call_records)
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("Writing loop ModelCallRecords must be unique")
        return self


__all__ = ["WritingLoopResult", "WritingLoopTerminalStatus"]
