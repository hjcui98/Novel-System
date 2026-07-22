"""Provider-neutral model request, response, usage, and audit contracts."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, JsonValue

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
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=600.0)


class ModelUsage(DomainModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
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
