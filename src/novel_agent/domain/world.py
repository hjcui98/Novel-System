"""Minimal plan, entity, event, state, relation, and time contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, JsonValue, model_validator

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import StableId
from novel_agent.domain.text import EvidenceRef


class TruthClass(StrEnum):
    ACCEPTED_WORLD_FACT = "accepted_world_fact"
    ASSERTION = "assertion"
    RUMOR = "rumor"
    DREAM = "dream"
    PREDICTION = "prediction"
    HYPOTHETICAL = "hypothetical"
    UNKNOWN = "unknown"
    CONTESTED = "contested"
    DISPROVED = "disproved"
    RETCONNED = "retconned"
    NOT_APPLICABLE = "not_applicable"


class StoryTime(DomainModel):
    worldline: str = Field(min_length=1)
    start_ordinal: int | None = None
    end_ordinal: int | None = None
    label: str | None = None

    @model_validator(mode="after")
    def validate_order(self) -> StoryTime:
        if (
            self.start_ordinal is not None
            and self.end_ordinal is not None
            and self.end_ordinal < self.start_ordinal
        ):
            raise ValueError("story time end must be greater than or equal to start")
        return self


class NarrativeOrder(DomainModel):
    chapter_index: int = Field(ge=0)
    scene_index: int | None = Field(default=None, ge=0)
    block_index: int | None = Field(default=None, ge=0)


class PlanNode(DomainModel):
    plan_node_id: StableId
    node_type: str = Field(min_length=1)
    title: str = Field(min_length=1)
    summary: str
    parent_id: StableId | None = None
    obligation_ids: tuple[StableId, ...] = ()


class Entity(DomainModel):
    entity_id: StableId
    entity_type: str = Field(min_length=1)
    internal_label: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    identity_invariants: tuple[str, ...] = ()


class Event(DomainModel):
    event_id: StableId
    event_type: str = Field(min_length=1)
    participant_ids: tuple[StableId, ...] = ()
    story_time: StoryTime | None = None
    narrative_order: NarrativeOrder | None = None
    effect_refs: tuple[StableId, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    truth_class: TruthClass


class StateRecord(DomainModel):
    state_id: StableId
    subject_id: StableId
    predicate: str = Field(min_length=1)
    value: JsonValue
    valid_time: StoryTime
    evidence_refs: tuple[EvidenceRef, ...] = ()
    truth_class: TruthClass


class RelationRecord(DomainModel):
    relation_id: StableId
    predicate: str = Field(min_length=1)
    subject_id: StableId
    object_id: StableId
    valid_time: StoryTime
    evidence_refs: tuple[EvidenceRef, ...] = ()
    truth_class: TruthClass
