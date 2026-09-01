"""Provider-neutral model request, response, usage, and audit contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, JsonValue, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, RunId, StableId, TaskId


class ModelRole(StrEnum):
    IMPLEMENTATION = "implementation_model"
    BATCH_TEST = "batch_test_model"


class ModelCallPurpose(StrEnum):
    DEVELOPMENT = "development"
    BATCH_TEST = "batch_test"
    EVALUATION = "evaluation"


class RetrievalInferenceOperation(StrEnum):
    EMBEDDING = "embedding"
    RERANK = "rerank"


class RetrievalInferenceStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class BudgetSource(StrEnum):
    EXPLICIT_REQUEST = "explicit_request"
    INVOCATION_BUDGET = "invocation_budget"
    ENDPOINT_DEFAULT = "endpoint_default"
    MODEL_MAX_AUTO = "model_max_auto"


class BudgetResolutionProfile(StrEnum):
    CANARY = "canary"
    STRICT = "strict"


class RetrievalInferenceUsage(DomainModel):
    input_items: int = Field(ge=0)
    input_characters: int = Field(ge=0)
    output_items: int = Field(ge=0)
    cost_usd: Decimal = Field(ge=Decimal("0"))


class RetrievalInferenceCallRecord(DomainModel):
    call_id: StableId
    run_id: RunId
    task_id: TaskId
    model_role: ModelRole
    purpose: ModelCallPurpose
    trace_id: str = Field(min_length=1)
    span_id: str | None = Field(default=None, min_length=1)
    endpoint: str = Field(min_length=1)
    model: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    runtime_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: RetrievalInferenceOperation
    usage: RetrievalInferenceUsage
    latency_ms: int = Field(ge=0)
    started_at: datetime
    completed_at: datetime
    status: RetrievalInferenceStatus
    error_type: str | None = Field(default=None, min_length=1)


class ModelRequest(DomainModel):
    request_id: StableId
    run_id: RunId
    task_id: TaskId
    attempt_id: StableId | None = None
    model_role: ModelRole
    purpose: ModelCallPurpose
    trace_id: str = Field(min_length=1)
    span_id: str | None = Field(default=None, min_length=1)
    prompt: str
    agent_id: StableId | None = None
    agent_mode: str | None = Field(default=None, min_length=1)
    agent_spec_hash: ArtifactId | None = None
    prompt_contract_hashes: tuple[ArtifactId, ...] = ()
    skill_contract_hashes: tuple[ArtifactId, ...] = ()
    tool_policy_hash: ArtifactId | None = None
    render_fingerprint: ArtifactId | None = None
    response_schema: dict[str, JsonValue] | None = None
    max_output_tokens: int | None = Field(default=None, ge=1, le=131072)
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=900.0)
    enable_thinking: bool | None = None
    thinking_token_budget: int | None = Field(default=None, ge=0, le=131072)
    scheduling_need_id: StableId | None = None
    scheduling_stage: str | None = Field(default=None, min_length=1)
    scheduling_dependency_ids: tuple[StableId, ...] = ()
    scheduling_priority: int = Field(default=50, ge=0, le=100)
    scheduling_timeout_seconds: float | None = Field(default=None, gt=0.0, le=3600.0)
    repetition_penalty: float | None = Field(default=None, gt=0.0, le=2.0)
    budget_source: BudgetSource | None = None


class EffectiveBudgetResult(DomainModel):
    """One resolved output budget shared by API payload, admission, and ledger."""

    budget_source: BudgetSource
    context_limit: int = Field(ge=1)
    estimated_input_tokens: int = Field(ge=0)
    body_output_budget: int = Field(ge=1)
    thinking_budget: int = Field(ge=0)
    total_output_budget: int = Field(ge=1)
    safety_allowance_tokens: int = Field(ge=0)
    reserved_sequence_tokens: int = Field(ge=1)
    available_input_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_budget_identity(self) -> EffectiveBudgetResult:
        expected = (
            self.estimated_input_tokens + self.total_output_budget + self.safety_allowance_tokens
        )
        if self.reserved_sequence_tokens != expected:
            raise ValueError("reserved sequence tokens must equal input, output, and safety")
        available = max(
            0, self.context_limit - self.total_output_budget - self.safety_allowance_tokens
        )
        if self.available_input_tokens != available:
            raise ValueError("available input tokens contradict the sequence identity")
        return self


class ModelCostAvailability(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class ModelUsage(DomainModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    cost_usd: Decimal = Field(ge=Decimal("0"))
    cost_availability: ModelCostAvailability = ModelCostAvailability.UNKNOWN


class ProviderModelResult(DomainModel):
    text: str
    model_version: str = Field(min_length=1)
    usage: ModelUsage
    provider_request_id: str | None = Field(default=None, min_length=1)


class ModelCallRecord(DomainModel):
    request_id: StableId
    run_id: RunId
    task_id: TaskId
    model_role: ModelRole
    purpose: ModelCallPurpose
    trace_id: str = Field(min_length=1)
    span_id: str | None = Field(default=None, min_length=1)
    endpoint: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    usage: ModelUsage
    latency_ms: int = Field(ge=0)
    started_at: datetime
    completed_at: datetime


class ModelTextResult(DomainModel):
    text: str
    call_record: ModelCallRecord


class ModelCallLedgerStatus(StrEnum):
    """Durable lifecycle for one unique provider generation request."""

    REQUESTED = "requested"
    COMPLETED = "completed"
    VALIDATION_REJECTED = "validation_rejected"
    OUTPUT_INCOMPLETE = "output_incomplete"
    TRANSPORT_EXHAUSTED = "transport_exhausted"
    UNCERTAIN = "uncertain"


class ModelCallLedgerEntry(DomainModel):
    request_id: StableId
    run_id: RunId
    task_id: TaskId
    attempt_id: StableId | None = None
    request_hash: ArtifactId
    effective_budget: EffectiveBudgetResult
    reasoning_included_in_completion_tokens: bool
    status: ModelCallLedgerStatus
    logical_phase: str = Field(default="unknown", min_length=1)
    provider_request_id: str | None = Field(default=None, min_length=1)
    provider_sent_at: datetime | None = None
    raw_response_hash: ArtifactId | None = None
    raw_artifact_ref: ArtifactRef | None = None
    call_record: ModelCallRecord | None = None
    validation_error: str | None = Field(default=None, max_length=4096)
    transport_error_type: str | None = Field(default=None, min_length=1, max_length=240)
    requested_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_terminal_evidence(self) -> ModelCallLedgerEntry:
        if self.status in {
            ModelCallLedgerStatus.COMPLETED,
            ModelCallLedgerStatus.VALIDATION_REJECTED,
            ModelCallLedgerStatus.OUTPUT_INCOMPLETE,
        } and (
            self.raw_response_hash is None or self.call_record is None or self.completed_at is None
        ):
            raise ValueError("completed model call requires response hash, call, and completion")
        if (
            self.status is ModelCallLedgerStatus.VALIDATION_REJECTED
            and self.validation_error is None
        ):
            raise ValueError("validation-rejected model call requires safe validation detail")
        if self.status is ModelCallLedgerStatus.TRANSPORT_EXHAUSTED and (
            self.transport_error_type is None or self.completed_at is None
        ):
            raise ValueError("transport exhaustion requires error evidence and completion")
        return self


class RawModelResponseArtifact(DomainModel):
    """Immutable provider response envelope stored before structured parsing."""

    artifact_version: str = Field(min_length=1)
    request_id: StableId
    run_id: RunId
    task_id: TaskId
    attempt_id: StableId | None = None
    request_hash: ArtifactId
    logical_phase: str = Field(default="unknown", min_length=1)
    model_role: ModelRole
    purpose: ModelCallPurpose
    endpoint: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    provider_request_id: str | None = Field(default=None, min_length=1)
    prompt_hash: ArtifactId
    response_schema_hash: ArtifactId | None = None
    raw_response_hash: ArtifactId
    raw_response_text: str
    call_record: ModelCallRecord
    finish_reason: str | None = Field(default=None, min_length=1)


class ModelCallLedgerAggregate(DomainModel):
    """Durable call usage grouped by run, task, attempt, and logical phase."""

    run_id: RunId
    task_id: TaskId
    attempt_id: StableId | None = None
    logical_phase: str = Field(min_length=1)
    request_count: int = Field(ge=0)
    schema_retry_count: int = Field(ge=0)
    status_counts: dict[str, int]
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=Decimal("0"))
    cost_availability: ModelCostAvailability
