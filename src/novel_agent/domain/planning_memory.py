"""Data contracts for the LLM Need Planner pipeline (Phase 1).

The planner pipeline is three layers: the LLM produces semantic drafts (no
graph ids), the deterministic grounder binds mentions to canonical world
records, and the validator emits final ``Stage1MemoryNeed`` instances.  This
module holds only the contracts; the services live in
``plan_conditioned_need_planner``, ``need_draft_grounder``, and
``need_validator``.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.benchmark import AuthorPlanningContext
from novel_agent.domain.ids import ArtifactId, RunId, StableId
from novel_agent.domain.memory import Stage1MemoryNeed
from novel_agent.domain.world import StoryTime

PLANNER_OUTPUT_SCHEMA_VERSION = "planned_need_draft.v1"


class GroundingStatus(StrEnum):
    GROUNDED = "grounded"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


class PlannerFallbackStatus(StrEnum):
    PLANNER = "planner"
    PLANNER_FALLBACK = "planner_fallback"


class PlannerInvocationAttemptStatus(StrEnum):
    SUCCEEDED = "succeeded"
    EMPTY_DRAFTS = "empty_drafts"
    ERROR = "error"


class EntityMention(DomainModel):
    """One natural-language entity reference in a draft (no graph id)."""

    label: str = Field(min_length=1)
    role_in_need: str = Field(default="", min_length=0)


class RelationMention(DomainModel):
    """One natural-language relation reference in a draft (no graph id)."""

    subject_label: str = Field(min_length=1)
    relation_label: str = Field(min_length=1)
    object_label: str = Field(min_length=1)


class PlannedNeedDraft(DomainModel):
    """LLM Planner output: semantic question plus mention-level anchors.

    Contains no graph ids.  ``draft_id`` is a short, non-empty identifier the
    LLM assigns; the validator sanitizes it into the final need id.
    """

    draft_id: str = Field(min_length=1, max_length=128)
    semantic_question: str = Field(min_length=1)
    entity_mentions: tuple[EntityMention, ...] = ()
    relation_mentions: tuple[RelationMention, ...] = ()
    trigger_plan_chapters: tuple[int, ...] = ()
    trigger_plan_goal: str = ""
    why_needed: str = ""
    required_claim_scopes: tuple[str, ...] = ()
    suggested_facets: tuple[str, ...] = ()
    historical_time_scope: str = ""
    query_hints: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_draft(self) -> PlannedNeedDraft:
        if any(chapter < 1 for chapter in self.trigger_plan_chapters):
            raise ValueError("trigger plan chapters must be positive")
        if len(self.trigger_plan_chapters) != len(set(self.trigger_plan_chapters)):
            raise ValueError("trigger plan chapters must be unique")
        if len({mention.label for mention in self.entity_mentions}) != len(self.entity_mentions):
            raise ValueError("entity mention labels must be unique")
        return self


class GroundedEntityMention(DomainModel):
    """Grounding result for one entity mention."""

    mention: str = Field(min_length=1)
    canonical_label: str
    entity_id: StableId | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    grounding_method: str = ""
    grounding_status: GroundingStatus


class GroundedRelationMention(DomainModel):
    """Grounding result for one relation mention."""

    subject_label: str = Field(min_length=1)
    relation_label: str = Field(min_length=1)
    object_label: str = Field(min_length=1)
    relation_id: StableId | None = None
    grounding_status: GroundingStatus
    confidence: float = Field(ge=0.0, le=1.0)
    grounding_method: str = ""


class GroundedNeedDraft(DomainModel):
    """Grounder output: the semantic draft with canonical world ids."""

    draft_id: str = Field(min_length=1)
    semantic_question: str = Field(min_length=1)
    entity_mentions: tuple[GroundedEntityMention, ...]
    relation_mentions: tuple[GroundedRelationMention, ...] = ()
    trigger_plan_chapters: tuple[int, ...] = ()
    trigger_plan_goal: str = ""
    why_needed: str = ""
    required_claim_scopes: tuple[str, ...] = ()
    suggested_facets: tuple[str, ...] = ()
    historical_time_scope: str = ""
    query_hints: tuple[str, ...] = ()


class PlannerEntitySummary(DomainModel):
    """Bounded entity row in the deterministic world summary."""

    label: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    entity_type: str = ""


class PlannerObligationSummary(DomainModel):
    """Bounded open-obligation row in the deterministic world summary."""

    description: str = Field(min_length=1)
    owner_labels: tuple[str, ...] = ()
    status: str = ""


class PlannerEventSummary(DomainModel):
    """Bounded recent-event row in the deterministic world summary."""

    event_type: str = Field(min_length=1)
    participant_labels: tuple[str, ...] = ()
    chapter: int | None = Field(default=None, ge=1)


class PlannerRelationSummary(DomainModel):
    """Bounded relation row in the deterministic world summary."""

    subject_label: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object_label: str = Field(min_length=1)


class PlannerStateSummary(DomainModel):
    """Cutoff-safe state surface needed for plan-to-history backward chaining."""

    subject_label: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    value: str = Field(min_length=1)


class PlannerWorldSummary(DomainModel):
    """Deterministic, bounded world projection shown to the LLM Planner.

    All collections are capped and every string is length-bounded so the
    prompt stays within the serialized request budget; truncation is explicit
    in the field names.
    """

    checkpoint_chapter: int = Field(ge=0)
    target_range: tuple[int, int]
    task_intent: str = ""
    plan_intent: str = ""
    entities: tuple[PlannerEntitySummary, ...] = ()
    states: tuple[PlannerStateSummary, ...] = ()
    open_obligations: tuple[PlannerObligationSummary, ...] = ()
    recent_events: tuple[PlannerEventSummary, ...] = ()
    key_relations: tuple[PlannerRelationSummary, ...] = ()
    entity_count: int = Field(ge=0)
    state_count: int = Field(ge=0)
    event_count: int = Field(ge=0)
    relation_count: int = Field(ge=0)
    obligation_count: int = Field(ge=0)
    truncated_entity_count: int = Field(default=0, ge=0)
    truncated_state_count: int = Field(default=0, ge=0)
    truncated_event_count: int = Field(default=0, ge=0)
    truncated_relation_count: int = Field(default=0, ge=0)
    truncated_obligation_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_summary(self) -> PlannerWorldSummary:
        start, end = self.target_range
        if start < 1 or end < start:
            raise ValueError("planner world summary target range is invalid")
        if self.entity_count - len(self.entities) != self.truncated_entity_count:
            raise ValueError("planner entity truncation count is inconsistent")
        if self.state_count - len(self.states) != self.truncated_state_count:
            raise ValueError("planner state truncation count is inconsistent")
        if self.relation_count - len(self.key_relations) != self.truncated_relation_count:
            raise ValueError("planner relation truncation count is inconsistent")
        return self


class PlannerArtifactMetadata(DomainModel):
    """Run-level lineage for one Planner invocation.

    The full model/prompt/world/raw-response fingerprint lives here once per
    run; every emitted ``Stage1MemoryNeed`` references it by
    ``planner_artifact_ref`` plus its own ``planned_draft_id`` and
    ``validated_need_set_hash``.
    """

    run_id: RunId
    planner_model: str = Field(min_length=1)
    planner_model_revision: str = Field(min_length=1)
    planner_prompt_version: str = Field(min_length=1)
    planner_prompt_hash: ArtifactId
    planner_output_schema_version: str = Field(min_length=1)
    temperature: float = Field(ge=0.0, le=2.0)
    requested_seed: int | None = Field(default=None, ge=0)
    effective_seed_supported: bool
    planning_context_hash: ArtifactId
    world_summary_hash: ArtifactId
    raw_response_hash: ArtifactId
    validated_need_set_hash: ArtifactId
    fallback_used: bool
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_lineage(self) -> PlannerArtifactMetadata:
        if self.raw_response_hash.root == "sha256:" + "0" * 64:
            raise ValueError("planner lineage requires a real raw response hash")
        return self


class PlannerInvocationAttempt(DomainModel):
    """One provider call, reconciled one-to-one with the Gateway ledger."""

    request_id: StableId
    status: PlannerInvocationAttemptStatus
    raw_response: str = ""
    raw_response_hash: ArtifactId
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    error_category: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_status(self) -> PlannerInvocationAttempt:
        if self.status is PlannerInvocationAttemptStatus.ERROR and self.error_category is None:
            raise ValueError("failed Planner attempt requires an error category")
        if (
            self.status is not PlannerInvocationAttemptStatus.ERROR
            and self.error_category is not None
        ):
            raise ValueError("successful Planner attempt cannot carry an error category")
        return self


class PlannerFinalNeedManifest(DomainModel):
    """Stable identity of one emitted Need without circular artifact references."""

    need_id: StableId
    source_draft_id: str = Field(min_length=1)
    need_payload_hash: ArtifactId
    completion_contract_hash: ArtifactId
    query_bundle_hash: ArtifactId


class PlannerRunResult(DomainModel):
    """One planner invocation outcome, including the fallback signal."""

    drafts: tuple[PlannedNeedDraft, ...]
    metadata: PlannerArtifactMetadata | None = None
    fallback_status: PlannerFallbackStatus
    error_category: str | None = Field(default=None, min_length=1)
    planning_context: AuthorPlanningContext
    world_summary: PlannerWorldSummary
    exact_prompt: str
    raw_response: str = ""
    attempts: tuple[PlannerInvocationAttempt, ...] = ()


class PlannerInvocationArtifact(DomainModel):
    """Dereferenceable, replay-complete record of one Planner invocation."""

    artifact_version: str = "planner_invocation_artifact.v2"
    planning_context: AuthorPlanningContext
    world_summary: PlannerWorldSummary
    exact_prompt: str
    metadata: PlannerArtifactMetadata | None = None
    raw_response: str = ""
    attempts: tuple[PlannerInvocationAttempt, ...] = ()
    parsed_drafts: tuple[PlannedNeedDraft, ...] = ()
    grounded_drafts: tuple[GroundedNeedDraft, ...] = ()
    accepted_draft_ids: tuple[str, ...] = ()
    rejected_reasons: dict[str, str] = Field(default_factory=dict)
    deduplicated_draft_ids: tuple[str, ...] = ()
    truncated_draft_ids: tuple[str, ...] = ()
    final_need_manifests: tuple[PlannerFinalNeedManifest, ...] = ()
    validated_need_set_hash: ArtifactId
    fallback_status: PlannerFallbackStatus
    fallback_reason: str | None = None
    artifact_ref: ArtifactRef | None = None

    @model_validator(mode="after")
    def validate_basis(self) -> PlannerInvocationArtifact:
        if self.metadata is not None:
            if self.metadata.planning_context_hash != self.planning_context.source_hash:
                raise ValueError("Planner artifact planning context hash mismatch")
            if self.metadata.validated_need_set_hash != self.validated_need_set_hash:
                raise ValueError("Planner artifact validated set hash mismatch")
        if self.fallback_status is PlannerFallbackStatus.PLANNER_FALLBACK:
            if self.fallback_reason is None:
                raise ValueError("Planner fallback artifact requires a reason")
        elif self.fallback_reason is not None:
            raise ValueError("successful Planner artifact cannot carry fallback reason")
        request_ids = tuple(attempt.request_id for attempt in self.attempts)
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("Planner artifact attempt request ids must be unique")
        need_ids = tuple(item.need_id for item in self.final_need_manifests)
        if len(need_ids) != len(set(need_ids)):
            raise ValueError("Planner artifact final Need ids must be unique")
        return self


class RetrievalQueryBundle(DomainModel):
    """Per-channel compiled queries for one Stage1MemoryNeed.

    Compiled deterministically by ``NeedQueryCompiler``; the executed channel
    set is the intersection of ``ROUTES[query_intent]`` and the queries
    available in this bundle.
    """

    semantic_query: str = Field(min_length=1)
    lexical_queries: tuple[str, ...] = Field(min_length=1)
    exact_entity_ids: tuple[StableId, ...] = ()
    exact_predicates: tuple[str, ...] = ()
    graph_seeds: tuple[StableId, ...] = ()
    graph_relations: tuple[str, ...] = ()
    time_scope: StoryTime | None = None
    excluded_information_labels: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_bundle(self) -> RetrievalQueryBundle:
        if len(self.lexical_queries) != len(set(self.lexical_queries)):
            raise ValueError("lexical queries must be unique")
        if len(self.exact_entity_ids) != len(set(self.exact_entity_ids)):
            raise ValueError("exact entity ids must be unique")
        if len(self.exact_predicates) != len(set(self.exact_predicates)):
            raise ValueError("exact predicates must be unique")
        return self


class PlannerNeedGenerationResult(DomainModel):
    """Reviewed-inquiry lineage for the Planner-specific Need set."""

    inquiry_ref: ArtifactRef
    inquiry_review_ref: ArtifactRef
    needs: tuple[Stage1MemoryNeed, ...]
    query_bundles: dict[str, RetrievalQueryBundle]
    rejected_question_ids: tuple[StableId, ...] = ()
    rejection_reasons: dict[str, str] = Field(default_factory=dict)
    validated_need_set_hash: ArtifactId
    generator_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_query_bundles(self) -> PlannerNeedGenerationResult:
        if set(self.query_bundles) != {need.need_id.root for need in self.needs}:
            raise ValueError("Planner Need query bundles must cover the final Need set")
        if len({need.need_id for need in self.needs}) != len(self.needs):
            raise ValueError("Planner Need identities must be unique")
        return self


__all__ = [
    "PLANNER_OUTPUT_SCHEMA_VERSION",
    "EntityMention",
    "GroundedEntityMention",
    "GroundedNeedDraft",
    "GroundedRelationMention",
    "GroundingStatus",
    "PlannedNeedDraft",
    "PlannerArtifactMetadata",
    "PlannerEntitySummary",
    "PlannerEventSummary",
    "PlannerFallbackStatus",
    "PlannerFinalNeedManifest",
    "PlannerInvocationArtifact",
    "PlannerInvocationAttempt",
    "PlannerInvocationAttemptStatus",
    "PlannerNeedGenerationResult",
    "PlannerObligationSummary",
    "PlannerRelationSummary",
    "PlannerRunResult",
    "PlannerStateSummary",
    "PlannerWorldSummary",
    "RelationMention",
    "RetrievalQueryBundle",
]
