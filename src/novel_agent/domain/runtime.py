"""Operational events, checkpoints, effects, and evaluation contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, JsonValue, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import CommitId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.model_calls import (
    ModelCallRecord,
    ModelRole,
    RetrievalInferenceCallRecord,
)


class RunEventType(StrEnum):
    RUN_CREATED = "run.created"
    RUN_RESUMED = "run.resumed"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    TASK_STARTED = "task.started"
    TASK_SUSPENDED = "task.suspended"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    MODEL_REQUESTED = "model.requested"
    MODEL_COMPLETED = "model.completed"
    MODEL_FAILED = "model.failed"
    AGENT_STARTED = "agent.started"
    AGENT_COMPLETED = "agent.completed"
    AGENT_FAILED = "agent.failed"
    TOOL_REQUESTED = "tool.requested"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    ARTIFACT_PRODUCED = "artifact.produced"
    ARTIFACT_SUPERSEDED = "artifact.superseded"
    COMMIT_REQUESTED = "commit.requested"
    COMMIT_ACCEPTED = "commit.accepted"
    COMMIT_REJECTED = "commit.rejected"
    EFFECT_REQUESTED = "effect.requested"
    EFFECT_COMPLETED = "effect.completed"
    EFFECT_UNCERTAIN = "effect.uncertain"
    CHECKPOINT_CREATED = "checkpoint.created"
    CANDIDATE_PROPOSED = "candidate.proposed"
    CANDIDATE_NORMALIZED = "candidate.normalized"
    CANDIDATE_REPAIRED = "candidate.repaired"
    CANDIDATE_VALIDATED = "candidate.validated"
    CANDIDATE_QUARANTINED = "candidate.quarantined"
    CURATOR_PROPOSAL_ATTEMPT_REQUESTED = "curator_proposal.attempt_requested"
    CURATOR_PROPOSAL_ATTEMPT_COMPLETED = "curator_proposal.attempt_completed"
    CURATOR_PROPOSAL_SCHEMA_REJECTED = "curator_proposal.schema_rejected"
    CURATOR_PROPOSAL_SEMANTIC_REJECTED = "curator_proposal.semantic_rejected"
    CURATOR_PROPOSAL_DETERMINISTICALLY_MERGED = "curator_proposal.deterministically_merged"
    CURATOR_PROPOSAL_RETRY_SCHEDULED = "curator_proposal.retry_scheduled"
    CURATOR_PROPOSAL_POISON_LOOP = "curator_proposal.poison_loop"
    CURATOR_PROPOSAL_BUDGET_EXHAUSTED = "curator_proposal.budget_exhausted"
    CURATOR_PROPOSAL_HUMAN_REQUIRED = "curator_proposal.human_required"
    REPAIR_DECIDED = "repair.decided"
    REPAIR_EXHAUSTED = "repair.exhausted"
    GUARDIAN_REQUESTED = "guardian.requested"
    GUARDIAN_COMPLETED = "guardian.completed"
    WORKFLOW_SUSPENDED = "workflow.suspended"
    WORKFLOW_RESUMED = "workflow.resumed"
    PROJECTION_WAITING = "projection.waiting"
    FRESHNESS_PASSED = "freshness.passed"
    INFORMATION_BOUNDARY_VERIFIED = "information_boundary.verified"
    ROOT_UPDATE_MATERIALIZED = "root_update.materialized"
    CONTEXT_VIEW_STARTED = "context.view_started"
    WRITER_WORK_PLAN_SETTLED = "writer.work_plan_settled"
    CONTEXT_MEMORY_REQUESTED = "context.memory_requested"
    CONTEXT_MEMORY_RESOLVED = "context.memory_resolved"
    CONTEXT_DELTA_APPLIED = "context.delta_applied"
    CONTEXT_PRESSURE_DETECTED = "context.pressure_detected"
    CONTEXT_COMPACTED = "context.compacted"
    WRITER_TURN_SETTLED = "writer.turn_settled"
    DRAFT_CANDIDATE_SETTLED = "draft.candidate_settled"
    EDITOR_REVIEW_SETTLED = "editor.review_settled"
    EDITOR_REPAIR_SETTLED = "editor.repair_settled"
    CANDIDATE_OBSERVATION_SETTLED = "candidate.observation_settled"
    CANDIDATE_RECONCILIATION_SETTLED = "candidate.reconciliation_settled"
    REQUEST_MEMORY = "context.request_memory"
    PLAN_REVIEW_SETTLED = "planning.plan_review_settled"
    CONTEXT_PRESSURE = "context.pressure"


class RunEvent(DomainModel):
    event_id: StableId
    run_id: RunId
    task_id: TaskId | None = None
    sequence_no: int = Field(ge=1)
    event_type: RunEventType
    occurred_at: datetime
    idempotency_identity: StableId
    payload_schema_version: SchemaVersion
    trace_id: str = Field(min_length=1)
    span_id: str | None = Field(default=None, min_length=1)
    payload: JsonValue
    artifact_refs: tuple[ArtifactRef, ...] = ()
    model_call_record: ModelCallRecord | RetrievalInferenceCallRecord | None = None
    agent_spec_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    prompt_fingerprint: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    skill_hashes: tuple[str, ...] = ()
    tool_policy_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_model_call_audit(self) -> RunEvent:
        record = self.model_call_record
        if self.event_type is RunEventType.MODEL_COMPLETED and record is None:
            raise ValueError("model.completed requires a complete model_call_record")
        if record is None:
            self._validate_active_payload()
            return self
        if self.event_type not in {
            RunEventType.MODEL_REQUESTED,
            RunEventType.MODEL_COMPLETED,
            RunEventType.MODEL_FAILED,
        }:
            raise ValueError("model_call_record is only valid on model events")
        if record.run_id != self.run_id or record.task_id != self.task_id:
            raise ValueError("model_call_record must belong to the event run and task")
        if record.trace_id != self.trace_id or record.span_id != self.span_id:
            raise ValueError("model_call_record must use the event trace and span")
        self._validate_active_payload()
        return self

    def _validate_active_payload(self) -> None:
        from novel_agent.domain.agent_context import validate_stage3_event_payload

        validate_stage3_event_payload(
            self.event_type.value,
            self.payload_schema_version,
            self.payload,
        )


class ResumabilityStatus(StrEnum):
    RESUMABLE = "resumable"
    BLOCKED = "blocked"
    TERMINAL = "terminal"


class RunCheckpoint(DomainModel):
    checkpoint_id: StableId
    run_id: RunId
    event_position: int = Field(ge=1)
    logical_stage: str = Field(min_length=1)
    state_artifact_ref: ArtifactRef
    completed_effect_ids: tuple[StableId, ...] = ()
    resumability_status: ResumabilityStatus
    reason: str | None = None


class EffectStatus(StrEnum):
    REQUESTED = "requested"
    COMPLETED = "completed"
    UNCERTAIN = "uncertain"
    COMPENSATED = "compensated"


class EffectReceipt(DomainModel):
    effect_identity: StableId
    external_system: str = Field(min_length=1)
    request_identity: StableId
    status: EffectStatus
    attempt_no: int = Field(ge=1)
    provider_request_id: str | None = None
    result_artifact_ref: ArtifactRef | None = None
    completed_at: datetime | None = None


class EvaluationDecision(StrEnum):
    SELECTED = "selected"
    REJECTED = "rejected"
    INFORMATIONAL = "informational"


class EvaluationMetric(DomainModel):
    name: str = Field(min_length=1)
    value: float
    unit: str | None = None


class EvaluationEntry(DomainModel):
    evaluation_id: StableId
    run_id: RunId
    candidate_id: StableId | None = None
    commit_id: CommitId | None = None
    evaluator: str = Field(min_length=1)
    evaluator_version: str = Field(min_length=1)
    model_role: ModelRole | None = None
    model_endpoint: str | None = None
    model_version: str | None = None
    model_cost_usd: Decimal | None = Field(default=None, ge=Decimal("0"))
    model_latency_ms: int | None = Field(default=None, ge=0)
    rubric_version: str = Field(min_length=1)
    metrics: tuple[EvaluationMetric, ...] = ()
    failure_codes: tuple[str, ...] = ()
    evidence_artifacts: tuple[ArtifactRef, ...] = ()
    decision: EvaluationDecision
    created_at: datetime

    @model_validator(mode="after")
    def validate_model_audit(self) -> EvaluationEntry:
        audit_values = (
            self.model_endpoint,
            self.model_version,
            self.model_cost_usd,
            self.model_latency_ms,
        )
        if self.model_role is None and any(value is not None for value in audit_values):
            raise ValueError("model audit metadata requires model_role")
        if self.model_role is not None and any(value is None for value in audit_values):
            raise ValueError("model evaluation requires complete endpoint/version/cost/latency")
        return self
