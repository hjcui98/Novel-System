"""Stage 1 canonical memory, retrieval, fusion, and context contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, JsonValue, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.text import EvidenceRef
from novel_agent.domain.world import (
    Entity,
    Event,
    RelationRecord,
    StateRecord,
    StoryTime,
    TruthClass,
)
from novel_agent.domain.writer_context import WriterContextSection


class ObligationKind(StrEnum):
    FORESHADOWING = "foreshadowing"
    PROMISE = "promise"
    OBJECTIVE = "objective"
    UNRESOLVED_CONFLICT = "unresolved_conflict"


class ObligationStatus(StrEnum):
    OPEN = "open"
    PROGRESSED = "progressed"
    RESOLVED = "resolved"
    ABANDONED = "abandoned"


class PlanObligation(DomainModel):
    obligation_id: StableId
    kind: ObligationKind
    description: str = Field(min_length=1)
    status: ObligationStatus
    owner_ids: tuple[StableId, ...] = ()
    due_chapter: int | None = Field(default=None, ge=1)
    evidence_refs: tuple[EvidenceRef, ...] = ()


class WorldRootDocument(DomainModel):
    root_hash: ArtifactId
    schema_version: SchemaVersion
    source_commit: CommitId
    entities: tuple[Entity, ...] = ()
    events: tuple[Event, ...] = ()
    states: tuple[StateRecord, ...] = ()
    relations: tuple[RelationRecord, ...] = ()
    obligations: tuple[PlanObligation, ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> WorldRootDocument:
        entity_ids = {entity.entity_id for entity in self.entities}
        if len(entity_ids) != len(self.entities):
            raise ValueError("world entity ids must be unique")
        record_ids = [
            *(event.event_id for event in self.events),
            *(state.state_id for state in self.states),
            *(relation.relation_id for relation in self.relations),
            *(obligation.obligation_id for obligation in self.obligations),
        ]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("world record ids must be unique")
        referenced = {
            *(participant for event in self.events for participant in event.participant_ids),
            *(state.subject_id for state in self.states),
            *(relation.subject_id for relation in self.relations),
            *(relation.object_id for relation in self.relations),
            *(owner for obligation in self.obligations for owner in obligation.owner_ids),
        }
        if not referenced.issubset(entity_ids):
            raise ValueError("world record references an unknown entity")
        return self


class R1RecordView(DomainModel):
    row_id: StableId
    source_commit: CommitId
    record_kind: str = Field(min_length=1)
    record_id: StableId
    predicate: str | None = None
    valid_start: int | None = None
    valid_end: int | None = None
    worldline: str | None = None
    narrative_start: int | None = None
    narrative_end: int | None = None
    access_scope: str = Field(default="writer_safe", min_length=1)
    truth_class: str | None = None
    entity_ids: tuple[StableId, ...] = ()
    record: dict[str, JsonValue]


class GraphPathDereferenceStatus(StrEnum):
    RELATION_ROWS_VERIFIED = "relation_rows_verified"
    L0_VERIFIED = "l0_verified"


class GraphPathReceipt(DomainModel):
    path_id: StableId
    source_commit: CommitId
    snapshot_id: StableId
    seed_entity_ids: tuple[StableId, ...] = Field(min_length=1)
    relation_row_ids: tuple[StableId, ...] = Field(min_length=1)
    relation_ids: tuple[StableId, ...] = Field(min_length=1)
    entity_path: tuple[StableId, ...] = Field(min_length=2)
    predicates: tuple[str, ...] = Field(min_length=1)
    directions: tuple[str, ...] = Field(min_length=1)
    valid_time: tuple[StoryTime, ...] = Field(min_length=1)
    edge_semantics: tuple[str, ...] = Field(min_length=1)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    dereference_status: GraphPathDereferenceStatus

    @model_validator(mode="after")
    def validate_path_shape(self) -> GraphPathReceipt:
        edge_count = len(self.relation_ids)
        if not all(
            len(items) == edge_count
            for items in (
                self.relation_row_ids,
                self.predicates,
                self.directions,
                self.valid_time,
                self.edge_semantics,
            )
        ):
            raise ValueError("graph path edge metadata lengths must match")
        if len(self.entity_path) != edge_count + 1:
            raise ValueError("graph path entity count must be edge count plus one")
        if self.entity_path[0] not in self.seed_entity_ids:
            raise ValueError("graph path must start at one of its declared seeds")
        if any(direction not in {"forward", "reverse"} for direction in self.directions):
            raise ValueError("graph path direction must be forward or reverse")
        if any(semantic != "canonical" for semantic in self.edge_semantics):
            raise ValueError("graph path receipt only permits canonical edge semantics")
        return self


class Stage1QueryIntent(StrEnum):
    CURRENT_STATE = "current_state"
    KNOWN_ID = "known_id"
    PLAN_NODE = "plan_node"
    MANDATORY_CONSTRAINT = "mandatory_constraint"
    SEMANTIC_HISTORY = "semantic_history"
    RELATED_EVENT = "related_event"
    PLAN_OBLIGATION = "plan_obligation"
    GLOBAL_ARC = "global_arc"
    CHAPTER_THREAD = "chapter_thread"
    CHARACTER_ARC = "character_arc"
    EXACT_QUOTE = "exact_quote"
    RARE_PHRASE = "rare_phrase"
    STYLE_VOICE = "style_voice"
    DIALOGUE_SAMPLE = "dialogue_sample"
    CAUSAL_MULTI_HOP = "causal_multi_hop"
    RELATION_CHAIN = "relation_chain"
    ANCHOR_INSUFFICIENT = "anchor_insufficient"


class NeedRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RequirementLevel(StrEnum):
    MANDATORY = "mandatory"
    OPTIONAL = "optional"


class ResolutionPath(StrEnum):
    EXACT_TEMPORAL = "exact_temporal"
    ANCHOR_FIRST = "anchor_first"
    HIERARCHY = "hierarchy"
    GROUNDED_DIRECT = "grounded_direct"
    TYPED_GRAPH = "typed_graph"


class CandidatePool(StrEnum):
    R0 = "r0"
    R1 = "r1"
    ANCHOR = "anchor"
    GROUNDED = "grounded"
    HIERARCHY = "hierarchy"
    GRAPH = "graph"


class NeedFacetKind(StrEnum):
    CURRENT_STATE = "current_state"
    RELATION_STATE = "relation_state"
    CAPABILITY_STATUS = "capability_status"
    LIMITATION = "limitation"
    KNOWLEDGE_BOUNDARY = "knowledge_boundary"
    CAUSAL_HISTORY = "causal_history"
    SETUP = "setup"
    UNRESOLVED_STATUS = "unresolved_status"
    COMMITMENT = "commitment"
    PLAN_NODE = "plan_node"


class ExpectedClaimScope(StrEnum):
    CURRENT = "current"
    HISTORICAL = "historical"
    KNOWLEDGE = "knowledge"
    PLANNED = "planned"


class FacetEvidenceRequirement(StrEnum):
    TRACEABLE_CUTOFF_SOURCE = "traceable_cutoff_source"
    CUTOFF_CURRENT_SOURCE = "cutoff_current_source"
    PLAN_PROVENANCE = "plan_provenance"
    DISTINCT_HISTORICAL_SOURCE = "distinct_historical_source"


class NeedUncertaintyPolicy(StrEnum):
    ALLOW_GAP_ONLY = "allow_gap_only"
    REJECT_UNVERIFIED_CLAIM = "reject_unverified_claim"


class NeedGapPolicy(StrEnum):
    EMIT_TYPED_GAP = "emit_typed_gap"
    FAIL_MANDATORY = "fail_mandatory"


class NeedFacet(DomainModel):
    need_facet_id: StableId
    need_id: StableId
    facet_kind: NeedFacetKind
    expected_claim_scope: ExpectedClaimScope
    derivation_refs: tuple[StableId, ...]
    producer: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)
    information_scope: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_derivation(self) -> NeedFacet:
        if not self.derivation_refs:
            raise ValueError("NeedFacet requires public derivation refs")
        if len(self.derivation_refs) != len(set(self.derivation_refs)):
            raise ValueError("NeedFacet derivation refs must be unique")
        return self


class NeedCompletionSpec(DomainModel):
    need_id: StableId
    required_need_facet_ids: tuple[StableId, ...]
    irreducible_need_facet_ids: tuple[StableId, ...]
    evidence_requirement_by_facet: dict[str, FacetEvidenceRequirement]
    min_distinct_evidence_sources: int = Field(default=1, ge=1)
    min_distinct_chapters: int = Field(default=1, ge=1)
    require_current_claim: bool = False
    require_causal_history: bool = False
    uncertainty_policy: NeedUncertaintyPolicy
    gap_policy: NeedGapPolicy
    producer: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)
    # Facet-level predicate binding (2026-08-14 review second follow-up P1):
    # for each required facet id, the exact predicates that can serve that
    # facet.  Distinct from Stage1MemoryNeed.predicates, which remains the
    # Need-wide OR-set used by R1 exact retrieval.  A facet without a binding
    # cannot be closed (fail-closed); a unit predicate closes only the facets
    # whose binding contains it.
    predicates_by_facet: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_facets(self) -> NeedCompletionSpec:
        required = set(self.required_need_facet_ids)
        irreducible = set(self.irreducible_need_facet_ids)
        if not required:
            raise ValueError("NeedCompletionSpec requires at least one facet")
        if len(required) != len(self.required_need_facet_ids):
            raise ValueError("required NeedFacet ids must be unique")
        if not irreducible.issubset(required):
            raise ValueError("irreducible NeedFacet ids must be required")
        if set(self.evidence_requirement_by_facet) != {
            item.root for item in self.required_need_facet_ids
        }:
            raise ValueError("evidence requirements must cover every required NeedFacet")
        if self.predicates_by_facet and set(self.predicates_by_facet) != {
            item.root for item in self.required_need_facet_ids
        }:
            raise ValueError("facet predicate bindings must cover every required NeedFacet")
        return self


class Stage1MemoryNeed(DomainModel):
    need_id: StableId
    run_id: RunId
    task_id: TaskId
    base_commit: CommitId
    chapter_target: int | None = Field(default=None, ge=1)
    horizon_target: tuple[int, int] | None = None
    need_type: str = Field(min_length=1)
    query_intent: Stage1QueryIntent
    query_text: str = Field(min_length=1)
    entity_ids: tuple[StableId, ...] = ()
    predicates: tuple[str, ...] = ()
    time_scope: StoryTime | None = None
    access_scope: str = Field(default="writer_safe", min_length=1)
    # Deprecated single-flag alias of the layered plan policies below.  Kept
    # only for the transition; it must equal retrieval_may_return_plan and is
    # removed after every call site migrates to the layered policies.
    allow_plan: bool = False
    planner_may_read_plan: bool = False
    retrieval_may_return_plan: bool = False
    claim_may_cite_plan: bool = False
    legacy_allow_plan: bool = False
    hierarchy_parent_unit_ids: tuple[StableId, ...] = ()
    why_needed: str = Field(min_length=1)
    risk_level: NeedRisk
    requirement: RequirementLevel
    preferred_resolution_path: ResolutionPath
    allowed_candidate_pools: tuple[CandidatePool, ...] = Field(min_length=1)
    expected_evidence_types: tuple[str, ...] = ()
    stop_condition: str = Field(min_length=1)
    purpose: str | None = Field(default=None, min_length=1)
    expected_section: WriterContextSection | None = None
    focus_ids: tuple[StableId, ...] = ()
    priority: int = Field(default=50, ge=0, le=100)
    query_hints: tuple[str, ...] = ()
    completion_criteria: str | None = Field(default=None, min_length=1)
    need_facets: tuple[NeedFacet, ...] = ()
    completion_spec: NeedCompletionSpec | None = None
    # LLM Planner lineage (Phase 1).  Present together on planner-produced
    # needs; template needs keep them empty.
    semantic_question: str = ""
    trigger_plan_chapters: tuple[int, ...] = ()
    trigger_plan_goal: str = ""
    # Host-verified canonical goal text per trigger chapter (ADR-0008 public
    # binding).  The model's ``trigger_plan_goal`` is only an auditable
    # explanation and is never used as the binding identity again.
    canonical_goal_by_chapter: dict[int, str] = Field(default_factory=dict)
    planner_artifact_ref: ArtifactId | None = None
    planned_draft_id: str | None = Field(default=None, min_length=1)
    validated_need_set_hash: ArtifactId | None = None

    @model_validator(mode="after")
    def validate_target(self) -> Stage1MemoryNeed:
        if self.chapter_target is None and self.horizon_target is None:
            raise ValueError("memory need requires a chapter or horizon target")
        if self.horizon_target is not None:
            start, end = self.horizon_target
            if start < 1 or end < start:
                raise ValueError("memory need horizon is invalid")
        if len(self.hierarchy_parent_unit_ids) != len(set(self.hierarchy_parent_unit_ids)):
            raise ValueError("memory need hierarchy parents must be unique")
        if len(self.focus_ids) != len(set(self.focus_ids)):
            raise ValueError("memory need focus ids must be unique")
        if self.legacy_allow_plan != self.retrieval_may_return_plan:
            raise ValueError("legacy allow_plan must equal the retrieval plan policy")
        if self.allow_plan != self.retrieval_may_return_plan:
            raise ValueError("deprecated allow_plan must equal retrieval_may_return_plan")
        if self.retrieval_may_return_plan and not self.planner_may_read_plan:
            raise ValueError("retrieval plan access requires planner plan access")
        planner_refs = (
            self.planner_artifact_ref,
            self.planned_draft_id,
            self.validated_need_set_hash,
        )
        lineage_complete = bool(self.semantic_question) and all(
            item is not None for item in planner_refs
        )
        if (self.semantic_question or any(item is not None for item in planner_refs)) and not (
            lineage_complete
        ):
            raise ValueError("planner-derived need requires complete planner lineage")
        if (
            lineage_complete
            and self.trigger_plan_chapters
            and not (self.trigger_plan_goal or self.canonical_goal_by_chapter)
        ):
            raise ValueError(
                "planner-derived trigger chapters require the canonical goal text or binding"
            )
        if self.canonical_goal_by_chapter and (
            set(self.canonical_goal_by_chapter) != set(self.trigger_plan_chapters)
            or any(not goal.strip() for goal in self.canonical_goal_by_chapter.values())
        ):
            raise ValueError("canonical goal binding must cover exactly the trigger chapters")
        if bool(self.need_facets) != (self.completion_spec is not None):
            raise ValueError("NeedFacet and NeedCompletionSpec must appear together")
        if self.completion_spec is not None:
            if self.completion_spec.need_id != self.need_id:
                raise ValueError("NeedCompletionSpec need id mismatch")
            facet_ids = {item.need_facet_id for item in self.need_facets}
            if len(facet_ids) != len(self.need_facets):
                raise ValueError("memory need facet ids must be unique")
            if any(item.need_id != self.need_id for item in self.need_facets):
                raise ValueError("NeedFacet need id mismatch")
            if not set(self.completion_spec.required_need_facet_ids).issubset(facet_ids):
                raise ValueError("NeedCompletionSpec references an unknown NeedFacet")
            if not self.claim_may_cite_plan and any(
                item.information_scope == "author_plan" for item in self.need_facets
            ):
                raise ValueError("plan-derived NeedFacet requires claim_may_cite_plan")
        return self


class HorizonNeedSet(DomainModel):
    horizon_start: int = Field(ge=1)
    horizon_end: int = Field(ge=1)
    shared_constraints: tuple[Stage1MemoryNeed, ...] = ()
    chapter_needs: tuple[Stage1MemoryNeed, ...] = ()
    progressive_needs: tuple[Stage1MemoryNeed, ...] = ()
    volume_obligations: tuple[Stage1MemoryNeed, ...] = ()

    @model_validator(mode="after")
    def validate_horizon(self) -> HorizonNeedSet:
        if self.horizon_end < self.horizon_start:
            raise ValueError("horizon end must not precede start")
        return self


class RetrievalUnitKind(StrEnum):
    FACT_ANCHOR = "fact_anchor"
    STATE_ANCHOR = "state_anchor"
    RELATION_ANCHOR = "relation_anchor"
    EVENT_ANCHOR = "event_anchor"
    SCENE_ANCHOR = "scene_anchor"
    CHAPTER_ANCHOR = "chapter_anchor"
    ARC_ANCHOR = "arc_anchor"
    PLAN_ANCHOR = "plan_anchor"
    GROUNDED_BLOCK = "grounded_block"
    GROUNDED_SPAN = "grounded_span"


class RetrievalChannel(StrEnum):
    R0 = "r0"
    R1_EXACT = "r1_exact"
    R1_TEMPORAL = "r1_temporal"
    ANCHOR_BM25 = "anchor_bm25"
    ANCHOR_DENSE = "anchor_dense"
    GROUNDED_BM25 = "grounded_bm25"
    GROUNDED_DENSE = "grounded_dense"
    HIERARCHY = "hierarchy"
    TYPED_GRAPH = "typed_graph"
    RERANK = "rerank"


class RetrievalUnit(DomainModel):
    unit_id: StableId
    unit_kind: RetrievalUnitKind
    source_commit: CommitId
    snapshot_id: StableId
    source_artifact: ArtifactId | None = None
    source_refs: tuple[ArtifactId, ...] = ()
    content_hash: ArtifactId | None = None
    text: str = Field(min_length=1)
    entity_ids: tuple[StableId, ...] = ()
    predicate: str | None = None
    canonical_value_id: StableId | None = None
    canonicalizer_version: str | None = Field(default=None, min_length=1)
    canonical_alias_receipt_ref: ArtifactRef | None = None
    parent_unit_id: StableId | None = None
    parent_unit_ids: tuple[StableId, ...] = ()
    worldline: str = Field(default="main", min_length=1)
    narrative_start: int | None = Field(default=None, ge=0)
    narrative_end: int | None = Field(default=None, ge=0)
    story_time_start: int | None = None
    story_time_end: int | None = None
    truth_class: TruthClass | None = None
    support_status: str | None = None
    access_scope: str = Field(default="writer_safe", min_length=1)
    information_label: str = Field(default="observed", min_length=1)
    derivation_taint: tuple[str, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    mandatory: bool = False

    @model_validator(mode="after")
    def validate_projection_metadata(self) -> RetrievalUnit:
        if (self.canonical_value_id is None) != (self.canonicalizer_version is None):
            raise ValueError("canonical value id and canonicalizer version must appear together")
        if self.canonical_alias_receipt_ref is not None and self.canonical_value_id is None:
            raise ValueError("canonical alias receipt requires canonical value metadata")
        if (
            self.narrative_start is not None
            and self.narrative_end is not None
            and self.narrative_end < self.narrative_start
        ):
            raise ValueError("retrieval unit narrative end precedes narrative start")
        if (
            self.story_time_start is not None
            and self.story_time_end is not None
            and self.story_time_end < self.story_time_start
        ):
            raise ValueError("retrieval unit story time end precedes story time start")
        if len(self.parent_unit_ids) != len(set(self.parent_unit_ids)):
            raise ValueError("retrieval unit parent ids must be unique")
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("retrieval unit source refs must be unique")
        return self


class ChannelHit(DomainModel):
    unit: RetrievalUnit
    channel: RetrievalChannel
    channel_rank: int = Field(ge=1)
    raw_score: float
    candidate_count: int = Field(ge=1)
    hit_reason: str = Field(min_length=1)
    graph_path_receipts: tuple[GraphPathReceipt, ...] = ()


class FusedCandidate(DomainModel):
    unit: RetrievalUnit
    fused_rank: int = Field(ge=1)
    rrf_score: float = Field(gt=0)
    channel_hits: tuple[ChannelHit, ...] = Field(min_length=1)
    selected: bool = True
    rejection_reason: str | None = None


class RetrievalStopReason(StrEnum):
    EXACT_SATISFIED = "exact_satisfied"
    BUDGET_SATISFIED = "budget_satisfied"
    CANDIDATES_EXHAUSTED = "candidates_exhausted"
    FALLBACK_EXHAUSTED = "fallback_exhausted"
    NO_EXECUTABLE_QUERY = "no_executable_query"


class FacetClosureStatus(StrEnum):
    """Per-facet evidence closure state shared by route, support and package."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    EXHAUSTED = "exhausted"
    NOT_EXECUTED = "not_executed"


class FacetEvidenceReceipt(DomainModel):
    """One required NeedFacet's closure against exact L0 evidence.

    Computed once from the selected retrieval candidates and reused by route
    stop reasons, support diagnostics and the package gap report, so a route
    can never claim ``exact_satisfied`` for a facet the package marks as a gap.
    """

    need_id: StableId
    need_facet_id: StableId
    facet_kind: NeedFacetKind
    mandatory: bool
    status: FacetClosureStatus
    supporting_unit_ids: tuple[StableId, ...] = ()
    stop_reason: str = ""


class NeedExecutionStatus(StrEnum):
    """Whether a legal Need actually received retrieval budget."""

    EXECUTED_WITH_CANDIDATES = "executed_with_candidates"
    EXECUTED_EMPTY = "executed_empty"
    NOT_EXECUTED_BUDGET_EXHAUSTED = "not_executed_budget_exhausted"
    NOT_EXECUTED_FRESHNESS_BLOCKED = "not_executed_freshness_blocked"
    NOT_EXECUTED_SCOPE_BLOCKED = "not_executed_scope_blocked"
    NOT_EXECUTED_NO_EXECUTABLE_QUERY = "not_executed_no_executable_query"
    NOT_EXECUTED_CONTROLLER_STOP = "not_executed_controller_stop"


class RetrievalTrace(DomainModel):
    need_id: StableId
    intent: Stage1QueryIntent
    allowed_channels: tuple[RetrievalChannel, ...]
    channel_candidate_counts: dict[RetrievalChannel, int]
    candidates: tuple[FusedCandidate, ...]
    fusion_applied: bool
    rerank_applied: bool = False
    channel_failures: dict[RetrievalChannel, str] = Field(default_factory=dict)
    fallback_used: bool = False
    fallback_reason: str | None = None
    stop_reason: RetrievalStopReason
    need_execution_status: NeedExecutionStatus = NeedExecutionStatus.EXECUTED_EMPTY
    calls_allocated: int = Field(default=0, ge=0)
    required_need_facet_ids: tuple[StableId, ...] = ()
    irreducible_need_facet_ids: tuple[StableId, ...] = ()
    closed_need_facet_ids: tuple[StableId, ...] = ()
    # Facet-driven retrieval: one receipt per required facet, computed from
    # exact L0 evidence before any claim/support layer runs.
    facet_receipts: tuple[FacetEvidenceReceipt, ...] = ()
    retrieval_pages: int = Field(default=1, ge=1)
    anchors_expanded: int = Field(default=0, ge=0)
    spans_expanded: int = Field(default=0, ge=0)
    l0_tokens: int = Field(default=0, ge=0)
    scenes_expanded: int = Field(default=0, ge=0)
    # Direct-retrieval unit ids, distinct from corridor expansion units
    # (raw evidence spans / style-or-reference optional units).
    direct_unit_ids: tuple[StableId, ...] = ()
    compiled_query_bundle: dict[str, JsonValue] = Field(default_factory=dict)
    effective_channels: tuple[RetrievalChannel, ...] = ()
    query_unavailable_reasons: dict[RetrievalChannel, str] = Field(default_factory=dict)
    full_chapters_read: int = Field(default=0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_execution_diagnostics(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "need_execution_status" not in normalized:
            normalized["need_execution_status"] = (
                NeedExecutionStatus.EXECUTED_WITH_CANDIDATES
                if normalized.get("candidates")
                else NeedExecutionStatus.EXECUTED_EMPTY
            )
        if "calls_allocated" not in normalized:
            normalized["calls_allocated"] = len(normalized.get("allowed_channels", ()))
        return normalized

    @model_validator(mode="after")
    def validate_execution_diagnostics(self) -> RetrievalTrace:
        not_executed = self.need_execution_status.value.startswith("not_executed_")
        if not_executed and self.calls_allocated:
            raise ValueError("unexecuted Need cannot have allocated retrieval calls")
        if not_executed and self.candidates:
            raise ValueError("unexecuted Need cannot have retrieval candidates")
        if (
            self.need_execution_status is NeedExecutionStatus.EXECUTED_WITH_CANDIDATES
            and not self.candidates
        ):
            raise ValueError("candidate execution status requires candidates")
        if self.need_execution_status is NeedExecutionStatus.EXECUTED_EMPTY and self.candidates:
            raise ValueError("empty execution status cannot contain candidates")
        if not set(self.closed_need_facet_ids).issubset(self.required_need_facet_ids):
            raise ValueError("closed Need facets must be required facets")
        if self.facet_receipts and any(
            receipt.need_id != self.need_id
            or receipt.need_facet_id not in self.required_need_facet_ids
            for receipt in self.facet_receipts
        ):
            raise ValueError("facet receipts must belong to this Need's required facets")
        if self.facet_receipts and set(self.closed_need_facet_ids) != {
            receipt.need_facet_id
            for receipt in self.facet_receipts
            if receipt.status is FacetClosureStatus.SUPPORTED
        }:
            raise ValueError("closed Need facets must match supported facet receipts")
        return self


class ContextBudgetReport(DomainModel):
    token_budget: int = Field(ge=1)
    mandatory_tokens: int = Field(ge=0)
    optional_tokens: int = Field(ge=0)
    dropped_optional_unit_ids: tuple[StableId, ...] = ()
    full_chapter_read_count: int = Field(ge=0)


class Stage1ContextPackage(DomainModel):
    context_id: StableId
    base_commit: CommitId
    snapshot_id: StableId
    task_contract: str = Field(min_length=1)
    mandatory_constraints: tuple[RetrievalUnit, ...] = ()
    current_world_state: tuple[RetrievalUnit, ...] = ()
    active_plan_obligations: tuple[RetrievalUnit, ...] = ()
    relevant_historical_events: tuple[RetrievalUnit, ...] = ()
    truth_and_knowledge_boundaries: tuple[RetrievalUnit, ...] = ()
    raw_evidence_spans: tuple[RetrievalUnit, ...] = ()
    style_or_reference_optional: tuple[RetrievalUnit, ...] = ()
    unresolved_gaps: tuple[str, ...] = ()
    need_facets: tuple[NeedFacet, ...] = ()
    need_completion_specs: tuple[NeedCompletionSpec, ...] = ()
    retrieval_traces: tuple[RetrievalTrace, ...] = ()
    budget_report: ContextBudgetReport
    contract_version: str = "stage1_context.legacy"
    benchmark_quality_eligible: bool = False

    @model_validator(mode="after")
    def validate_need_contracts(self) -> Stage1ContextPackage:
        facet_need_ids = {item.need_id for item in self.need_facets}
        spec_need_ids = {item.need_id for item in self.need_completion_specs}
        if facet_need_ids != spec_need_ids:
            raise ValueError("Stage1 Context NeedFacet and completion specs must cover same Needs")
        return self


class DerivedBuildStatus(StrEnum):
    BUILDING = "building"
    EXACT = "exact"
    PARTIAL = "partial"
    FAILED = "failed"


class DerivedSnapshotLite(DomainModel):
    snapshot_id: StableId
    source_commit: CommitId
    anchor_build_id: StableId
    anchor_index_version: str = Field(min_length=1)
    grounded_index_version: str = Field(min_length=1)
    embedding_profile: str = Field(min_length=1)
    fusion_profile: str = Field(min_length=1)
    build_status: DerivedBuildStatus
    build_profile: str = Field(default="unspecified", min_length=1)
    retrieval_backend_profile: str = Field(default="unspecified", min_length=1)
    projection_attestation: dict[str, JsonValue] | None = None
    failure_debt: tuple[str, ...] = ()
    published_at: datetime | None = None


class FreshnessMode(StrEnum):
    WAIT_FOR_EXACT = "wait_for_exact"
    DEGRADED_CANONICAL = "degraded_canonical"
    BLOCK_ON_MISMATCH = "block_on_mismatch"
    MANUAL_OVERRIDE = "manual_override"


class FreshnessStatus(StrEnum):
    READY = "ready"
    WAITING = "waiting"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    OVERRIDDEN = "overridden"


class FreshnessRequest(DomainModel):
    canonical_commit: CommitId
    r1_basis_commit: CommitId
    required_snapshot_id: StableId
    actual_alias_commit: CommitId | None = None
    actual_snapshot: DerivedSnapshotLite | None = None
    mode: FreshnessMode
    manual_approval_id: StableId | None = None

    @model_validator(mode="after")
    def validate_override(self) -> FreshnessRequest:
        if self.mode is FreshnessMode.MANUAL_OVERRIDE and self.manual_approval_id is None:
            raise ValueError("manual freshness override requires an approval id")
        if self.mode is not FreshnessMode.MANUAL_OVERRIDE and self.manual_approval_id is not None:
            raise ValueError("approval id is only valid for manual freshness override")
        return self


class FreshnessDecision(DomainModel):
    status: FreshnessStatus
    canonical_commit: CommitId
    r1_basis_commit: CommitId
    required_snapshot_id: StableId
    actual_alias_commit: CommitId | None = None
    actual_snapshot_id: StableId | None = None
    actual_snapshot_commit: CommitId | None = None
    reason: str = Field(min_length=1)
    manual_approval_id: StableId | None = None
