"""Stage 3 generation quality evaluation contracts (workstream D).

These are the machine-readable evaluation results and the minimal collection
contracts for Editor and reconciliation output.  The collection models are
deliberately tolerant of unknown fields so they can be populated from the
published Stage 3 Editor/Curator contracts through a thin adapter.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from novel_agent.domain.base import DomainModel
from novel_agent.domain.generation import (
    DraftArtifact,
    WriterExecutionResult,
    WriterTerminalStatus,
    WritingTaskContract,
)
from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import Stage1ContextPackage
from novel_agent.domain.writer_context import WriterContextPackage

_NonEmptyText = Annotated[str, StringConstraints(min_length=1)]
_TOLERANT_CONFIG = ConfigDict(extra="allow", strict=True, frozen=True)


class ContextScheme(StrEnum):
    """The three comparable Context inputs of a Stage 3 generation run."""

    RECENT_PROSE = "recent_prose"
    SIMPLE_RETRIEVAL = "simple_retrieval"
    WRITER_CONTEXT_PACKAGE = "writer_context_package"


class CaseInputStatus(StrEnum):
    READY = "ready"
    MISSING = "missing"


class Stage3FailureCategory(StrEnum):
    """Typed scheme status; a failed scheme is never replaced by another result."""

    INPUT_NOT_READY = "input_not_ready"
    WRITER_FAILED = "writer_failed"
    EDITOR_FAILED = "editor_failed"
    RECONCILIATION_FAILED = "reconciliation_failed"
    EVALUATION_FAILED = "evaluation_failed"
    COMPLETED = "completed"


class RuleCheckKind(StrEnum):
    """Deterministic, explainable rule checks over a candidate draft."""

    MANDATORY_CONSTRAINT_PRESENT = "mandatory_constraint_present"
    PLAN_OBLIGATION_PRESENT = "plan_obligation_present"
    PRESERVE_REQUIREMENT_PRESENT = "preserve_requirement_present"
    FORBIDDEN_REVEAL_ABSENT = "forbidden_reveal_absent"
    DRAFT_LENGTH_IN_POLICY = "draft_length_in_policy"
    DECLARED_HINT_EVIDENCE_PRESENT = "declared_hint_evidence_present"


class RuleCheckResult(DomainModel):
    check_id: StableId
    kind: RuleCheckKind
    passed: bool
    reference: _NonEmptyText
    detail: _NonEmptyText


class RuleAssessment(DomainModel):
    checks: tuple[RuleCheckResult, ...]

    @property
    def passed_count(self) -> int:
        return sum(1 for check in self.checks if check.passed)

    @property
    def failed_count(self) -> int:
        return len(self.checks) - self.passed_count

    def passed_for(self, kind: RuleCheckKind) -> bool | None:
        """True only when every check of ``kind`` passed; None when absent."""
        selected = tuple(check for check in self.checks if check.kind is kind)
        if not selected:
            return None
        return all(check.passed for check in selected)


class EditorialVerdict(StrEnum):
    PASS = "pass"
    LOCAL_REPAIR = "local_repair"
    MAJOR_REWRITE = "major_rewrite"


class CollectedEditorialIssue(DomainModel):
    model_config = _TOLERANT_CONFIG
    issue_type: str = ""
    severity: str = ""
    location: str = ""
    description: str = ""


class CollectedEditorialReport(DomainModel):
    """Minimal Editor collection contract; tolerant of future Editor fields."""

    model_config = _TOLERANT_CONFIG
    report_id: _NonEmptyText
    draft_id: _NonEmptyText
    verdict: EditorialVerdict
    issues: tuple[CollectedEditorialIssue, ...] = ()
    repair_count: int = Field(default=0, ge=0)
    rewrite_count: int = Field(default=0, ge=0)
    unresolved_issues: tuple[_NonEmptyText, ...] = ()

    @model_validator(mode="after")
    def validate_verdict_counts(self) -> CollectedEditorialReport:
        if self.verdict is EditorialVerdict.LOCAL_REPAIR and self.repair_count == 0:
            raise ValueError("LOCAL_REPAIR report requires at least one repair")
        if self.verdict is EditorialVerdict.MAJOR_REWRITE and self.rewrite_count == 0:
            raise ValueError("MAJOR_REWRITE report requires at least one rewrite")
        return self


class ReconciliationVerdict(StrEnum):
    MATCHED = "matched"
    DECLARED_ONLY = "declared_only"
    OBSERVED_ONLY = "observed_only"
    MISMATCHED = "mismatched"


class CollectedReconciliationItem(DomainModel):
    model_config = _TOLERANT_CONFIG
    verdict: ReconciliationVerdict
    subject: str = ""
    writer_hint: str = ""
    curator_observation: str = ""
    detail: str = ""


class CollectedReconciliationResult(DomainModel):
    """Minimal reconciliation collection contract; tolerant of future fields."""

    model_config = _TOLERANT_CONFIG
    result_id: _NonEmptyText
    draft_id: _NonEmptyText
    items: tuple[CollectedReconciliationItem, ...] = ()


class Stage3CaseContextInput(DomainModel):
    scheme: ContextScheme
    input_status: CaseInputStatus
    context_package: Stage1ContextPackage | None = None
    writer_context_package: WriterContextPackage | None = None
    entry: str = Field(default="fixture", min_length=1)

    @model_validator(mode="after")
    def validate_input_status(self) -> Stage3CaseContextInput:
        supplied = sum(
            value is not None for value in (self.context_package, self.writer_context_package)
        )
        if self.input_status is CaseInputStatus.READY and supplied != 1:
            raise ValueError("READY scheme input requires a context package")
        if self.input_status is CaseInputStatus.MISSING and supplied:
            raise ValueError("MISSING scheme input cannot carry a context package")
        if self.scheme is ContextScheme.WRITER_CONTEXT_PACKAGE:
            if self.input_status is CaseInputStatus.READY and self.writer_context_package is None:
                raise ValueError("WriterContextPackage scheme requires the formal package")
        elif self.writer_context_package is not None:
            raise ValueError("baseline schemes cannot carry WriterContextPackage")
        return self


class Stage3EvaluationCase(DomainModel):
    """One comparable generation case; evaluator-only data stays out of Writer input."""

    case_id: StableId
    writing_task: WritingTaskContract
    inputs: tuple[Stage3CaseContextInput, ...] = Field(min_length=1)
    evaluator_instructions: tuple[_NonEmptyText, ...] = ()

    @model_validator(mode="after")
    def validate_schemes(self) -> Stage3EvaluationCase:
        schemes = tuple(item.scheme for item in self.inputs)
        if len(schemes) != len(set(schemes)):
            raise ValueError("case scheme inputs must be unique")
        return self

    def input_for(self, scheme: ContextScheme) -> Stage3CaseContextInput | None:
        for item in self.inputs:
            if item.scheme is scheme:
                return item
        return None


class EvaluatorDimension(StrEnum):
    """Dimensions left to an independent evaluator or blind human review."""

    CONTINUITY_AND_FACT_CONFLICT = "continuity_and_fact_conflict"
    UNPLANNED_LEAK_OR_FABRICATION = "unplanned_leak_or_fabrication"
    PLAN_FOLLOWING = "plan_following"
    LITERARY_QUALITY_DEGRADATION = "literary_quality_degradation"


class EvaluatorScore(DomainModel):
    case_id: StableId
    scheme: ContextScheme
    dimension: EvaluatorDimension
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    rationale: str = ""
    source: Literal["scripted", "human"] = "scripted"


class Stage3SchemeResult(DomainModel):
    """Uniform per-scheme outcome; missing pieces are typed, never zeroed."""

    case_id: StableId
    scheme: ContextScheme
    status: Stage3FailureCategory
    writer: WriterExecutionResult | None = None
    draft: DraftArtifact | None = None
    editorial: CollectedEditorialReport | None = None
    reconciliation: CollectedReconciliationResult | None = None
    rules: RuleAssessment | None = None
    evaluator_scores: tuple[EvaluatorScore, ...] = ()
    failure_detail: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def validate_consistency(self) -> Stage3SchemeResult:
        if self.status is Stage3FailureCategory.INPUT_NOT_READY:
            if self.writer is not None or self.draft is not None:
                raise ValueError("INPUT_NOT_READY scheme result cannot carry Writer artifacts")
            if self.failure_detail is None:
                raise ValueError("INPUT_NOT_READY scheme result requires failure detail")
            return self
        if self.status is Stage3FailureCategory.WRITER_FAILED:
            if self.writer is None or self.draft is not None:
                raise ValueError("WRITER_FAILED scheme result requires only a failed Writer result")
            if self.writer.status is WriterTerminalStatus.COMPLETED:
                raise ValueError("WRITER_FAILED scheme result contains a completed Writer result")
            if self.failure_detail is None:
                raise ValueError("WRITER_FAILED scheme result requires failure detail")
            return self
        if self.writer is None or self.draft is None:
            raise ValueError("non-input scheme results require Writer result and draft")
        if self.writer.draft != self.draft:
            raise ValueError("scheme draft differs from its Writer result draft")
        if self.status is Stage3FailureCategory.COMPLETED:
            if self.editorial is None or self.reconciliation is None:
                raise ValueError("COMPLETED scheme result requires Editorial and reconciliation")
            if self.failure_detail is not None:
                raise ValueError("COMPLETED scheme result cannot carry failure detail")
            return self
        if self.status is Stage3FailureCategory.EDITOR_FAILED:
            if self.editorial is not None:
                raise ValueError("EDITOR_FAILED scheme result cannot carry an Editorial report")
            if self.failure_detail is None:
                raise ValueError("EDITOR_FAILED scheme result requires failure detail")
            return self
        if self.status is Stage3FailureCategory.RECONCILIATION_FAILED:
            if self.editorial is None:
                raise ValueError("RECONCILIATION_FAILED requires a collected Editorial report")
            if self.reconciliation is not None:
                raise ValueError("RECONCILIATION_FAILED cannot carry a reconciliation result")
            if self.failure_detail is None:
                raise ValueError("RECONCILIATION_FAILED scheme result requires failure detail")
            return self
        if self.status is not Stage3FailureCategory.EVALUATION_FAILED:
            raise ValueError(f"unsupported scheme status: {self.status.value}")
        scored = {entry.dimension for entry in self.evaluator_scores if entry.score is not None}
        if scored == set(EvaluatorDimension):
            raise ValueError("EVALUATION_FAILED scheme result has complete evaluation")
        if self.editorial is None or self.reconciliation is None:
            raise ValueError("EVALUATION_FAILED requires collected Editor and reconciliation")
        if self.failure_detail is None:
            raise ValueError("EVALUATION_FAILED scheme result requires failure detail")
        return self


class Stage3CaseResult(DomainModel):
    case_id: StableId
    schemes: tuple[Stage3SchemeResult, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_result(self) -> Stage3CaseResult:
        if any(result.case_id != self.case_id for result in self.schemes):
            raise ValueError("scheme result belongs to another case")
        schemes = tuple(result.scheme for result in self.schemes)
        if len(schemes) != len(set(schemes)):
            raise ValueError("case result scheme results must be unique")
        return self


class Stage3RunConfig(DomainModel):
    git_commit: _NonEmptyText
    git_dirty: bool
    writer_model: _NonEmptyText
    generation_parameters: dict[str, object] = Field(default_factory=dict)
    command: _NonEmptyText
    created_at: datetime
    case_directory: _NonEmptyText
    output_directory: _NonEmptyText


class Stage3EvaluationReport(DomainModel):
    """Machine-readable unified evaluation result for aggregation and review."""

    report_id: StableId
    run_config: Stage3RunConfig
    cases: tuple[Stage3CaseResult, ...]

    @model_validator(mode="after")
    def validate_report(self) -> Stage3EvaluationReport:
        case_ids = tuple(result.case_id for result in self.cases)
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("evaluation report case ids must be unique")
        return self


class Stage3SchemeSummary(DomainModel):
    scheme: ContextScheme
    case_count: int = Field(default=0, ge=0)
    completed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    failures: dict[Stage3FailureCategory, int] = Field(default_factory=dict)
    editor_verdicts: dict[EditorialVerdict, int] = Field(default_factory=dict)
    repair_count: int = Field(default=0, ge=0)
    rewrite_count: int = Field(default=0, ge=0)
    reconciliation: dict[ReconciliationVerdict, int] = Field(default_factory=dict)
    rule_passed: int = Field(default=0, ge=0)
    rule_total: int = Field(default=0, ge=0)
    evaluator_scored_dimensions: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)


class Stage3RunSummary(DomainModel):
    report_id: StableId
    case_count: int = Field(default=0, ge=0)
    scheme_count: int = Field(default=0, ge=0)
    completed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    schemes: tuple[Stage3SchemeSummary, ...]
    failure_totals: dict[Stage3FailureCategory, int] = Field(default_factory=dict)
    limitations: tuple[_NonEmptyText, ...] = ()


class HumanScoreEntry(DomainModel):
    case_id: StableId
    scheme: ContextScheme
    dimension: EvaluatorDimension
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    rationale: str = ""
    reviewer_label: str = Field(default="", max_length=240)


__all__ = [
    "CaseInputStatus",
    "CollectedEditorialIssue",
    "CollectedEditorialReport",
    "CollectedReconciliationItem",
    "CollectedReconciliationResult",
    "ContextScheme",
    "EditorialVerdict",
    "EvaluatorDimension",
    "EvaluatorScore",
    "HumanScoreEntry",
    "ReconciliationVerdict",
    "RuleAssessment",
    "RuleCheckKind",
    "RuleCheckResult",
    "Stage3CaseContextInput",
    "Stage3CaseResult",
    "Stage3EvaluationCase",
    "Stage3EvaluationReport",
    "Stage3FailureCategory",
    "Stage3RunConfig",
    "Stage3RunSummary",
    "Stage3SchemeResult",
    "Stage3SchemeSummary",
]
