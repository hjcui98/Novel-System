"""Stage 3 Editor review, repair, and Writer/Curator reconciliation contracts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from pydantic import Field, StringConstraints, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.generation import (
    DeclaredMemoryHint,
    DraftArtifact,
    MemoryHintChangeKind,
    RewriteDirective,
    RewriteScope,
    WritingTaskContract,
)
from novel_agent.domain.ids import ArtifactId, CommitId, StableId
from novel_agent.domain.model_calls import ModelCallRecord
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentType,
    ExecutionStatus,
)

if TYPE_CHECKING:

    class _EditorContext(Protocol):
        @property
        def context_id(self) -> StableId: ...

        @property
        def base_commit(self) -> CommitId: ...

        @property
        def snapshot_id(self) -> StableId: ...

        @property
        def task_contract(self) -> str: ...

    type WriterContextSnapshot = _EditorContext
else:
    try:
        from novel_agent.domain.generation import WriterContextSnapshot
    except ImportError:  # pragma: no cover - compatibility with the isolated pre-migration Writer
        from novel_agent.domain.memory import Stage1ContextPackage as WriterContextSnapshot

_NonEmptyText = Annotated[str, StringConstraints(min_length=1)]


class EditorialVerdict(StrEnum):
    """The only routing outcomes an Editor may produce."""

    PASS = "PASS"
    LOCAL_REPAIR = "LOCAL_REPAIR"
    MAJOR_REWRITE = "MAJOR_REWRITE"


class EditorialIssueType(StrEnum):
    CONSTRAINT_VIOLATION = "constraint_violation"
    CONTINUITY = "continuity"
    POV = "pov"
    DISCLOSURE = "disclosure"
    PLAN = "plan"
    STYLE = "style"
    CONTEXT_GAP = "context_gap"
    UNSUPPORTED_CHANGE = "unsupported_change"
    STRUCTURE = "structure"


class EditorialSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class EditorialTerminalStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class ReconciliationClass(StrEnum):
    MATCHED = "MATCHED"
    DECLARED_ONLY = "DECLARED_ONLY"
    OBSERVED_ONLY = "OBSERVED_ONLY"
    MISMATCHED = "MISMATCHED"


class DraftSpan(DomainModel):
    """A trusted, service-generated character range in a candidate Draft."""

    block_id: StableId | None = None
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_order(self) -> DraftSpan:
        if self.end < self.start:
            raise ValueError("Draft span end precedes start")
        return self


class EditorialLocation(DomainModel):
    """A review issue location; offsets are trusted only after service resolution."""

    block_id: StableId | None = None
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    evidence_quote: _NonEmptyText | None = None
    occurrence: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_location(self) -> EditorialLocation:
        if (self.start is None) != (self.end is None):
            raise ValueError("Editorial location start and end must be supplied together")
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError("Editorial location end precedes start")
        if self.start is None and self.block_id is not None:
            raise ValueError("Editorial block id requires a resolved character range")
        return self


class EditorialIssue(DomainModel):
    issue_id: StableId
    issue_type: EditorialIssueType
    severity: EditorialSeverity
    description: _NonEmptyText
    location: EditorialLocation | None = None
    repairable: bool = False
    structural: bool = False


class LocalRepairScope(DomainModel):
    """The complete trusted boundary an Editor local repair may change."""

    issue_ids: tuple[StableId, ...] = Field(min_length=1)
    allowed_spans: tuple[DraftSpan, ...] = Field(min_length=1)
    instructions: tuple[_NonEmptyText, ...] = Field(min_length=1)
    preserve_requirements: tuple[_NonEmptyText, ...] = ()

    @model_validator(mode="after")
    def validate_scope(self) -> LocalRepairScope:
        if len(self.issue_ids) != len(set(self.issue_ids)):
            raise ValueError("local repair issue ids must be unique")
        return self


class EditorialRepairHistoryEntry(DomainModel):
    report_id: StableId
    draft_id: ArtifactId
    verdict: EditorialVerdict
    repaired_draft_id: ArtifactId | None = None


class EditorialReviewInput(DomainModel):
    """Minimum trusted input for an independent read-only Editor review."""

    draft: DraftArtifact
    writing_task: WritingTaskContract
    context: WriterContextSnapshot
    prior_repair_history: tuple[EditorialRepairHistoryEntry, ...] = ()

    @model_validator(mode="after")
    def validate_same_candidate_task(self) -> EditorialReviewInput:
        if self.context.task_contract != self.writing_task.contract_id.root:
            raise ValueError("Editor context and WritingTaskContract belong to different tasks")
        basis = self.draft.basis
        if (
            basis.base_commit != self.context.base_commit
            or basis.snapshot_id != self.context.snapshot_id
            or basis.context_id != self.context.context_id
        ):
            raise ValueError("Editor Draft and Context belong to different snapshots")
        draft_ids = tuple(item.draft_id for item in self.prior_repair_history)
        if any(item_id == self.draft.draft_id for item_id in draft_ids):
            raise ValueError("Editor repair history cannot contain the current Draft")
        return self


class EditorialIssueDraft(DomainModel):
    """Untrusted model issue output; offsets and IDs are bound by the service."""

    issue_type: EditorialIssueType
    severity: EditorialSeverity
    description: _NonEmptyText
    evidence_quote: _NonEmptyText | None = None
    occurrence: int = Field(default=0, ge=0)
    block_hint: _NonEmptyText | None = None
    repairable: bool = False
    structural: bool = False


class EditorReviewPayload(DomainModel):
    """Structured response accepted from the Editor REVIEW model call."""

    verdict: EditorialVerdict
    issues: tuple[EditorialIssueDraft, ...] = ()
    repair_instructions: tuple[_NonEmptyText, ...] = ()
    preserve_requirements: tuple[_NonEmptyText, ...] = ()
    rewrite_targets: tuple[_NonEmptyText, ...] = ()
    rewrite_preserve_requirements: tuple[_NonEmptyText, ...] = ()
    planner_replan_required: bool = False
    unresolved_needs: tuple[_NonEmptyText, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def normalize_advisory_route(cls, value: object) -> object:
        """Keep unresolved-but-related context advisory instead of inventing a rewrite."""

        if not isinstance(value, Mapping):
            return value
        raw = dict(value)
        for field in (
            "issues",
            "repair_instructions",
            "preserve_requirements",
            "rewrite_targets",
            "rewrite_preserve_requirements",
            "unresolved_needs",
        ):
            sequence = raw.get(field)
            if isinstance(sequence, list):
                raw[field] = tuple(sequence)
        verdict = raw.get("verdict")
        issues = raw.get("issues", ())
        unresolved_needs = raw.get("unresolved_needs", ())
        if (
            verdict not in {EditorialVerdict.PASS, EditorialVerdict.PASS.value}
            and not issues
            and unresolved_needs
            and not any(
                raw.get(field)
                for field in (
                    "repair_instructions",
                    "preserve_requirements",
                    "rewrite_targets",
                    "rewrite_preserve_requirements",
                )
            )
        ):
            raw["verdict"] = EditorialVerdict.PASS.value
            raw["planner_replan_required"] = False
        if verdict in {
            EditorialVerdict.MAJOR_REWRITE,
            EditorialVerdict.MAJOR_REWRITE.value,
        } and not raw.get("rewrite_targets"):
            blocking_descriptions = tuple(
                description
                for issue in issues
                for description in (
                    issue.get("description")
                    if isinstance(issue, Mapping)
                    else getattr(issue, "description", None),
                )
                if isinstance(description, str)
                and description.strip()
                and (
                    (
                        issue.get("structural")
                        if isinstance(issue, Mapping)
                        else getattr(issue, "structural", False)
                    )
                    is True
                    or (
                        issue.get("severity")
                        if isinstance(issue, Mapping)
                        else getattr(issue, "severity", None)
                    )
                    in {EditorialSeverity.CRITICAL, EditorialSeverity.CRITICAL.value}
                )
            )
            if blocking_descriptions:
                raw["rewrite_targets"] = tuple(
                    f"Resolve the blocking editorial issue: {description}"
                    for description in blocking_descriptions
                )
        return raw

    @model_validator(mode="after")
    def validate_model_route(self) -> EditorReviewPayload:
        if self.verdict is EditorialVerdict.LOCAL_REPAIR:
            aligned = tuple(
                issue.model_copy(update={"repairable": True})
                if (
                    issue.severity in {EditorialSeverity.ERROR, EditorialSeverity.CRITICAL}
                    and not issue.structural
                    and not issue.repairable
                )
                else issue
                for issue in self.issues
            )
            if aligned != self.issues:
                self = self.model_copy(update={"issues": aligned})
        if self.verdict is EditorialVerdict.PASS:
            if (
                self.repair_instructions
                or self.preserve_requirements
                or self.rewrite_targets
                or self.rewrite_preserve_requirements
                or self.planner_replan_required
            ):
                raise ValueError("PASS cannot include repair, rewrite, or replan instructions")
            if any(
                issue.repairable
                or issue.structural
                or issue.severity in {EditorialSeverity.ERROR, EditorialSeverity.CRITICAL}
                for issue in self.issues
            ):
                raise ValueError("PASS cannot include blocking editorial issues")
        elif not self.issues:
            raise ValueError("non-PASS Editor verdict requires at least one issue")
        if self.verdict is EditorialVerdict.LOCAL_REPAIR:
            if not self.repair_instructions:
                raise ValueError("LOCAL_REPAIR requires repair instructions")
            if self.rewrite_targets or self.rewrite_preserve_requirements:
                raise ValueError("LOCAL_REPAIR cannot include major rewrite instructions")
            if self.planner_replan_required:
                raise ValueError("LOCAL_REPAIR cannot request a Planner replan")
            if any(
                issue.structural
                or (
                    issue.severity in {EditorialSeverity.ERROR, EditorialSeverity.CRITICAL}
                    and not issue.repairable
                )
                for issue in self.issues
            ):
                raise ValueError("LOCAL_REPAIR requires every blocking issue to be repairable")
        if self.verdict is EditorialVerdict.MAJOR_REWRITE:
            if not self.rewrite_targets:
                raise ValueError("MAJOR_REWRITE requires rewrite targets")
            if self.repair_instructions or self.preserve_requirements:
                raise ValueError("MAJOR_REWRITE cannot include local repair instructions")
            if not any(
                issue.structural or issue.severity is EditorialSeverity.CRITICAL
                for issue in self.issues
            ):
                raise ValueError("MAJOR_REWRITE requires a structural or critical issue")
        return self


class EditorRepairPayload(DomainModel):
    """Structured response for one bounded local repair."""

    repaired_text: _NonEmptyText
    self_observations: tuple[_NonEmptyText, ...] = ()

    @model_validator(mode="after")
    def validate_text(self) -> EditorRepairPayload:
        if not self.repaired_text.strip():
            raise ValueError("Editor repaired text must not be blank")
        return self


class EditorialReport(DomainModel):
    """Read-only Editor result used for routing; it never authorizes a write."""

    report_id: StableId
    draft_id: ArtifactId
    task_contract_id: StableId
    context_id: StableId
    base_commit: CommitId
    verdict: EditorialVerdict
    issues: tuple[EditorialIssue, ...] = ()
    repair_scope: LocalRepairScope | None = None
    rewrite_directive: RewriteDirective | None = None
    planner_replan_required: bool = False
    unresolved_needs: tuple[_NonEmptyText, ...] = ()
    receipt: AgentExecutionReceipt
    model_call_record: ModelCallRecord | None = None
    created_at: datetime

    @model_validator(mode="after")
    def validate_report_route(self) -> EditorialReport:
        issue_ids = tuple(issue.issue_id for issue in self.issues)
        if len(issue_ids) != len(set(issue_ids)):
            raise ValueError("Editorial issue ids must be unique")
        if self.verdict is EditorialVerdict.PASS:
            if (
                self.repair_scope is not None
                or self.rewrite_directive is not None
                or self.planner_replan_required
            ):
                raise ValueError("PASS cannot carry a repair scope or rewrite directive")
            if any(
                issue.repairable
                or issue.structural
                or issue.severity in {EditorialSeverity.ERROR, EditorialSeverity.CRITICAL}
                for issue in self.issues
            ):
                raise ValueError("PASS cannot carry blocking issues")
        elif self.verdict is EditorialVerdict.LOCAL_REPAIR:
            if (
                self.repair_scope is None
                or self.rewrite_directive is not None
                or self.planner_replan_required
            ):
                raise ValueError("LOCAL_REPAIR requires only a repair scope")
            scoped_ids = set(self.repair_scope.issue_ids)
            if not scoped_ids.issubset(issue_ids):
                raise ValueError("local repair scope references an unknown issue")
            blocking = {
                issue.issue_id
                for issue in self.issues
                if issue.repairable
                or issue.structural
                or issue.severity in {EditorialSeverity.ERROR, EditorialSeverity.CRITICAL}
            }
            if not blocking.issubset(scoped_ids) or any(
                issue.structural or (issue.issue_id in blocking and not issue.repairable)
                for issue in self.issues
            ):
                raise ValueError("LOCAL_REPAIR does not cover all blocking issues")
        else:
            directive = self.rewrite_directive
            if self.repair_scope is not None or directive is None:
                raise ValueError("MAJOR_REWRITE requires only a rewrite directive")
            if directive.parent_draft_id != self.draft_id:
                raise ValueError("rewrite directive parent does not match reviewed Draft")
            if directive.scope is not RewriteScope.MAJOR_REWRITE:
                raise ValueError("Editor major rewrite directive has the wrong scope")
            if not any(
                issue.structural or issue.severity is EditorialSeverity.CRITICAL
                for issue in self.issues
            ):
                raise ValueError("MAJOR_REWRITE requires a structural or critical issue")
        if self.receipt.agent_type is not AgentType.EDITOR:
            raise ValueError("EditorialReport receipt must belong to the Editor")
        if self.receipt.agent_mode is not AgentMode.REVIEW:
            raise ValueError("EditorialReport receipt must be a REVIEW execution")
        if self.receipt.status is not ExecutionStatus.SUCCEEDED:
            raise ValueError("EditorialReport requires a successful Editor receipt")
        return self


class RepairedDraft(DomainModel):
    """A new candidate text produced by one bounded Editor local repair."""

    draft_id: ArtifactId
    parent_draft_id: ArtifactId
    repair_report_id: StableId
    text_artifact: ArtifactRef
    changed_spans: tuple[DraftSpan, ...] = Field(min_length=1)
    editor_receipt: AgentExecutionReceipt
    model_call_record: ModelCallRecord | None = None
    created_at: datetime
    candidate_only: Literal[True] = True
    preserve_verified: Literal[True] = True

    @model_validator(mode="after")
    def validate_repair_lineage(self) -> RepairedDraft:
        if self.draft_id == self.parent_draft_id:
            raise ValueError("Repaired Draft must have a new candidate identity")
        if self.editor_receipt.agent_type is not AgentType.EDITOR:
            raise ValueError("Repaired Draft receipt must belong to the Editor")
        if self.editor_receipt.agent_mode is not AgentMode.LOCAL_REPAIR:
            raise ValueError("Repaired Draft receipt must be a LOCAL_REPAIR execution")
        if self.editor_receipt.status is not ExecutionStatus.SUCCEEDED:
            raise ValueError("Repaired Draft requires a successful Editor receipt")
        return self

    @property
    def source_draft_id(self) -> ArtifactId:
        return self.parent_draft_id


class CuratorChangeObservation(DomainModel):
    """A minimal independent observation extracted from the repaired/current Draft."""

    observation_id: StableId
    subject_hint: _NonEmptyText
    change_kind: MemoryHintChangeKind
    predicate_hint: _NonEmptyText | None = None
    value_hint: _NonEmptyText | None = None
    evidence_quote: _NonEmptyText | None = None
    target_id: StableId | None = None


class CandidateObservationPayload(DomainModel):
    """Model-only observation output; service telemetry is bound after generation."""

    draft_id: ArtifactId
    changes: tuple[CuratorChangeObservation, ...] = Field(default=(), max_length=4)


class CuratorObservation(DomainModel):
    """Observation set explicitly bound to the Draft it read."""

    draft_id: ArtifactId
    changes: tuple[CuratorChangeObservation, ...] = ()
    model_call_record: ModelCallRecord | None = None

    @model_validator(mode="after")
    def validate_change_ids(self) -> CuratorObservation:
        ids = tuple(change.observation_id for change in self.changes)
        if len(ids) != len(set(ids)):
            raise ValueError("Curator observation ids must be unique")
        return self


class ReconciliationComparison(DomainModel):
    comparison_id: StableId
    classification: ReconciliationClass
    writer_hint_index: int | None = Field(default=None, ge=0)
    observation_id: StableId | None = None
    writer_hint: DeclaredMemoryHint | None = None
    observation: CuratorChangeObservation | None = None
    reason: _NonEmptyText

    @model_validator(mode="after")
    def validate_sides(self) -> ReconciliationComparison:
        if self.classification is ReconciliationClass.MATCHED and (
            self.writer_hint is None or self.observation is None
        ):
            raise ValueError("MATCHED reconciliation requires both sides")
        if self.classification is ReconciliationClass.DECLARED_ONLY and (
            self.writer_hint is None
            or self.writer_hint_index is None
            or self.observation is not None
        ):
            raise ValueError("DECLARED_ONLY reconciliation requires only Writer data")
        if self.classification is ReconciliationClass.OBSERVED_ONLY and (
            self.writer_hint is not None
            or self.writer_hint_index is not None
            or self.observation is None
        ):
            raise ValueError("OBSERVED_ONLY reconciliation requires only Curator data")
        if self.classification is ReconciliationClass.MISMATCHED and (
            self.writer_hint is None or self.writer_hint_index is None or self.observation is None
        ):
            raise ValueError("MISMATCHED reconciliation requires both sides")
        if self.classification is ReconciliationClass.MATCHED and self.writer_hint_index is None:
            raise ValueError("MATCHED reconciliation requires a Writer hint index")
        if (self.observation_id is None) != (self.observation is None):
            raise ValueError("reconciliation observation id does not match its observation")
        return self


class ReconciliationResult(DomainModel):
    """Deterministic comparison of weak Writer declarations and Curator observations."""

    result_id: StableId
    draft_id: ArtifactId
    writer_hints: tuple[DeclaredMemoryHint, ...] = ()
    curator_observation: CuratorObservation
    comparisons: tuple[ReconciliationComparison, ...] = ()

    @model_validator(mode="after")
    def validate_binding(self) -> ReconciliationResult:
        if self.curator_observation.draft_id != self.draft_id:
            raise ValueError("Reconciliation result is bound to another Draft")
        writer_indexes = tuple(
            item.writer_hint_index
            for item in self.comparisons
            if item.writer_hint_index is not None
        )
        if len(writer_indexes) != len(self.writer_hints) or set(writer_indexes) != set(
            range(len(self.writer_hints))
        ):
            raise ValueError("Reconciliation result must account for every Writer hint once")
        observation_ids = tuple(
            item.observation_id for item in self.comparisons if item.observation_id is not None
        )
        expected_observation_ids = {
            item.observation_id for item in self.curator_observation.changes
        }
        if (
            len(observation_ids) != len(expected_observation_ids)
            or set(observation_ids) != expected_observation_ids
        ):
            raise ValueError("Reconciliation result must account for every observation once")
        return self

    @property
    def matched(self) -> tuple[ReconciliationComparison, ...]:
        return tuple(
            item for item in self.comparisons if item.classification is ReconciliationClass.MATCHED
        )

    @property
    def declared_only(self) -> tuple[ReconciliationComparison, ...]:
        return tuple(
            item
            for item in self.comparisons
            if item.classification is ReconciliationClass.DECLARED_ONLY
        )

    @property
    def observed_only(self) -> tuple[ReconciliationComparison, ...]:
        return tuple(
            item
            for item in self.comparisons
            if item.classification is ReconciliationClass.OBSERVED_ONLY
        )

    @property
    def mismatched(self) -> tuple[ReconciliationComparison, ...]:
        return tuple(
            item
            for item in self.comparisons
            if item.classification is ReconciliationClass.MISMATCHED
        )


__all__ = [
    "CandidateObservationPayload",
    "CuratorChangeObservation",
    "CuratorObservation",
    "DraftSpan",
    "EditorRepairPayload",
    "EditorReviewPayload",
    "EditorialIssue",
    "EditorialIssueDraft",
    "EditorialIssueType",
    "EditorialLocation",
    "EditorialRepairHistoryEntry",
    "EditorialReport",
    "EditorialReviewInput",
    "EditorialSeverity",
    "EditorialTerminalStatus",
    "EditorialVerdict",
    "LocalRepairScope",
    "ReconciliationClass",
    "ReconciliationComparison",
    "ReconciliationResult",
    "RepairedDraft",
]
