"""Provider-neutral model request, response, usage, and audit contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, JsonValue, model_validator

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
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=600.0)
    enable_thinking: bool | None = None
    thinking_token_budget: int | None = Field(default=None, ge=0, le=131072)
    scheduling_need_id: StableId | None = None
    scheduling_stage: str | None = Field(default=None, min_length=1)
    scheduling_dependency_ids: tuple[StableId, ...] = ()
    scheduling_priority: int = Field(default=50, ge=0, le=100)
    scheduling_timeout_seconds: float | None = Field(default=None, gt=0.0, le=3600.0)
    repetition_penalty: float | None = Field(default=None, gt=0.0, le=2.0)


class ModelUsage(DomainModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    cost_usd: Decimal = Field(ge=Decimal("0"))


class ProviderModelResult(DomainModel):
    text: str
    model_version: str = Field(min_length=1)
    usage: ModelUsage


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
    TRANSPORT_EXHAUSTED = "transport_exhausted"
    UNCERTAIN = "uncertain"


class ModelCallLedgerEntry(DomainModel):
    request_id: StableId
    run_id: RunId
    task_id: TaskId
    request_hash: ArtifactId
    status: ModelCallLedgerStatus
    raw_response_hash: ArtifactId | None = None
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
