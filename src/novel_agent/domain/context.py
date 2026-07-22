"""Stage 0 read-side query, evidence, and context contracts."""

from enum import StrEnum

from pydantic import Field

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import CommitId, RunId, StableId, TaskId
from novel_agent.domain.text import EvidenceRef


class QueryIntent(StrEnum):
    EXACT_STATE = "exact_state"
    TEMPORAL = "temporal"
    SEMANTIC_HISTORY = "semantic_history"
    GLOBAL_ARC = "global_arc"
    PLAN_OBLIGATION = "plan_obligation"
    EXACT_QUOTE = "exact_quote"
    STYLE_SAMPLE = "style_sample"


class QueryContract(DomainModel):
    query_id: StableId
    base_commit: CommitId
    intent: QueryIntent
    question: str = Field(min_length=1)
    worldline: str = Field(min_length=1)
    result_limit: int = Field(default=20, ge=1, le=1000)


class MemoryRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class MemoryUrgency(StrEnum):
    ADVISORY = "advisory"
    BLOCKING = "blocking"


class MemoryNeed(DomainModel):
    need_id: StableId
    requester_agent: str = Field(min_length=1)
    run_id: RunId
    task_id: TaskId
    base_commit: CommitId
    gap_type: str = Field(min_length=1)
    question: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    worldline: str = Field(min_length=1)
    known_context: tuple[str, ...] = ()
    requested_evidence_types: tuple[str, ...] = ()
    risk: MemoryRisk
    urgency: MemoryUrgency
    continuation_checkpoint_id: StableId | None = None


class EvidenceItem(DomainModel):
    item_id: StableId
    evidence_ref: EvidenceRef
    summary: str
    relevance_score: float = Field(ge=0.0, le=1.0)


class EvidencePack(DomainModel):
    query: QueryContract
    items: tuple[EvidenceItem, ...] = ()
    unresolved_gaps: tuple[str, ...] = ()


class ContextAssemblyPlan(DomainModel):
    plan_id: StableId
    base_commit: CommitId
    source_query_ids: tuple[StableId, ...] = ()
    mandatory_item_ids: tuple[StableId, ...] = ()
    optional_item_ids: tuple[StableId, ...] = ()
    token_budget: int = Field(ge=1)


class ContextPackage(DomainModel):
    context_id: StableId
    base_commit: CommitId
    assembly_plan_id: StableId
    mandatory_constraints: tuple[EvidenceItem, ...] = ()
    current_world_state: tuple[EvidenceItem, ...] = ()
    relationship_and_emotion: tuple[EvidenceItem, ...] = ()
    relevant_historical_events: tuple[EvidenceItem, ...] = ()
    truth_and_knowledge_boundaries: tuple[EvidenceItem, ...] = ()
    unresolved_gaps: tuple[str, ...] = ()
    token_count: int = Field(ge=0)
