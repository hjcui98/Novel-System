"""Event-derived Agent Context View and safe compaction contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints, TypeAdapter, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, SchemaVersion, StableId, TaskId

_Text = Annotated[str, StringConstraints(min_length=1)]
CONTEXT_EVENT_SCHEMA_VERSION = SchemaVersion("1.0.0")


class ContextConsumer(StrEnum):
    WRITER = "writer"
    PLANNER = "planner"


class ContextLayer(StrEnum):
    PROTECTED = "protected"
    MEMORY = "memory"
    WORKING = "working"
    RECENT_SETTLED = "recent_settled"
    COMPACTED_PREFIX = "compacted_prefix"


class ContextItemKind(StrEnum):
    SYSTEM_POLICY = "system_policy"
    TOOL_POLICY = "tool_policy"
    WRITING_TASK = "writing_task"
    ACCEPTED_PLAN = "accepted_plan"
    PROJECT_PROFILE = "project_profile"
    AUTHOR_INTENT = "author_intent"
    PLANNING_INQUIRY = "planning_inquiry"
    GOAL_PROPOSAL = "goal_proposal"
    RECENT_PROSE = "recent_prose"
    MEMORY_CLAIM = "memory_claim"
    EVIDENCE_HANDLE = "evidence_handle"
    UNRESOLVED_NEED = "unresolved_need"
    WORK_PLAN = "work_plan"
    DRAFT_CHECKPOINT = "draft_checkpoint"
    EDITOR_INSTRUCTION = "editor_instruction"
    MODEL_BATCH = "model_batch"
    TOOL_BATCH = "tool_batch"
    RUNTIME_SUMMARY = "runtime_summary"


class ContextViewItem(DomainModel):
    item_id: StableId
    layer: ContextLayer
    kind: ContextItemKind
    content: _Text
    token_count: int = Field(ge=1)
    source_artifact_refs: tuple[ArtifactRef, ...] = ()
    source_event_range: tuple[int, int] | None = None
    atomic_group_id: StableId | None = None
    supersedes_item_ids: tuple[StableId, ...] = ()
    mandatory: bool = False
    information_scope: Literal["writer_safe", "planner_safe", "runtime"] = "writer_safe"
    instruction_boundary: bool = False
    pending_effect: bool = False

    @model_validator(mode="after")
    def validate_item(self) -> ContextViewItem:
        if self.source_event_range is not None:
            start, end = self.source_event_range
            if start < 1 or end < start:
                raise ValueError("context item event range is invalid")
        if self.kind is ContextItemKind.RUNTIME_SUMMARY and (
            self.information_scope != "runtime" or self.instruction_boundary
        ):
            raise ValueError("runtime summary cannot masquerade as an instruction")
        if self.pending_effect and self.layer is ContextLayer.COMPACTED_PREFIX:
            raise ValueError("pending effects cannot be compacted")
        return self


class ContextDeltaStatus(StrEnum):
    RESOLVED = "RESOLVED"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"
    DENIED = "DENIED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


class ContextDelta(DomainModel):
    delta_id: StableId
    request_ref: ArtifactRef
    resolution_ref: ArtifactRef
    parent_view_revision: int = Field(ge=0)
    base_commit: CommitId | None = None
    snapshot_id: StableId | None = None
    profile_ref: ArtifactRef | None = None
    plan_ref: ArtifactRef | None = None
    added_memory_items: tuple[ContextViewItem, ...] = ()
    superseded_item_ids: tuple[StableId, ...] = ()
    resolved_need_ids: tuple[StableId, ...] = ()
    unresolved_need_ids: tuple[StableId, ...] = ()
    evidence_refs: tuple[ArtifactRef, ...] = ()
    token_impact: int
    information_scope: Literal["writer_safe", "planner_safe"] = "writer_safe"
    status: ContextDeltaStatus

    @model_validator(mode="after")
    def validate_delta(self) -> ContextDelta:
        if (self.base_commit is None) != (self.snapshot_id is None):
            raise ValueError("ContextDelta commit and snapshot basis must appear together")
        if any(item.layer is not ContextLayer.MEMORY for item in self.added_memory_items):
            raise ValueError("ContextDelta can add only memory-layer items")
        if any(
            item.information_scope not in {self.information_scope, "runtime"}
            for item in self.added_memory_items
        ):
            raise ValueError("ContextDelta item scope exceeds the declared information scope")
        ids = tuple(item.item_id for item in self.added_memory_items)
        if len(ids) != len(set(ids)):
            raise ValueError("ContextDelta item ids must be unique")
        if set(ids) & set(self.superseded_item_ids):
            raise ValueError("ContextDelta cannot add and supersede the same item")
        has_content = bool(self.added_memory_items or self.resolved_need_ids)
        if self.status is ContextDeltaStatus.RESOLVED and not has_content:
            raise ValueError("resolved ContextDelta must add evidence or resolve a Need")
        if self.status in {
            ContextDeltaStatus.DENIED,
            ContextDeltaStatus.BUDGET_EXHAUSTED,
        } and (self.added_memory_items or self.resolved_need_ids):
            raise ValueError("denied ContextDelta cannot expose resolved memory")
        return self


class ProviderValidityReceipt(DomainModel):
    receipt_id: StableId
    tokenizer: _Text
    tokenizer_version: _Text
    sequence_limit: int = Field(ge=1)
    reserved_output_tokens: int = Field(ge=0)
    safety_allowance_tokens: int = Field(ge=0)
    rendered_input_tokens: int = Field(ge=0)
    available_input_tokens: int = Field(ge=0)
    atomic_groups_valid: bool
    provider_valid: bool
    context_hash: ArtifactId

    @model_validator(mode="after")
    def validate_capacity(self) -> ProviderValidityReceipt:
        expected = max(
            0,
            self.sequence_limit - self.reserved_output_tokens - self.safety_allowance_tokens,
        )
        if self.available_input_tokens != expected:
            raise ValueError("provider receipt available capacity is inconsistent")
        valid = self.atomic_groups_valid and self.rendered_input_tokens <= expected
        if self.provider_valid != valid:
            raise ValueError("provider receipt validity contradicts token or atomic checks")
        return self


class ContextCompactionReceipt(DomainModel):
    receipt_id: StableId
    run_id: RunId
    parent_view_revision: int = Field(ge=0)
    parent_generation: int = Field(ge=0)
    basis_event_position: int = Field(ge=1)
    covered_event_range: tuple[int, int]
    kept_boundary: int = Field(ge=0)
    removed_item_ids: tuple[StableId, ...]
    compacted_items: tuple[ContextViewItem, ...]
    summary_artifact: ArtifactRef | None = None
    detail_artifact: ArtifactRef | None = None
    input_context_hash: ArtifactId
    output_context_hash: ArtifactId
    deterministic: bool
    safe_cut: bool
    published_generation: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_receipt(self) -> ContextCompactionReceipt:
        start, end = self.covered_event_range
        if start < 1 or end < start or end > self.basis_event_position:
            raise ValueError("compaction receipt event coverage is invalid")
        if self.published_generation != self.parent_generation + 1:
            raise ValueError("compaction generation must advance exactly once")
        if not self.safe_cut:
            raise ValueError("unsafe compaction receipt cannot be published")
        if (self.summary_artifact is None) != (self.detail_artifact is None):
            raise ValueError("summary and detail artifacts must be persisted together")
        return self


class AgentContextView(DomainModel):
    run_id: RunId
    task_id: TaskId
    consumer: ContextConsumer
    revision: int = Field(ge=0)
    generation: int = Field(ge=0)
    basis_event_position: int = Field(ge=0)
    base_commit: CommitId | None = None
    snapshot_id: StableId | None = None
    profile_ref: ArtifactRef | None = None
    plan_ref: ArtifactRef | None = None
    information_scope: Literal["writer_safe", "planner_safe"]
    seed_package_ref: ArtifactRef
    protected_items: tuple[ContextViewItem, ...]
    active_memory_items: tuple[ContextViewItem, ...] = ()
    working_items: tuple[ContextViewItem, ...] = ()
    recent_settled_tail: tuple[ContextViewItem, ...] = ()
    compacted_prefix_items: tuple[ContextViewItem, ...] = ()
    compacted_item_ids: tuple[StableId, ...] = ()
    unresolved_need_ids: tuple[StableId, ...] = ()
    compacted_prefix_ref: ArtifactRef | None = None
    covered_event_range: tuple[int, int] | None = None
    kept_boundary: int = Field(default=0, ge=0)
    token_report: dict[str, int] = Field(default_factory=dict)
    provider_validity_receipt: ProviderValidityReceipt | None = None
    context_hash: ArtifactId

    @model_validator(mode="after")
    def validate_view(self) -> AgentContextView:
        if (self.base_commit is None) != (self.snapshot_id is None):
            raise ValueError("Context View commit and snapshot basis must appear together")
        if self.consumer is ContextConsumer.WRITER and any(
            item is None
            for item in (self.base_commit, self.snapshot_id, self.profile_ref, self.plan_ref)
        ):
            raise ValueError("Writer Context View requires complete accepted project basis")
        if self.consumer is ContextConsumer.WRITER and self.information_scope != "writer_safe":
            raise ValueError("Writer Context View must use writer-safe information scope")
        if self.consumer is ContextConsumer.PLANNER and self.information_scope != "planner_safe":
            raise ValueError("Planner Context View must use planner-safe information scope")
        layers = (
            (self.protected_items, ContextLayer.PROTECTED),
            (self.active_memory_items, ContextLayer.MEMORY),
            (self.working_items, ContextLayer.WORKING),
            (self.recent_settled_tail, ContextLayer.RECENT_SETTLED),
            (self.compacted_prefix_items, ContextLayer.COMPACTED_PREFIX),
        )
        if any(item.layer is not expected for items, expected in layers for item in items):
            raise ValueError("context item is stored in the wrong layer")
        all_items = tuple(item for items, _expected in layers for item in items)
        ids = tuple(item.item_id for item in all_items)
        if len(ids) != len(set(ids)):
            raise ValueError("AgentContextView item ids must be unique")
        if len(self.compacted_item_ids) != len(set(self.compacted_item_ids)):
            raise ValueError("compacted Context item ids must be unique")
        if set(ids) & set(self.compacted_item_ids):
            raise ValueError("active and compacted Context item ids cannot overlap")
        if any(not item.mandatory for item in self.protected_items):
            raise ValueError("protected context items must be mandatory")
        if self.information_scope == "writer_safe" and any(
            item.information_scope == "planner_safe" for item in all_items
        ):
            raise ValueError("Writer Context View contains planner-only information")
        if self.covered_event_range is not None:
            start, end = self.covered_event_range
            if start < 1 or end < start or end > self.basis_event_position:
                raise ValueError("Context View compacted event range is invalid")
        return self


class ContextPressure(DomainModel):
    rendered_input_tokens: int = Field(ge=0)
    available_input_tokens: int = Field(ge=0)
    soft_limit_tokens: int = Field(ge=0)
    hard_limit_tokens: int = Field(ge=0)
    soft_exceeded: bool
    hard_exceeded: bool

    @model_validator(mode="after")
    def validate_pressure(self) -> ContextPressure:
        if self.soft_limit_tokens > self.hard_limit_tokens:
            raise ValueError("soft context limit exceeds hard limit")
        if self.soft_exceeded != (self.rendered_input_tokens > self.soft_limit_tokens):
            raise ValueError("soft pressure flag is inconsistent")
        if self.hard_exceeded != (self.rendered_input_tokens > self.hard_limit_tokens):
            raise ValueError("hard pressure flag is inconsistent")
        return self


class WriterWorkPlanSettledPayload(DomainModel):
    work_plan_ref: ArtifactRef
    working_item: ContextViewItem


class ContextMemoryRequestedPayload(DomainModel):
    request_ref: ArtifactRef
    request_fingerprint: ArtifactId


class ContextMemoryResolvedPayload(DomainModel):
    request_ref: ArtifactRef
    resolution_ref: ArtifactRef
    status: ContextDeltaStatus


class ContextDeltaAppliedPayload(DomainModel):
    delta: ContextDelta


class ContextPressureDetectedPayload(DomainModel):
    pressure: ContextPressure


class ContextCompactedPayload(DomainModel):
    receipt: ContextCompactionReceipt


class SettledArtifactPayload(DomainModel):
    artifact_ref: ArtifactRef
    parent_artifact_ref: ArtifactRef | None = None


_STAGE3_PAYLOADS: dict[str, type[DomainModel]] = {
    "context.view_started": SettledArtifactPayload,
    "writer.work_plan_settled": WriterWorkPlanSettledPayload,
    "context.memory_requested": ContextMemoryRequestedPayload,
    "context.memory_resolved": ContextMemoryResolvedPayload,
    "context.delta_applied": ContextDeltaAppliedPayload,
    "context.pressure_detected": ContextPressureDetectedPayload,
    "context.compacted": ContextCompactedPayload,
    "writer.turn_settled": SettledArtifactPayload,
    "draft.candidate_settled": SettledArtifactPayload,
    "editor.review_settled": SettledArtifactPayload,
    "editor.repair_settled": SettledArtifactPayload,
    "candidate.observation_settled": SettledArtifactPayload,
    "candidate.reconciliation_settled": SettledArtifactPayload,
}


def validate_stage3_event_payload(
    event_type: str,
    schema_version: SchemaVersion,
    payload: JsonValue,
) -> None:
    model = _STAGE3_PAYLOADS.get(event_type)
    if model is None:
        return
    if schema_version != CONTEXT_EVENT_SCHEMA_VERSION:
        raise ValueError("unknown Stage 3 event payload version")
    # RunEvent payloads are a JSON boundary: tuples, enums, and typed identifiers
    # have already been serialized by the producer before the envelope validates.
    # Keep the domain models strict everywhere else while allowing that one
    # explicit representation conversion here.
    TypeAdapter(model).validate_python(payload, strict=False)


__all__ = [
    "CONTEXT_EVENT_SCHEMA_VERSION",
    "AgentContextView",
    "ContextCompactedPayload",
    "ContextCompactionReceipt",
    "ContextConsumer",
    "ContextDelta",
    "ContextDeltaAppliedPayload",
    "ContextDeltaStatus",
    "ContextItemKind",
    "ContextLayer",
    "ContextMemoryRequestedPayload",
    "ContextMemoryResolvedPayload",
    "ContextPressure",
    "ContextPressureDetectedPayload",
    "ContextViewItem",
    "ProviderValidityReceipt",
    "SettledArtifactPayload",
    "WriterWorkPlanSettledPayload",
    "validate_stage3_event_payload",
]
