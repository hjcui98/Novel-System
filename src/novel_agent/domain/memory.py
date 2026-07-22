"""Stage 1 canonical memory, retrieval, fusion, and context contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, JsonValue, model_validator

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.text import EvidenceRef
from novel_agent.domain.world import Entity, Event, RelationRecord, StateRecord, StoryTime


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
    truth_class: str | None = None
    entity_ids: tuple[StableId, ...] = ()
    record: dict[str, JsonValue]


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
    why_needed: str = Field(min_length=1)
    risk_level: NeedRisk
    requirement: RequirementLevel
    preferred_resolution_path: ResolutionPath
    allowed_candidate_pools: tuple[CandidatePool, ...] = Field(min_length=1)
    expected_evidence_types: tuple[str, ...] = ()
    stop_condition: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_target(self) -> Stage1MemoryNeed:
        if self.chapter_target is None and self.horizon_target is None:
            raise ValueError("memory need requires a chapter or horizon target")
        if self.horizon_target is not None:
            start, end = self.horizon_target
            if start < 1 or end < start:
                raise ValueError("memory need horizon is invalid")
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
    text: str = Field(min_length=1)
    entity_ids: tuple[StableId, ...] = ()
    parent_unit_id: StableId | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    mandatory: bool = False


class ChannelHit(DomainModel):
    unit: RetrievalUnit
    channel: RetrievalChannel
    channel_rank: int = Field(ge=1)
    raw_score: float
    candidate_count: int = Field(ge=1)
    hit_reason: str = Field(min_length=1)


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


class RetrievalTrace(DomainModel):
    need_id: StableId
    intent: Stage1QueryIntent
    allowed_channels: tuple[RetrievalChannel, ...]
    channel_candidate_counts: dict[RetrievalChannel, int]
    candidates: tuple[FusedCandidate, ...]
    fusion_applied: bool
    fallback_used: bool = False
    fallback_reason: str | None = None
    stop_reason: RetrievalStopReason
    anchors_expanded: int = Field(default=0, ge=0)
    spans_expanded: int = Field(default=0, ge=0)
    l0_tokens: int = Field(default=0, ge=0)
    scenes_expanded: int = Field(default=0, ge=0)
    full_chapters_read: int = Field(default=0, ge=0)


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
    retrieval_traces: tuple[RetrievalTrace, ...] = ()
    budget_report: ContextBudgetReport


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
