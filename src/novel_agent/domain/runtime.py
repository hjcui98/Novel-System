"""Operational events, checkpoints, effects, and evaluation contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, JsonValue, TypeAdapter, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import CommitId, ProjectId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.model_calls import (
    ModelCallRecord,
    ModelRole,
    RetrievalInferenceCallRecord,
)

STAGE5_EVENT_SCHEMA_VERSION = SchemaVersion("1.0.0")


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
    RUNTIME_TASK_CREATED = "runtime.task.created"
    RUNTIME_TASK_CLAIMED = "runtime.task.claimed"
    RUNTIME_ATTEMPT_STARTED = "runtime.attempt.started"
    RUNTIME_ATTEMPT_SETTLED = "runtime.attempt.settled"
    RUNTIME_TASK_BLOCKED = "runtime.task.blocked"
    RUNTIME_ACCEPTANCE_RECORDED = "runtime.acceptance.recorded"
    RUNTIME_EFFECT_REQUESTED = "runtime.effect.requested"
    RUNTIME_EFFECT_TERMINAL = "runtime.effect.terminal"
    RUNTIME_CHECKPOINT_SAVED = "runtime.checkpoint.saved"
    RUNTIME_CONTROL_RECORDED = "runtime.control.recorded"
    RUNTIME_WRITER_CLAIMED = "runtime.writer.claimed"
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
        validate_stage5_event_payload(
            self.event_type.value,
            self.payload_schema_version,
            self.payload,
        )


class TaskKind(StrEnum):
    PLAN_CANDIDATE = "plan_candidate"
    PLAN_ACCEPTANCE = "plan_acceptance"
    PLAN_COMMIT = "plan_commit"
    DRAFT_CANDIDATE = "draft_candidate"
    DRAFT_ACCEPTANCE = "draft_acceptance"
    DRAFT_COMMIT = "draft_commit"
    PROJECTION_FRESHNESS = "projection_freshness"
    MAINTENANCE = "maintenance"


class TaskPurpose(StrEnum):
    NORMAL = "normal"
    LOOKAHEAD = "lookahead"
    REPLAN = "replan"
    DERIVED_MAINTENANCE = "derived_maintenance"


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_RETRY = "waiting_retry"
    BUDGET_REVIEW = "budget_review"
    RECOVERY_PENDING = "recovery_pending"
    BLOCKED = "blocked"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttemptOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"


class FailureClass(StrEnum):
    WORKER_STARTUP = "worker_startup"
    WORKER_LEASE_EXPIRED = "worker_lease_expired"
    PROVIDER_TRANSIENT = "provider_transient"
    SCHEDULING_TIMEOUT = "scheduling_timeout"
    SCHEDULING_BUDGET_UNSATISFIABLE = "scheduling_budget_unsatisfiable"
    LEAF_SCHEMA_REJECTED = "leaf_schema_rejected"
    LEAF_REVIEW_REQUIRED = "leaf_review_required"
    CANON_EXTRACTION_GAP = "canon_extraction_gap"
    BASIS_CHANGED = "basis_changed"
    PERMISSION_DENIED = "permission_denied"
    VALIDATION_REJECTED = "validation_rejected"
    COMMIT_CONFLICT = "commit_conflict"
    PROJECTION_FAILED = "projection_failed"
    FRESHNESS_WAITING = "freshness_waiting"
    FRESHNESS_BLOCKED = "freshness_blocked"
    WRITER_LANE_BUSY = "writer_lane_busy"
    EFFECT_UNCERTAIN = "effect_uncertain"
    POISON_LOOP = "poison_loop"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"
    RUNTIME_CAPABILITY_UNAVAILABLE = "runtime_capability_unavailable"
    EXTERNAL_RESOURCE_UNAVAILABLE = "external_resource_unavailable"
    UNKNOWN = "unknown"


class RetryOwner(StrEnum):
    MODEL_GATEWAY = "model_gateway"
    LEAF = "leaf"
    RUNTIME = "runtime"
    RECONCILER = "reconciler"
    ACCEPTANCE = "acceptance"
    TRUSTED_COMMIT = "trusted_commit"
    PROJECTION = "projection"
    FRESHNESS = "freshness"
    OPERATOR = "operator"
    NONE = "none"


class RecoveryCheckpoint(StrEnum):
    LATEST_SETTLED = "latest_settled_checkpoint"
    NONE = "none"


class FailurePolicy(DomainModel):
    retry_owner: RetryOwner
    retryable: bool
    consumes_task_budget: bool
    consumes_creative_budget: bool
    resume_from: RecoveryCheckpoint
    fallback_status: TaskStatus


_FAILURE_POLICIES: dict[FailureClass, FailurePolicy] = {
    FailureClass.WORKER_STARTUP: FailurePolicy(
        retry_owner=RetryOwner.RUNTIME,
        retryable=True,
        consumes_task_budget=True,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.LATEST_SETTLED,
        fallback_status=TaskStatus.WAITING_RETRY,
    ),
    FailureClass.WORKER_LEASE_EXPIRED: FailurePolicy(
        retry_owner=RetryOwner.RUNTIME,
        retryable=True,
        consumes_task_budget=False,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.LATEST_SETTLED,
        fallback_status=TaskStatus.WAITING_RETRY,
    ),
    FailureClass.PROVIDER_TRANSIENT: FailurePolicy(
        retry_owner=RetryOwner.MODEL_GATEWAY,
        retryable=True,
        consumes_task_budget=False,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.LATEST_SETTLED,
        fallback_status=TaskStatus.WAITING_RETRY,
    ),
    FailureClass.SCHEDULING_TIMEOUT: FailurePolicy(
        retry_owner=RetryOwner.MODEL_GATEWAY,
        retryable=True,
        consumes_task_budget=False,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.LATEST_SETTLED,
        fallback_status=TaskStatus.WAITING_RETRY,
    ),
    FailureClass.SCHEDULING_BUDGET_UNSATISFIABLE: FailurePolicy(
        retry_owner=RetryOwner.OPERATOR,
        retryable=False,
        consumes_task_budget=False,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.NONE,
        fallback_status=TaskStatus.BLOCKED,
    ),
    FailureClass.LEAF_SCHEMA_REJECTED: FailurePolicy(
        retry_owner=RetryOwner.LEAF,
        retryable=True,
        consumes_task_budget=True,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.LATEST_SETTLED,
        fallback_status=TaskStatus.WAITING_RETRY,
    ),
    FailureClass.LEAF_REVIEW_REQUIRED: FailurePolicy(
        retry_owner=RetryOwner.LEAF,
        retryable=False,
        consumes_task_budget=False,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.NONE,
        fallback_status=TaskStatus.RECOVERY_PENDING,
    ),
    FailureClass.CANON_EXTRACTION_GAP: FailurePolicy(
        retry_owner=RetryOwner.NONE,
        retryable=False,
        consumes_task_budget=False,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.NONE,
        fallback_status=TaskStatus.BLOCKED,
    ),
    FailureClass.BASIS_CHANGED: FailurePolicy(
        retry_owner=RetryOwner.OPERATOR,
        retryable=False,
        consumes_task_budget=False,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.NONE,
        fallback_status=TaskStatus.BLOCKED,
    ),
    FailureClass.PERMISSION_DENIED: FailurePolicy(
        retry_owner=RetryOwner.OPERATOR,
        retryable=False,
        consumes_task_budget=False,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.NONE,
        fallback_status=TaskStatus.BLOCKED,
    ),
    FailureClass.VALIDATION_REJECTED: FailurePolicy(
        retry_owner=RetryOwner.TRUSTED_COMMIT,
        retryable=False,
        consumes_task_budget=True,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.NONE,
        fallback_status=TaskStatus.BLOCKED,
    ),
    FailureClass.COMMIT_CONFLICT: FailurePolicy(
        retry_owner=RetryOwner.OPERATOR,
        retryable=False,
        consumes_task_budget=True,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.NONE,
        fallback_status=TaskStatus.BLOCKED,
    ),
    FailureClass.PROJECTION_FAILED: FailurePolicy(
        retry_owner=RetryOwner.PROJECTION,
        retryable=True,
        consumes_task_budget=True,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.LATEST_SETTLED,
        fallback_status=TaskStatus.WAITING_RETRY,
    ),
    FailureClass.FRESHNESS_WAITING: FailurePolicy(
        retry_owner=RetryOwner.FRESHNESS,
        retryable=True,
        consumes_task_budget=False,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.LATEST_SETTLED,
        fallback_status=TaskStatus.WAITING_RETRY,
    ),
    FailureClass.FRESHNESS_BLOCKED: FailurePolicy(
        retry_owner=RetryOwner.OPERATOR,
        retryable=False,
        consumes_task_budget=False,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.NONE,
        fallback_status=TaskStatus.BLOCKED,
    ),
    FailureClass.WRITER_LANE_BUSY: FailurePolicy(
        retry_owner=RetryOwner.RUNTIME,
        retryable=True,
        consumes_task_budget=False,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.LATEST_SETTLED,
        fallback_status=TaskStatus.WAITING_RETRY,
    ),
    FailureClass.EFFECT_UNCERTAIN: FailurePolicy(
        retry_owner=RetryOwner.RECONCILER,
        retryable=False,
        consumes_task_budget=False,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.LATEST_SETTLED,
        fallback_status=TaskStatus.RECOVERY_PENDING,
    ),
    FailureClass.POISON_LOOP: FailurePolicy(
        retry_owner=RetryOwner.OPERATOR,
        retryable=False,
        consumes_task_budget=True,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.NONE,
        fallback_status=TaskStatus.BUDGET_REVIEW,
    ),
    FailureClass.BUDGET_EXHAUSTED: FailurePolicy(
        retry_owner=RetryOwner.NONE,
        retryable=False,
        consumes_task_budget=False,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.NONE,
        fallback_status=TaskStatus.BUDGET_REVIEW,
    ),
    FailureClass.CANCELLED: FailurePolicy(
        retry_owner=RetryOwner.NONE,
        retryable=False,
        consumes_task_budget=False,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.NONE,
        fallback_status=TaskStatus.CANCELLED,
    ),
    FailureClass.RUNTIME_CAPABILITY_UNAVAILABLE: FailurePolicy(
        retry_owner=RetryOwner.OPERATOR,
        retryable=False,
        consumes_task_budget=False,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.NONE,
        fallback_status=TaskStatus.RECOVERY_PENDING,
    ),
    FailureClass.EXTERNAL_RESOURCE_UNAVAILABLE: FailurePolicy(
        retry_owner=RetryOwner.OPERATOR,
        retryable=False,
        consumes_task_budget=False,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.NONE,
        fallback_status=TaskStatus.RECOVERY_PENDING,
    ),
    FailureClass.UNKNOWN: FailurePolicy(
        retry_owner=RetryOwner.OPERATOR,
        retryable=False,
        consumes_task_budget=False,
        consumes_creative_budget=False,
        resume_from=RecoveryCheckpoint.NONE,
        fallback_status=TaskStatus.RECOVERY_PENDING,
    ),
}


_FAILURE_CODE_ALIASES = {
    "StructuredGenerationExhausted": FailureClass.LEAF_SCHEMA_REJECTED,
    "structured_generation_exhausted": FailureClass.LEAF_SCHEMA_REJECTED,
    "UpdateWorkerBuildIdCompatibility": FailureClass.RUNTIME_CAPABILITY_UNAVAILABLE,
    "external_resource_unavailable": FailureClass.EXTERNAL_RESOURCE_UNAVAILABLE,
}


def normalize_failure_class(failure: FailureClass | str | None) -> FailureClass | None:
    if failure is None or isinstance(failure, FailureClass):
        return failure
    try:
        return FailureClass(failure)
    except ValueError:
        return _FAILURE_CODE_ALIASES.get(failure, FailureClass.UNKNOWN)


def failure_policy(failure: FailureClass | str) -> FailurePolicy:
    if set(_FAILURE_POLICIES) != set(FailureClass):  # pragma: no cover - import-time invariant
        raise RuntimeError("FailureClass policy mapping is not exhaustive")
    normalized = normalize_failure_class(failure)
    if normalized is None:  # pragma: no cover - type guard for callers outside the contract
        return _FAILURE_POLICIES[FailureClass.UNKNOWN]
    return _FAILURE_POLICIES[normalized]


class TaskEligibility(DomainModel):
    eligible: bool
    status: TaskStatus
    reason_code: str = Field(min_length=1, max_length=128)


class AttemptFence(DomainModel):
    project_id: ProjectId
    task_id: TaskId
    attempt_id: StableId
    claim_token: StableId
    task_revision: int = Field(ge=1)
    writer_generation: int = Field(ge=0)


class TaskRecord(DomainModel):
    task_id: TaskId
    run_id: RunId
    project_id: ProjectId
    kind: TaskKind
    purpose: TaskPurpose = TaskPurpose.NORMAL
    task_revision: int = Field(ge=0)
    status: TaskStatus
    priority: int = 0
    scheduled_for: datetime | None = None
    basis_commit: CommitId
    basis_snapshot: StableId | None = None
    policy_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    permission_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    input_artifact_refs: tuple[ArtifactRef, ...] = ()
    candidate_binding_ref: ArtifactRef | None = None
    dependency_task_ids: tuple[TaskId, ...] = ()
    current_attempt_id: StableId | None = None
    terminal_artifact_refs: tuple[ArtifactRef, ...] = ()
    block_cause: str | None = Field(default=None, max_length=512)
    failure_budget: int = Field(default=3, ge=0)
    retry_tranche_size: int = Field(default=3, ge=1)
    planner_memory_budget_extensions: int = Field(default=0, ge=0)
    writer_generation: int = Field(default=0, ge=0)
    chapter_index: int = Field(default=0, ge=0)
    target_chapters: int = Field(default=1, ge=1)
    horizon_start: int | None = Field(default=None, ge=1)
    horizon_end: int | None = Field(default=None, ge=1)
    protected_chapter_index: int | None = Field(default=None, ge=1)
    affects_future_plan: bool | None = None
    projection_after: str | None = Field(default=None, pattern=r"^(plan|draft)$")
    paused: bool = False
    cancel_requested: bool = False
    superseded: bool = False

    @model_validator(mode="after")
    def validate_purpose(self) -> TaskRecord:
        if (self.horizon_start is None) != (self.horizon_end is None):
            raise ValueError("task horizon bounds must appear together")
        if (
            self.horizon_start is not None
            and self.horizon_end is not None
            and self.horizon_end < self.horizon_start
        ):
            raise ValueError("task horizon end precedes start")
        if self.purpose is TaskPurpose.LOOKAHEAD and (
            self.kind not in {TaskKind.PLAN_CANDIDATE, TaskKind.PLAN_ACCEPTANCE}
            or self.protected_chapter_index is None
            or self.horizon_start is None
            or self.horizon_start <= self.protected_chapter_index
        ):
            raise ValueError("lookahead requires a future Plan horizon and protected chapter")
        if self.purpose is TaskPurpose.DERIVED_MAINTENANCE and (
            self.kind is not TaskKind.MAINTENANCE or not self.input_artifact_refs
        ):
            raise ValueError(
                "derived maintenance requires a maintenance task with a finding artifact"
            )
        if self.affects_future_plan is not None and self.kind not in {
            TaskKind.DRAFT_ACCEPTANCE,
            TaskKind.DRAFT_COMMIT,
            TaskKind.PROJECTION_FRESHNESS,
        }:
            raise ValueError("future-Plan impact belongs only to the Draft commit chain")
        return self


class TaskAttempt(DomainModel):
    attempt_id: StableId
    task_id: TaskId
    attempt_no: int = Field(ge=1)
    worker_id: str = Field(min_length=1, max_length=128)
    claim_token_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    fence_generation: int = Field(ge=1)
    claimed_at: datetime
    heartbeat_at: datetime | None = None
    lease_expires_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    source_checkpoint_id: StableId | None = None
    effect_frontier: tuple[StableId, ...] = ()
    outcome: AttemptOutcome | None = None
    failure_class: FailureClass | None = None

    @model_validator(mode="after")
    def validate_lease(self) -> TaskAttempt:
        if (self.heartbeat_at is None) != (self.lease_expires_at is None):
            raise ValueError("Attempt heartbeat and lease expiry must appear together")
        if self.heartbeat_at is None or self.lease_expires_at is None:
            return self
        if self.heartbeat_at < self.claimed_at:
            raise ValueError("Attempt heartbeat cannot precede claim")
        if self.lease_expires_at <= self.heartbeat_at:
            raise ValueError("Attempt lease must expire after its heartbeat")
        return self


def evaluate_task_eligibility(
    task: TaskRecord,
    *,
    now: datetime,
    current_commit: CommitId,
    dependency_statuses: tuple[TaskStatus, ...],
    permission_hash: str,
    writer_generation: int,
) -> TaskEligibility:
    """Single pure eligibility definition shared by recompute, claim, and resume."""

    if task.status is TaskStatus.BUDGET_REVIEW:
        return TaskEligibility(
            eligible=False,
            status=TaskStatus.BUDGET_REVIEW,
            reason_code="budget_extension_required",
        )
    if task.status is not TaskStatus.READY:
        return TaskEligibility(
            eligible=False, status=task.status, reason_code="status_not_claimable"
        )
    if task.current_attempt_id is not None:
        return TaskEligibility(
            eligible=False, status=TaskStatus.RECOVERY_PENDING, reason_code="attempt_active"
        )
    if task.paused or task.superseded:
        return TaskEligibility(
            eligible=False, status=TaskStatus.PENDING, reason_code="paused_or_superseded"
        )
    if task.failure_budget <= 0:
        return TaskEligibility(
            eligible=False,
            status=TaskStatus.BUDGET_REVIEW,
            reason_code="failure_budget_exhausted",
        )
    if task.scheduled_for is not None and task.scheduled_for > now:
        return TaskEligibility(
            eligible=False, status=TaskStatus.PENDING, reason_code="scheduled_for_future"
        )
    if any(status is not TaskStatus.SUCCEEDED for status in dependency_statuses):
        return TaskEligibility(
            eligible=False, status=TaskStatus.PENDING, reason_code="dependency_not_succeeded"
        )
    if task.basis_commit != current_commit:
        return TaskEligibility(
            eligible=False, status=TaskStatus.BLOCKED, reason_code="basis_changed"
        )
    if task.permission_hash != permission_hash:
        return TaskEligibility(
            eligible=False, status=TaskStatus.BLOCKED, reason_code="permission_changed"
        )
    if task.writer_generation != writer_generation:
        return TaskEligibility(
            eligible=False, status=TaskStatus.BLOCKED, reason_code="writer_generation_changed"
        )
    return TaskEligibility(eligible=True, status=TaskStatus.READY, reason_code="eligible")


class TaskCreatedPayload(DomainModel):
    task: TaskRecord


class TaskClaimedPayload(DomainModel):
    attempt: TaskAttempt
    fence_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class TaskAttemptStartedPayload(DomainModel):
    attempt_id: StableId
    worker_id: str = Field(min_length=1, max_length=128)
    started_at: datetime


class TaskAttemptSettledPayload(DomainModel):
    attempt_id: StableId
    outcome: AttemptOutcome
    task_status: TaskStatus
    failure_class: FailureClass | None = None
    block_cause: str | None = Field(default=None, max_length=512)
    terminal_artifact_refs: tuple[ArtifactRef, ...] = ()
    ended_at: datetime


class TaskBlockedPayload(DomainModel):
    failure_class: FailureClass
    cause_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sanitized_message: str = Field(min_length=1, max_length=512)
    error_artifact_ref: ArtifactRef | None = None


class EffectRequestedPayload(DomainModel):
    effect: EffectReceipt
    task_id: TaskId
    attempt_id: StableId


class EffectTerminalPayload(DomainModel):
    effect: EffectReceipt
    task_id: TaskId
    attempt_id: StableId


class CheckpointCreatedPayload(DomainModel):
    checkpoint: RunCheckpoint
    task_id: TaskId
    attempt_id: StableId


class ControlIntentPayload(DomainModel):
    command_id: StableId
    action: str = Field(min_length=1, max_length=64)
    actor_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=512)
    additional_attempts: int = Field(default=0, ge=0)
    additional_planner_memory_tranches: int = Field(default=0, ge=0)
    writer_generation_after: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_budget_extension(self) -> ControlIntentPayload:
        if self.action == "extend_budget" and not (
            self.additional_attempts or self.additional_planner_memory_tranches
        ):
            raise ValueError("budget extension must add a retry or Planner Memory tranche")
        if self.writer_generation_after is not None and (
            self.action not in {"retry", "unblock"} or self.writer_generation_after != 0
        ):
            raise ValueError("only retry or unblock may release writer generation")
        return self


class AcceptanceRecordedPayload(DomainModel):
    command_id: StableId
    receipt_id: StableId
    candidate_id: StableId
    candidate_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    decision: str = Field(min_length=1, max_length=32)
    actor_kind: str = Field(min_length=1, max_length=32)


class WriterClaimedPayload(DomainModel):
    attempt_id: StableId
    writer_generation: int = Field(ge=1)


_STAGE5_PAYLOADS: dict[str, type[DomainModel]] = {
    RunEventType.RUNTIME_TASK_CREATED.value: TaskCreatedPayload,
    RunEventType.RUNTIME_TASK_CLAIMED.value: TaskClaimedPayload,
    RunEventType.RUNTIME_ATTEMPT_STARTED.value: TaskAttemptStartedPayload,
    RunEventType.RUNTIME_ATTEMPT_SETTLED.value: TaskAttemptSettledPayload,
    RunEventType.RUNTIME_TASK_BLOCKED.value: TaskBlockedPayload,
    RunEventType.RUNTIME_EFFECT_REQUESTED.value: EffectRequestedPayload,
    RunEventType.RUNTIME_EFFECT_TERMINAL.value: EffectTerminalPayload,
    RunEventType.RUNTIME_CHECKPOINT_SAVED.value: CheckpointCreatedPayload,
    RunEventType.RUNTIME_CONTROL_RECORDED.value: ControlIntentPayload,
    RunEventType.RUNTIME_ACCEPTANCE_RECORDED.value: AcceptanceRecordedPayload,
    RunEventType.RUNTIME_WRITER_CLAIMED.value: WriterClaimedPayload,
}


def validate_stage5_event_payload(
    event_type: str,
    schema_version: SchemaVersion,
    payload: JsonValue,
) -> None:
    model = _STAGE5_PAYLOADS.get(event_type)
    if model is None:
        return
    if schema_version != STAGE5_EVENT_SCHEMA_VERSION:
        raise ValueError("unknown Stage 5 event payload version")
    TypeAdapter(model).validate_python(payload, strict=False)


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
