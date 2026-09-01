"""Minimal plan, entity, event, state, relation, and time contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, CommitId, StableId
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


class GraphStoryTime(StoryTime):
    """Graph repair time constrained to the canonical main worldline."""

    # Keep this field required.  A model response that omits the worldline
    # must fail the bounded schema retry instead of silently inheriting the
    # canonical value and bypassing temporal-shape repair.
    worldline: Literal["main"]


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


class EntityResolutionStatus(StrEnum):
    UNIQUE_LABEL = "unique_label"
    UNIQUE_ALIAS = "unique_alias"
    AMBIGUOUS = "ambiguous"
    MISSING = "missing"


class RelationBackfillStatus(StrEnum):
    ACCEPTED = "accepted"
    DEDUPED = "deduped"
    REJECTED = "rejected"


class GraphCandidateSupportStatus(StrEnum):
    SUPPORTED = "supported"
    REJECTED = "rejected"


class EntityAdmissionStatus(StrEnum):
    REUSED = "reused"
    CREATED = "created"
    DEDUPED = "deduped"
    REJECTED = "rejected"


class GraphCandidatePageStatus(StrEnum):
    COMPLETE = "complete"
    HAS_MORE = "has_more"


class GraphSourceUnitStatus(StrEnum):
    CONTINUE = "continue"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class GraphEntityCandidateDraft(DomainModel):
    kind: Literal["entity"] = "entity"
    surface: str = Field(min_length=1, max_length=160)
    entity_type: str = Field(min_length=1, max_length=160)
    evidence_quotes: tuple[str, ...] = Field(min_length=1, max_length=4)


class GraphRelationCandidateDraft(DomainModel):
    kind: Literal["relation"] = "relation"
    subject_surface: str = Field(min_length=1, max_length=160)
    predicate: str = Field(min_length=1, max_length=160)
    object_surface: str = Field(min_length=1, max_length=160)
    valid_time: GraphStoryTime
    source_truth_class: TruthClass
    evidence_quotes: tuple[str, ...] = Field(min_length=1, max_length=4)

    @field_validator("valid_time", mode="before")
    @classmethod
    def coerce_graph_time(cls, value: object) -> object:
        if isinstance(value, StoryTime):
            if value.worldline != "main":
                raise ValueError("graph relation candidate valid_time must use worldline=main")
            return GraphStoryTime.model_validate(value.model_dump())
        if isinstance(value, dict) and value.get("worldline") not in {None, "main"}:
            raise ValueError("graph relation candidate valid_time must use worldline=main")
        return value

    @model_validator(mode="after")
    def validate_worldline(self) -> GraphRelationCandidateDraft:
        # Graph repair writes to the main canonical World timeline.  A model
        # may otherwise emit a schema-valid label such as ``chapter_95_end``;
        # reject it here so the bounded graph-page retry can correct the
        # temporal shape before support/admission rather than persisting a
        # parallel worldline.
        if self.valid_time.worldline != "main":
            raise ValueError("graph relation candidate valid_time must use worldline=main")
        return self


GraphCandidateDraft = Annotated[
    GraphEntityCandidateDraft | GraphRelationCandidateDraft,
    Field(discriminator="kind"),
]


class GraphCandidatePageDraft(DomainModel):
    status: GraphCandidatePageStatus
    candidates: tuple[GraphCandidateDraft, ...] = Field(default=(), max_length=12)
    no_graph_candidate_reason: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_page_semantics(self) -> GraphCandidatePageDraft:
        # Round-19 repair: canonicalize the demonstrated semantically empty
        # no-candidate forms before the conditional checks (review-19).  The
        # model emits small, complete page JSONs whose
        # `no_graph_candidate_reason` is an empty/whitespace string or is
        # omitted.  A page WITH candidates must not carry a no-op reason, so
        # the reason becomes None; an empty COMPLETE page must carry a reason,
        # so it gains the stable operational value
        # `model_returned_no_graph_candidates`.  Candidates, status, and any
        # substantive reason are never altered; an empty `has_more` page and a
        # non-empty page with a substantive reason still fail below.  (A
        # `mode="before"` hook must not mutate the input here: the domain is
        # strict and revalidates a mutated dict in Python mode, which rejects
        # JSON arrays for tuple fields.)
        reason = self.no_graph_candidate_reason
        canonical = reason
        if self.candidates and reason is not None and not reason.strip():
            canonical = None
        elif (
            not self.candidates
            and self.status is GraphCandidatePageStatus.COMPLETE
            and (reason is None or not reason.strip())
        ):
            canonical = "model_returned_no_graph_candidates"
        page = (
            self
            if canonical is reason
            else self.model_copy(update={"no_graph_candidate_reason": canonical})
        )
        if page.candidates and page.no_graph_candidate_reason is not None:
            raise ValueError("non-empty graph candidate page cannot carry a no-op reason")
        if not page.candidates and (
            page.status is not GraphCandidatePageStatus.COMPLETE
            or not page.no_graph_candidate_reason
        ):
            raise ValueError("empty graph candidate page must be complete and carry a reason")
        relation_endpoints = {
            surface
            for candidate in page.candidates
            if isinstance(candidate, GraphRelationCandidateDraft)
            for surface in (candidate.subject_surface, candidate.object_surface)
        }
        if any(
            candidate.surface not in relation_endpoints
            for candidate in page.candidates
            if isinstance(candidate, GraphEntityCandidateDraft)
        ):
            raise ValueError("entity candidate must be a relation endpoint in the same page")
        if any(
            candidate.source_truth_class
            in {
                TruthClass.UNKNOWN,
                TruthClass.NOT_APPLICABLE,
            }
            for candidate in page.candidates
            if isinstance(candidate, GraphRelationCandidateDraft)
        ):
            raise ValueError("graph relation candidate requires an explicit source truth class")
        return page

    @property
    def entities(self) -> tuple[GraphEntityCandidateDraft, ...]:
        return tuple(
            item for item in self.candidates if isinstance(item, GraphEntityCandidateDraft)
        )

    @property
    def relations(self) -> tuple[GraphRelationCandidateDraft, ...]:
        return tuple(
            item for item in self.candidates if isinstance(item, GraphRelationCandidateDraft)
        )


class WorldGraphEntityCandidate(DomainModel):
    candidate_id: StableId
    source_batch_id: StableId
    surface: str = Field(min_length=1)
    entity_type: str = Field(min_length=1)
    evidence_refs: tuple[EvidenceRef, ...] = ()
    support_status: GraphCandidateSupportStatus
    support_reason: str = Field(min_length=1)


class WorldGraphRelationCandidate(DomainModel):
    candidate_id: StableId
    source_batch_id: StableId
    source_state_id: StableId | None = None
    subject_surface: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object_surface: str = Field(min_length=1)
    valid_time: StoryTime
    evidence_refs: tuple[EvidenceRef, ...] = ()
    source_truth_class: TruthClass
    support_status: GraphCandidateSupportStatus
    support_reason: str = Field(min_length=1)


class WorldGraphCandidateBatch(DomainModel):
    batch_id: StableId
    source_text_root: ArtifactId
    base_commit: CommitId
    chapter_index: int | None = Field(default=None, ge=1)
    source_unit_id: StableId | None = None
    page_index: int = Field(default=0, ge=0)
    unit_status: GraphSourceUnitStatus = GraphSourceUnitStatus.COMPLETE
    incomplete_reason: str | None = Field(default=None, min_length=1)
    source_candidate_ids: tuple[StableId, ...] = ()
    exact_evidence_candidate_ids: tuple[StableId, ...] = ()
    candidate_keys: tuple[str, ...] = ()
    deduped_candidate_keys: tuple[str, ...] = ()
    policy_version: str = Field(min_length=1)
    model_request_id: StableId | None = None
    entities: tuple[WorldGraphEntityCandidate, ...] = ()
    relations: tuple[WorldGraphRelationCandidate, ...] = ()

    @model_validator(mode="after")
    def validate_unit_status(self) -> WorldGraphCandidateBatch:
        if self.unit_status is GraphSourceUnitStatus.INCOMPLETE:
            if self.incomplete_reason is None:
                raise ValueError("incomplete graph source unit requires a reason")
        elif self.incomplete_reason is not None:
            raise ValueError("complete or continuing graph source unit cannot carry a reason")
        return self


class EntityAliasResolutionReceipt(DomainModel):
    receipt_id: StableId
    mention: str = Field(min_length=1)
    status: EntityResolutionStatus
    matched_entity_ids: tuple[StableId, ...] = ()
    resolved_entity_id: StableId | None = None
    match_basis: str = Field(min_length=1)
    evidence_refs: tuple[EvidenceRef, ...] = ()
    reason: str | None = None

    @model_validator(mode="after")
    def validate_resolution(self) -> EntityAliasResolutionReceipt:
        unique = self.status in {
            EntityResolutionStatus.UNIQUE_LABEL,
            EntityResolutionStatus.UNIQUE_ALIAS,
        }
        if unique and (
            self.resolved_entity_id is None or self.matched_entity_ids != (self.resolved_entity_id,)
        ):
            raise ValueError("unique entity resolution requires one matching resolved entity")
        if not unique and self.resolved_entity_id is not None:
            raise ValueError("non-unique entity resolution cannot expose a resolved entity")
        return self


class EntityAdmissionReceipt(DomainModel):
    candidate_id: StableId
    source_batch_id: StableId
    surface: str = Field(min_length=1)
    entity_type: str = Field(min_length=1)
    status: EntityAdmissionStatus
    entity_id: StableId | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    resolution: EntityAliasResolutionReceipt
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def validate_admission(self) -> EntityAdmissionReceipt:
        admitted = self.status in {
            EntityAdmissionStatus.REUSED,
            EntityAdmissionStatus.CREATED,
            EntityAdmissionStatus.DEDUPED,
        }
        if admitted and self.entity_id is None:
            raise ValueError("admitted entity candidate requires an entity id")
        if admitted and self.rejection_reason is not None:
            raise ValueError("admitted entity candidate cannot have a rejection reason")
        if not admitted and self.rejection_reason is None:
            raise ValueError("rejected entity candidate requires a reason")
        return self


class RelationBackfillReceipt(DomainModel):
    candidate_id: StableId
    source_batch_id: StableId
    source_state_id: StableId | None = None
    source_truth_class: TruthClass
    status: RelationBackfillStatus
    predicate: str = Field(min_length=1)
    subject_surface: str = Field(min_length=1)
    object_surface: str = Field(min_length=1)
    subject_id: StableId | None = None
    object_id: StableId | None = None
    relation_id: StableId | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    subject_resolution: EntityAliasResolutionReceipt | None = None
    object_resolution: EntityAliasResolutionReceipt | None = None
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def validate_status(self) -> RelationBackfillReceipt:
        if self.status is RelationBackfillStatus.ACCEPTED:
            if (
                self.subject_id is None
                or self.object_id is None
                or self.relation_id is None
                or not self.evidence_refs
            ):
                raise ValueError(
                    "accepted relation backfill requires endpoints, identity, and evidence"
                )
            if self.rejection_reason is not None:
                raise ValueError("accepted relation backfill cannot have a rejection reason")
        elif self.status is RelationBackfillStatus.DEDUPED:
            if self.subject_id is None or self.object_id is None or self.relation_id is None:
                raise ValueError("deduped relation requires canonical relation identity")
            if self.rejection_reason is not None:
                raise ValueError("deduped relation cannot have a rejection reason")
        elif self.rejection_reason is None:
            raise ValueError("rejected relation backfill requires a rejection reason")
        return self


class WorldGraphExtractionReceipt(DomainModel):
    receipt_id: StableId
    source_world_root: ArtifactId
    repaired_world_root: ArtifactId
    predicate_registry_version: str = Field(min_length=1)
    alias_policy_version: str = Field(min_length=1)
    source_batch_ids: tuple[StableId, ...] = ()
    completed_source_unit_ids: tuple[StableId, ...] = ()
    incomplete_source_unit_ids: tuple[StableId, ...] = ()
    entity_admissions: tuple[EntityAdmissionReceipt, ...] = ()
    candidates: tuple[RelationBackfillReceipt, ...] = ()
    accepted_relation_ids: tuple[StableId, ...] = ()
    retained_state_ids: tuple[StableId, ...] = ()
    accepted_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    deduped_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_accepted_relations(self) -> WorldGraphExtractionReceipt:
        if set(self.completed_source_unit_ids) & set(self.incomplete_source_unit_ids):
            raise ValueError("graph source unit cannot be both complete and incomplete")
        receipt_ids = tuple(
            candidate.relation_id
            for candidate in self.candidates
            if candidate.status is RelationBackfillStatus.ACCEPTED
        )
        if receipt_ids != self.accepted_relation_ids:
            raise ValueError("accepted relation ids must match accepted candidate receipts")
        statuses = tuple(admission.status.value for admission in self.entity_admissions) + tuple(
            candidate.status.value for candidate in self.candidates
        )
        if self.accepted_count != sum(
            status in {"accepted", "created", "reused"} for status in statuses
        ):
            raise ValueError("graph receipt accepted accounting mismatch")
        if self.rejected_count != statuses.count("rejected"):
            raise ValueError("graph receipt rejected accounting mismatch")
        if self.deduped_count != statuses.count("deduped"):
            raise ValueError("graph receipt deduped accounting mismatch")
        return self
