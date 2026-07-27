"""Candidate changes, validation, and atomic commit contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints, model_validator

from novel_agent.domain.artifacts import ArtifactRef, RootKind, RootManifest
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
)
from novel_agent.domain.text import EvidenceRef, TextSpanRef
from novel_agent.domain.world import NarrativeOrder, TruthClass


class ChangeOperationType(StrEnum):
    CREATE = "create"
    REPLACE = "replace"
    RETIRE = "retire"


class WorldRecordKind(StrEnum):
    ENTITY = "entity"
    EVENT = "event"
    STATE = "state"
    RELATION = "relation"
    OBLIGATION = "obligation"


class ExtractionRule(DomainModel):
    rule_id: StableId
    phrase: str = Field(min_length=1)
    operation: ChangeOperationType
    record_kind: WorldRecordKind
    target_id: StableId
    record: dict[str, JsonValue]


class CuratorEvidenceSelection(TextSpanRef):
    """Minimal text-span selection emitted by a semantic Curator."""


CuratorShortText = Annotated[str, StringConstraints(min_length=1, max_length=160)]
CuratorScalar = CuratorShortText | int | float | bool | None


class CuratorStoryTime(DomainModel):
    worldline: CuratorShortText
    start_ordinal: int | None = None
    end_ordinal: int | None = None
    label: CuratorShortText | None = None

    @model_validator(mode="after")
    def validate_order(self) -> CuratorStoryTime:
        if (
            self.start_ordinal is not None
            and self.end_ordinal is not None
            and self.end_ordinal < self.start_ordinal
        ):
            raise ValueError("Curator story time end precedes start")
        return self


class CuratorEntityRecord(DomainModel):
    entity_type: CuratorShortText
    internal_label: CuratorShortText
    aliases: tuple[CuratorShortText, ...] = Field(default=(), max_length=4)
    identity_invariants: tuple[CuratorShortText, ...] = Field(default=(), max_length=4)


class CuratorEventRecord(DomainModel):
    event_type: CuratorShortText
    participant_ids: tuple[StableId, ...] = Field(default=(), max_length=6)
    story_time: CuratorStoryTime | None = None
    narrative_order: NarrativeOrder | None = None
    effect_refs: tuple[StableId, ...] = Field(default=(), max_length=4)
    truth_class: TruthClass


class CuratorStateRecord(DomainModel):
    subject_id: StableId
    predicate: CuratorShortText
    value: CuratorScalar
    valid_time: CuratorStoryTime
    truth_class: TruthClass


class CuratorRelationRecord(DomainModel):
    predicate: CuratorShortText
    subject_id: StableId
    object_id: StableId
    valid_time: CuratorStoryTime
    truth_class: TruthClass


class CuratorObligationRecord(DomainModel):
    kind: Literal["foreshadowing", "promise", "objective", "unresolved_conflict"]
    description: CuratorShortText
    status: Literal["open", "progressed", "resolved", "abandoned"]
    owner_ids: tuple[StableId, ...] = Field(default=(), max_length=6)
    due_chapter: int | None = Field(default=None, ge=1)


CuratorTypedRecord = (
    CuratorEntityRecord
    | CuratorEventRecord
    | CuratorStateRecord
    | CuratorRelationRecord
    | CuratorObligationRecord
)


class CuratedOperationDraft(DomainModel):
    operation: ChangeOperationType
    record_kind: WorldRecordKind
    target_id: StableId
    record: CuratorTypedRecord
    evidence_refs: tuple[CuratorEvidenceSelection, ...] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def validate_record_kind(self) -> CuratedOperationDraft:
        expected = {
            WorldRecordKind.ENTITY: CuratorEntityRecord,
            WorldRecordKind.EVENT: CuratorEventRecord,
            WorldRecordKind.STATE: CuratorStateRecord,
            WorldRecordKind.RELATION: CuratorRelationRecord,
            WorldRecordKind.OBLIGATION: CuratorObligationRecord,
        }[self.record_kind]
        if not isinstance(self.record, expected):
            raise ValueError("Curator record_kind does not match typed record")
        return self


class ChapterChangeDraft(DomainModel):
    chapter_index: int = Field(ge=1)
    operations: tuple[CuratedOperationDraft, ...] = Field(default=(), max_length=4)
    coverage: float = Field(default=1.0, ge=0, le=1)
    unresolved: tuple[CuratorShortText, ...] = Field(default=(), max_length=4)
    declared_vs_observed_diff: tuple[CuratorShortText, ...] = Field(default=(), max_length=4)


class EvidenceCandidate(DomainModel):
    """Trusted evidence span candidate; model may only copy candidate_id."""

    candidate_id: StableId
    block_id: StableId
    chapter_index: int = Field(ge=1)
    scene_index: int = Field(ge=0)
    text: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    content_hash: ArtifactId

    @model_validator(mode="after")
    def validate_span(self) -> EvidenceCandidate:
        if not (self.start < self.end):
            raise ValueError("evidence candidate requires start < end")
        return self


class EvidenceCandidateView(DomainModel):
    """Model-visible subset of a trusted evidence candidate."""

    candidate_id: StableId
    block_id: StableId
    text: str = Field(min_length=1)


class EvidenceQuoteSelection(DomainModel):
    """Exact-quote fallback when pre-split candidates cannot cover a span."""

    block_id: StableId
    exact_quote: str = Field(min_length=1, max_length=240)
    left_context: str | None = Field(default=None, max_length=80)
    right_context: str | None = Field(default=None, max_length=80)
    occurrence: int | None = Field(default=None, ge=0)


class CuratedOperationDraftV2(DomainModel):
    """Curator operation draft that references opaque evidence candidate IDs."""

    operation: ChangeOperationType
    record_kind: WorldRecordKind
    target_id: StableId
    record: CuratorTypedRecord
    evidence_candidate_ids: tuple[StableId, ...] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def validate_record_kind(self) -> CuratedOperationDraftV2:
        expected = {
            WorldRecordKind.ENTITY: CuratorEntityRecord,
            WorldRecordKind.EVENT: CuratorEventRecord,
            WorldRecordKind.STATE: CuratorStateRecord,
            WorldRecordKind.RELATION: CuratorRelationRecord,
            WorldRecordKind.OBLIGATION: CuratorObligationRecord,
        }[self.record_kind]
        if not isinstance(self.record, expected):
            raise ValueError("Curator record_kind does not match typed record")
        return self


class ChapterChangeDraftV2(DomainModel):
    """V2 Curator draft: no model-emitted character offsets."""

    chapter_index: int = Field(ge=1)
    operations: tuple[CuratedOperationDraftV2, ...] = Field(max_length=4)
    coverage: float = Field(default=1.0, ge=0, le=1)
    unresolved: tuple[CuratorShortText, ...] = Field(default=(), max_length=4)
    declared_vs_observed_diff: tuple[CuratorShortText, ...] = Field(default=(), max_length=4)
    no_durable_delta_reason: CuratorShortText | None = None
    no_op_evidence_candidate_ids: tuple[StableId, ...] = Field(
        default=(),
        max_length=4,
    )

    @model_validator(mode="after")
    def validate_explicit_no_op(self) -> ChapterChangeDraftV2:
        if self.operations and (
            self.no_durable_delta_reason is not None or self.no_op_evidence_candidate_ids
        ):
            raise ValueError("non-empty Curator draft cannot include no-op proof")
        return self


class EvidenceSupportDisposition(StrEnum):
    SUPPORTS = "supports"
    PARTIAL = "partial"
    CONTRADICTS = "contradicts"
    UNRELATED = "unrelated"


class EvidenceSupportDecision(DomainModel):
    operation_index: int = Field(ge=0)
    candidate_id: StableId
    disposition: EvidenceSupportDisposition
    reason_code: str = Field(min_length=1, max_length=64)


class EvidenceRepairAction(StrEnum):
    REPLACE_EVIDENCE = "replace_evidence"
    DROP_OPERATION = "drop_operation"
    MARK_UNRESOLVED = "mark_unresolved"


class EvidenceRepairDraft(DomainModel):
    """Field-level evidence-only repair; record payload is immutable."""

    operation_index: int = Field(ge=0)
    replacement_candidate_ids: tuple[StableId, ...] = ()
    action: EvidenceRepairAction


class StateTransitionEdge(DomainModel):
    from_value: JsonValue
    to_value: JsonValue


class StateTransitionRule(DomainModel):
    predicate: str = Field(min_length=1)
    allowed: tuple[StateTransitionEdge, ...] = Field(min_length=1)


class StateTransitionPolicy(DomainModel):
    policy_id: StableId
    schema_version: SchemaVersion
    rules: tuple[StateTransitionRule, ...] = ()
    allow_unlisted_predicates: bool = True

    @model_validator(mode="after")
    def validate_rules(self) -> StateTransitionPolicy:
        predicates = tuple(rule.predicate for rule in self.rules)
        if len(predicates) != len(set(predicates)):
            raise ValueError("state transition policy predicates must be unique")
        return self


class ModelValidationSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class ModelValidationFindingDraft(DomainModel):
    code: str = Field(min_length=1)
    severity: ModelValidationSeverity
    message: str = Field(min_length=1)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)


class ModelValidationDraft(DomainModel):
    findings: tuple[ModelValidationFindingDraft, ...] = ()


class ChangeOperation(DomainModel):
    operation_id: StableId
    root_kind: RootKind
    operation: ChangeOperationType
    target_id: StableId
    payload: JsonValue
    evidence_refs: tuple[EvidenceRef, ...] = ()


class ObservedChangeSet(DomainModel):
    change_set_id: StableId
    base_commit: CommitId
    source_artifact: ArtifactRef
    operations: tuple[ChangeOperation, ...] = ()


class CandidateChangeBundle(DomainModel):
    bundle_id: StableId
    project_id: ProjectId
    run_id: RunId
    base_commit: CommitId
    observed_changes: ObservedChangeSet
    proposed_roots: RootManifest
    produced_artifacts: tuple[ArtifactRef, ...] = ()


class ValidationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


class ValidationFinding(DomainModel):
    code: str = Field(min_length=1)
    severity: str = Field(min_length=1)
    message: str = Field(min_length=1)
    evidence_refs: tuple[EvidenceRef, ...] = ()


class ValidationReport(DomainModel):
    report_id: StableId
    bundle_id: StableId
    status: ValidationStatus
    findings: tuple[ValidationFinding, ...] = ()
    schema_version: SchemaVersion
    validation_profile: str = Field(default="stage1-validator-v1", min_length=1)
    validated_at: datetime


class CommitRequest(DomainModel):
    request_id: StableId
    project_id: ProjectId
    base_commit: CommitId
    idempotency_key: StableId
    bundle: CandidateChangeBundle
    validation_report: ValidationReport


class CommitStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CONFLICTED = "conflicted"


class CommitResult(DomainModel):
    request_id: StableId
    status: CommitStatus
    commit_id: CommitId | None = None
    manifest: RootManifest | None = None
    reason: str | None = None
    committed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> CommitResult:
        if self.status is CommitStatus.ACCEPTED:
            if self.commit_id is None or self.manifest is None or self.committed_at is None:
                raise ValueError("accepted commit requires commit_id, manifest, and committed_at")
        elif self.reason is None:
            raise ValueError("non-accepted commit requires a reason")
        return self
