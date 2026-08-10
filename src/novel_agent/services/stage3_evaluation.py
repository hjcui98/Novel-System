"""Stage 3 generation quality evaluation service (workstream D).

Provides case loading, unified Writer run construction for the three Context
schemes, deterministic rule checks, Editor/reconciliation collection, human
score import, summary aggregation, and Markdown rendering.  The tool never
issues a Stage 3 verdict.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import ValidationError

from novel_agent.domain.generation import (
    WriterArtifactBasis,
    WriterBudget,
    WriterContextHandoffRequest,
    WriterContextItem,
    WriterContextSnapshot,
    WriterExecutionResult,
    WriterInvocation,
    WriterSidecar,
    WriterSourceBinding,
    WriterTerminalStatus,
)
from novel_agent.domain.ids import ArtifactId, ProjectId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.memory import Stage1ContextPackage
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.stage2 import AgentMode, FutureIsolationAttestation
from novel_agent.domain.stage3_evaluation import (
    CaseInputStatus,
    CollectedEditorialReport,
    CollectedReconciliationResult,
    ContextScheme,
    EditorialVerdict,
    EvaluatorDimension,
    EvaluatorScore,
    HumanScoreEntry,
    ReconciliationVerdict,
    RuleAssessment,
    RuleCheckKind,
    RuleCheckResult,
    Stage3EvaluationCase,
    Stage3EvaluationReport,
    Stage3FailureCategory,
    Stage3RunSummary,
    Stage3SchemeResult,
    Stage3SchemeSummary,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.writer_draft_integration import WriterContextHandoffAdapter

_CONTEXT_MEDIA_TYPE = "application/vnd.novel-agent.writer-context-snapshot+json"
_WRITING_MEDIA_TYPE = "application/vnd.novel-agent.writing-task-contract+json"
_PLAN_MEDIA_TYPE = "application/vnd.novel-agent.plan-input+json"
_PROFILE_MEDIA_TYPE = "application/vnd.novel-agent.project-profile-input+json"
_SOURCE_MEDIA_TYPE = "application/vnd.novel-agent.writer-source+json"

_VERBATIM_MIN_CHARACTERS = 8
_TERM_COVERAGE_THRESHOLD = 0.5
_CLAUSE_SEPARATORS = "，。；！？、\n\t:：;；!?"  # noqa: RUF001
_TERM_PATTERN = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]+")


class Stage3EvaluationError(ValueError):
    """Base error for the Stage 3 evaluation tool."""


class CaseLoadError(Stage3EvaluationError):
    """A case file cannot be parsed into a Stage3EvaluationCase."""


class CollectionError(Stage3EvaluationError):
    """A collected Editor or reconciliation file is unreadable or invalid."""


class HumanScoreImportError(Stage3EvaluationError):
    """Human scores reference unknown cases or are malformed."""


@dataclass(frozen=True, slots=True)
class PreparedWriterRun:
    """One comparable Writer call for a scheme; all schemes share one entry."""

    scheme: ContextScheme
    invocation: WriterInvocation
    request: ModelRequest


class IndependentEvaluator(Protocol):
    """Independent evaluator boundary; human review is imported separately."""

    def evaluate(
        self,
        case: Stage3EvaluationCase,
        scheme: ContextScheme,
    ) -> tuple[EvaluatorScore, ...]: ...


def load_case(path: Path) -> Stage3EvaluationCase:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise CaseLoadError(f"cannot read case JSON: {path}") from error
    try:
        return Stage3EvaluationCase.model_validate_json(raw)
    except ValidationError as error:
        raise CaseLoadError(f"invalid evaluation case: {_validation_summary(error)}") from error


def collect_editorial_report(path: Path) -> CollectedEditorialReport | None:
    if not path.exists():
        return None
    return _collect(path, CollectedEditorialReport, "editorial report")


def collect_reconciliation_result(path: Path) -> CollectedReconciliationResult | None:
    if not path.exists():
        return None
    return _collect(path, CollectedReconciliationResult, "reconciliation result")


def _collect[T: CollectedEditorialReport | CollectedReconciliationResult](
    path: Path,
    model: type[T],
    label: str,
) -> T | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CollectionError(f"cannot read {label}: {path}") from error
    try:
        return cast(T, model.model_validate_json(json.dumps(payload, ensure_ascii=False)))
    except ValidationError as error:
        raise CollectionError(f"invalid {label}: {_validation_summary(error)}") from error


def evaluate_rules(
    case: Stage3EvaluationCase,
    scheme: ContextScheme,
    draft_text: str,
    sidecar: WriterSidecar | None = None,
) -> RuleAssessment:
    """Run the deterministic, explainable rule checks over one draft."""

    checks: list[RuleCheckResult] = []
    index = 0
    for constraint in case.writing_task.mandatory_constraints:
        checks.append(
            _reference_check(
                case,
                scheme,
                index,
                RuleCheckKind.MANDATORY_CONSTRAINT_PRESENT,
                constraint,
                draft_text,
            )
        )
        index += 1
    context_input = case.input_for(scheme)
    if context_input is not None and context_input.context_package is not None:
        legacy_context = context_input.context_package
        for unit in (
            *legacy_context.mandatory_constraints,
            *legacy_context.current_world_state,
        ):
            checks.append(
                _reference_check(
                    case,
                    scheme,
                    index,
                    RuleCheckKind.MANDATORY_CONSTRAINT_PRESENT,
                    unit.text,
                    draft_text,
                )
            )
            index += 1
        for unit in legacy_context.active_plan_obligations:
            checks.append(
                _reference_check(
                    case,
                    scheme,
                    index,
                    RuleCheckKind.PLAN_OBLIGATION_PRESENT,
                    unit.text,
                    draft_text,
                )
            )
            index += 1
    elif context_input is not None and context_input.writer_context_package is not None:
        writer_context = context_input.writer_context_package
        for item in (
            *writer_context.continuity_constraints,
            *writer_context.current_world_state,
        ):
            checks.append(
                _reference_check(
                    case,
                    scheme,
                    index,
                    RuleCheckKind.MANDATORY_CONSTRAINT_PRESENT,
                    item.claim,
                    draft_text,
                )
            )
            index += 1
        for item in writer_context.plan_and_obligations:
            checks.append(
                _reference_check(
                    case,
                    scheme,
                    index,
                    RuleCheckKind.PLAN_OBLIGATION_PRESENT,
                    item.claim,
                    draft_text,
                )
            )
            index += 1
    for requirement in case.writing_task.preserve_requirements:
        checks.append(
            _reference_check(
                case,
                scheme,
                index,
                RuleCheckKind.PRESERVE_REQUIREMENT_PRESENT,
                requirement,
                draft_text,
            )
        )
        index += 1
    for reveal in case.writing_task.forbidden_reveals:
        checks.append(
            _reference_check(
                case,
                scheme,
                index,
                RuleCheckKind.FORBIDDEN_REVEAL_ABSENT,
                reveal,
                draft_text,
                negate=True,
            )
        )
        index += 1
    policy = case.writing_task.length_policy
    length = len(draft_text)
    checks.append(
        RuleCheckResult(
            check_id=_check_id(case, scheme, index),
            kind=RuleCheckKind.DRAFT_LENGTH_IN_POLICY,
            passed=policy.minimum_characters <= length <= policy.maximum_characters,
            reference=f"{policy.minimum_characters}..{policy.maximum_characters}",
            detail=f"draft length is {length} characters",
        )
    )
    index += 1
    if sidecar is not None:
        for hint in sidecar.declared_memory_hints:
            checks.append(
                RuleCheckResult(
                    check_id=_check_id(case, scheme, index),
                    kind=RuleCheckKind.DECLARED_HINT_EVIDENCE_PRESENT,
                    passed=hint.evidence_quote in draft_text,
                    reference=hint.evidence_quote,
                    detail="declared hint evidence quote appears in draft"
                    if hint.evidence_quote in draft_text
                    else "declared hint evidence quote not found in draft",
                )
            )
            index += 1
    return RuleAssessment(checks=tuple(checks))


def _reference_check(
    case: Stage3EvaluationCase,
    scheme: ContextScheme,
    index: int,
    kind: RuleCheckKind,
    reference: str,
    draft_text: str,
    *,
    negate: bool = False,
) -> RuleCheckResult:
    matched = _matched(reference, draft_text)
    passed = not matched if negate else matched
    detail = (
        "reference found in draft (verbatim clause or term coverage)"
        if matched
        else "reference not found in draft (verbatim clause or term coverage)"
    )
    return RuleCheckResult(
        check_id=_check_id(case, scheme, index),
        kind=kind,
        passed=passed,
        reference=reference,
        detail=detail,
    )


def _check_id(case: Stage3EvaluationCase, scheme: ContextScheme, index: int) -> StableId:
    return StableId(f"check.{case.case_id.root}.{scheme.value}.{index}")


def _matched(reference: str, draft_text: str) -> bool:
    for clause in _clauses(reference):
        if len(clause) >= _VERBATIM_MIN_CHARACTERS and clause in draft_text:
            return True
    terms = _content_terms(reference)
    if not terms:
        return False
    covered = sum(1 for term in terms if term in draft_text)
    return covered / len(terms) >= _TERM_COVERAGE_THRESHOLD


def _clauses(text: str) -> tuple[str, ...]:
    return tuple(
        clause.strip()
        for clause in re.split(f"[{re.escape(_CLAUSE_SEPARATORS)}]", text)
        if clause.strip()
    )


def _content_terms(text: str) -> tuple[str, ...]:
    """Character bigrams of the reference with punctuation removed.

    Bigrams are a documented approximation for Chinese key-phrase coverage;
    the rules never decide a scheme verdict by themselves.
    """

    compact = "".join(_TERM_PATTERN.findall(text))
    terms: list[str] = []
    for index in range(len(compact) - 1):
        term = compact[index : index + 2]
        if term not in terms:
            terms.append(term)
    return tuple(terms)


def evaluation_complete(scores: Sequence[EvaluatorScore]) -> bool:
    scored = {entry.dimension for entry in scores if entry.score is not None}
    return scored == set(EvaluatorDimension)


def merge_evaluator_scores(
    scripted: Sequence[EvaluatorScore],
    human: Sequence[EvaluatorScore],
) -> tuple[EvaluatorScore, ...]:
    """Merge scores; human entries replace scripted scores on the same key."""

    human_keys = {(entry.case_id, entry.scheme, entry.dimension) for entry in human}
    merged = list(human)
    merged.extend(
        entry
        for entry in scripted
        if (entry.case_id, entry.scheme, entry.dimension) not in human_keys
    )
    return tuple(merged)


def assemble_scheme_result(
    *,
    case: Stage3EvaluationCase,
    scheme: ContextScheme,
    writer: WriterExecutionResult | None,
    editorial: CollectedEditorialReport | None,
    reconciliation: CollectedReconciliationResult | None,
    rules: RuleAssessment | None = None,
    evaluator_scores: Sequence[EvaluatorScore] = (),
    evaluation_required: bool = False,
) -> Stage3SchemeResult:
    """Build one uniform scheme result with typed failure attribution."""

    if writer is None or writer.status is not WriterTerminalStatus.COMPLETED:
        if writer is None:
            return Stage3SchemeResult(
                case_id=case.case_id,
                scheme=scheme,
                status=Stage3FailureCategory.INPUT_NOT_READY,
                failure_detail="scheme context input missing or not ready",
            )
        return Stage3SchemeResult(
            case_id=case.case_id,
            scheme=scheme,
            status=Stage3FailureCategory.WRITER_FAILED,
            writer=writer,
            failure_detail=_writer_failure_detail(writer),
        )
    draft = writer.draft
    if draft is None:
        raise Stage3EvaluationError("completed Writer result is missing its draft")
    resolved_scores = tuple(evaluator_scores)
    draft_id = draft.draft_id.root
    if editorial is not None and editorial.draft_id != draft_id:
        return Stage3SchemeResult(
            case_id=case.case_id,
            scheme=scheme,
            status=Stage3FailureCategory.EDITOR_FAILED,
            writer=writer,
            draft=draft,
            rules=rules,
            failure_detail="EditorialReport belongs to another Draft",
        )
    if editorial is None:
        return Stage3SchemeResult(
            case_id=case.case_id,
            scheme=scheme,
            status=Stage3FailureCategory.EDITOR_FAILED,
            writer=writer,
            draft=draft,
            rules=rules,
            failure_detail="EditorialReport missing",
        )
    if reconciliation is not None and reconciliation.draft_id != draft_id:
        return Stage3SchemeResult(
            case_id=case.case_id,
            scheme=scheme,
            status=Stage3FailureCategory.RECONCILIATION_FAILED,
            writer=writer,
            draft=draft,
            editorial=editorial,
            rules=rules,
            failure_detail="ReconciliationResult belongs to another Draft",
        )
    if reconciliation is None:
        return Stage3SchemeResult(
            case_id=case.case_id,
            scheme=scheme,
            status=Stage3FailureCategory.RECONCILIATION_FAILED,
            writer=writer,
            draft=draft,
            editorial=editorial,
            rules=rules,
            failure_detail="ReconciliationResult missing",
        )
    if evaluation_required and not evaluation_complete(resolved_scores):
        return Stage3SchemeResult(
            case_id=case.case_id,
            scheme=scheme,
            status=Stage3FailureCategory.EVALUATION_FAILED,
            writer=writer,
            draft=draft,
            editorial=editorial,
            reconciliation=reconciliation,
            rules=rules,
            evaluator_scores=resolved_scores,
            failure_detail="independent evaluation or human scores missing",
        )
    return Stage3SchemeResult(
        case_id=case.case_id,
        scheme=scheme,
        status=Stage3FailureCategory.COMPLETED,
        writer=writer,
        draft=draft,
        editorial=editorial,
        reconciliation=reconciliation,
        rules=rules,
        evaluator_scores=resolved_scores,
    )


def _writer_failure_detail(writer: WriterExecutionResult) -> str:
    parts = [writer.status.value]
    if writer.failure_code is not None:
        parts.append(writer.failure_code.value)
    if writer.failure_detail:
        parts.append(writer.failure_detail[:512])
    return "; ".join(parts)


def import_human_scores(
    path: Path,
    known_case_ids: Collection[StableId],
) -> tuple[HumanScoreEntry, ...]:
    """Import human scores; unknown cases and malformed entries are rejected."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HumanScoreImportError(f"cannot read human scores: {path}") from error
    if not isinstance(payload, list):
        raise HumanScoreImportError("human scores must be a JSON list of entries")
    entries: list[HumanScoreEntry] = []
    for item in payload:
        if not isinstance(item, dict):
            raise HumanScoreImportError("human score entry must be a JSON object")
        try:
            entry = HumanScoreEntry.model_validate_json(json.dumps(item, ensure_ascii=False))
        except ValidationError as error:
            raise HumanScoreImportError(
                f"invalid human score entry: {_validation_summary(error)}"
            ) from error
        if entry.case_id not in known_case_ids:
            raise HumanScoreImportError(
                f"human score references unknown case: {entry.case_id.root}"
            )
        if entry.score is not None:
            entries.append(entry)
    return tuple(entries)


def to_evaluator_scores(entries: Sequence[HumanScoreEntry]) -> tuple[EvaluatorScore, ...]:
    return tuple(
        EvaluatorScore(
            case_id=entry.case_id,
            scheme=entry.scheme,
            dimension=entry.dimension,
            score=entry.score,
            rationale=entry.rationale,
            source="human",
        )
        for entry in entries
    )


def export_human_scoring_package(
    cases: Sequence[Stage3EvaluationCase],
    output_directory: Path,
    drafts: dict[StableId, dict[ContextScheme, str]],
) -> tuple[Path, Path, Path]:
    """Export the blind human scoring package consumed by import_human_scores."""

    output_directory.mkdir(parents=True, exist_ok=True)
    entries = [
        {
            "case_id": case.case_id.root,
            "scheme": scheme.value,
            "dimension": dimension.value,
            "score": None,
            "rationale": "",
            "reviewer_label": "",
        }
        for case in cases
        for scheme in ContextScheme
        if case.input_for(scheme) is not None
        for dimension in EvaluatorDimension
    ]
    scores_path = output_directory / "human_scores.json"
    scores_path.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    instructions_path = output_directory / "instructions.json"
    instructions_path.write_text(
        json.dumps(
            {
                case.case_id.root: {
                    "writing_task": case.writing_task.model_dump(mode="json"),
                    "evaluator_instructions": list(case.evaluator_instructions),
                }
                for case in cases
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    drafts_path = output_directory / "drafts.json"
    drafts_path.write_text(
        json.dumps(
            {
                case_id.root: {scheme.value: text for scheme, text in scheme_drafts.items()}
                for case_id, scheme_drafts in drafts.items()
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return scores_path, instructions_path, drafts_path


class ScriptedEvaluator:
    """Deterministic scripted evaluator reading per-case scores from JSON."""

    def __init__(self, scores_path: Path) -> None:
        try:
            payload = json.loads(scores_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise Stage3EvaluationError(
                f"cannot read scripted evaluator scores: {scores_path}"
            ) from error
        if not isinstance(payload, dict):
            raise Stage3EvaluationError("scripted evaluator scores must be a JSON object")
        self._scores = payload

    def evaluate(
        self,
        case: Stage3EvaluationCase,
        scheme: ContextScheme,
    ) -> tuple[EvaluatorScore, ...]:
        case_scores = self._scores.get(case.case_id.root)
        if not isinstance(case_scores, dict):
            return ()
        scheme_scores = case_scores.get(scheme.value)
        if not isinstance(scheme_scores, dict):
            return ()
        scores: list[EvaluatorScore] = []
        for dimension in EvaluatorDimension:
            value = scheme_scores.get(dimension.value)
            if not isinstance(value, dict) or "score" not in value:
                continue
            score_value = value["score"]
            if score_value is not None and not isinstance(score_value, (int, float)):
                continue
            scores.append(
                EvaluatorScore(
                    case_id=case.case_id,
                    scheme=scheme,
                    dimension=dimension,
                    score=None if score_value is None else float(score_value),
                    rationale=str(value.get("rationale", "")),
                )
            )
        return tuple(scores)


class Stage3WriterRunBuilder:
    """Unified Writer invocation entry for all three Context schemes."""

    def __init__(
        self,
        *,
        artifacts: ArtifactRepository,
        schema_version: SchemaVersion,
        project_id: ProjectId,
        run_id: RunId,
        writer_configuration_fingerprint: ArtifactId,
        model_configuration_fingerprint: ArtifactId,
        attestation: FutureIsolationAttestation,
        source_binding: WriterSourceBinding,
        plan_payload: bytes,
        profile_payload: bytes,
        source_payload: bytes,
        budget: WriterBudget,
    ) -> None:
        self._artifacts = artifacts
        self._schema_version = schema_version
        self._project_id = project_id
        self._run_id = run_id
        self._writer_configuration_fingerprint = writer_configuration_fingerprint
        self._model_configuration_fingerprint = model_configuration_fingerprint
        self._attestation = attestation
        self._source_binding = source_binding
        self._budget = budget
        self._plan_artifact = artifacts.put(plan_payload, _PLAN_MEDIA_TYPE, schema_version)
        self._profile_artifact = artifacts.put(
            profile_payload,
            _PROFILE_MEDIA_TYPE,
            schema_version,
        )
        self._source_artifact = artifacts.put(
            source_payload,
            _SOURCE_MEDIA_TYPE,
            schema_version,
        )

    def prepare(self, case: Stage3EvaluationCase) -> tuple[PreparedWriterRun, ...]:
        """Build comparable Writer runs for every READY scheme input."""

        writing_artifact = self._artifacts.put(
            canonical_json_bytes(case.writing_task.model_dump(mode="json")),
            _WRITING_MEDIA_TYPE,
            self._schema_version,
        )
        runs: list[PreparedWriterRun] = []
        for scheme_input in case.inputs:
            if scheme_input.input_status is not CaseInputStatus.READY:
                continue
            if scheme_input.context_package is None and scheme_input.writer_context_package is None:
                raise Stage3EvaluationError(
                    f"READY scheme input {scheme_input.scheme.value} lacks a context package"
                )
            if scheme_input.writer_context_package is not None:
                package = scheme_input.writer_context_package
                handoff = WriterContextHandoffAdapter(
                    self._artifacts,
                    self._schema_version,
                ).adapt(
                    WriterContextHandoffRequest(
                        integration_id=StableId(
                            f"evaluation-handoff.{case.case_id.root}.{scheme_input.scheme.value}"
                        ),
                        run_id=self._run_id,
                        task_id=TaskId(package.task_contract.task_id.root),
                        project_id=self._project_id,
                        context_package=package,
                        writing_task=case.writing_task,
                        plan_artifact=self._plan_artifact,
                        project_profile_artifact=self._profile_artifact,
                        future_isolation_attestation=self._attestation,
                        writer_configuration_fingerprint=(self._writer_configuration_fingerprint),
                        model_configuration_fingerprint=(self._model_configuration_fingerprint),
                        source_artifacts=(self._source_binding,),
                        budget=self._budget,
                    )
                )
                invocation = handoff.invocation
                request = ModelRequest(
                    request_id=StableId(f"request.{case.case_id.root}.{scheme_input.scheme.value}"),
                    run_id=self._run_id,
                    task_id=invocation.task_id,
                    model_role=ModelRole.BATCH_TEST,
                    purpose=ModelCallPurpose.BATCH_TEST,
                    trace_id=f"trace.{case.case_id.root}.{scheme_input.scheme.value}",
                    prompt="replaced by sealed WriterAgent",
                    timeout_seconds=self._budget.timeout_seconds,
                )
                runs.append(
                    PreparedWriterRun(
                        scheme=scheme_input.scheme,
                        invocation=invocation,
                        request=request,
                    )
                )
                continue
            assert scheme_input.context_package is not None
            context = _writer_snapshot(scheme_input.context_package)
            context_artifact = self._artifacts.put(
                canonical_json_bytes(context.model_dump(mode="json")),
                _CONTEXT_MEDIA_TYPE,
                self._schema_version,
            )
            basis = WriterArtifactBasis(
                project_id=self._project_id,
                base_commit=context.base_commit,
                snapshot_id=context.snapshot_id,
                context_id=context.context_id,
                context_artifact=context_artifact,
                context_fingerprint=context_artifact.artifact_id,
                writing_contract_artifact=writing_artifact,
                plan_artifact=self._plan_artifact,
                project_profile_artifact=self._profile_artifact,
                configuration_fingerprint=self._writer_configuration_fingerprint,
                model_configuration_fingerprint=self._model_configuration_fingerprint,
                future_isolation_attestation=self._attestation,
                source_artifacts=(self._source_binding,),
            )
            invocation = WriterInvocation(
                invocation_id=StableId(
                    f"invocation.{case.case_id.root}.{scheme_input.scheme.value}"
                ),
                run_id=self._run_id,
                task_id=TaskId(f"task.{case.case_id.root}.{scheme_input.scheme.value}"),
                mode=AgentMode.DRAFT,
                basis=basis,
                writing_task=case.writing_task,
                context_package=context,
                input_artifacts=(
                    context_artifact,
                    writing_artifact,
                    self._plan_artifact,
                    self._profile_artifact,
                    self._source_artifact,
                ),
                budget=self._budget,
            )
            request = ModelRequest(
                request_id=StableId(f"request.{case.case_id.root}.{scheme_input.scheme.value}"),
                run_id=self._run_id,
                task_id=TaskId(f"task.{case.case_id.root}.{scheme_input.scheme.value}"),
                model_role=ModelRole.BATCH_TEST,
                purpose=ModelCallPurpose.BATCH_TEST,
                trace_id=f"trace.{case.case_id.root}.{scheme_input.scheme.value}",
                prompt="replaced by sealed WriterAgent",
                timeout_seconds=self._budget.timeout_seconds,
            )
            runs.append(
                PreparedWriterRun(
                    scheme=scheme_input.scheme,
                    invocation=invocation,
                    request=request,
                )
            )
        return tuple(runs)


def _writer_snapshot(context: Stage1ContextPackage) -> WriterContextSnapshot:
    """Project an evaluation fixture into the current Writing Core input contract."""

    sections = (
        ("mandatory_constraints", context.mandatory_constraints),
        ("current_world_state", context.current_world_state),
        ("active_plan_obligations", context.active_plan_obligations),
        ("relevant_historical_events", context.relevant_historical_events),
        ("truth_and_knowledge_boundaries", context.truth_and_knowledge_boundaries),
        ("style_or_reference_optional", context.style_or_reference_optional),
    )
    items = tuple(
        WriterContextItem(
            item_id=unit.unit_id,
            category=category,
            text=unit.text,
            source_commit=unit.source_commit,
            snapshot_id=unit.snapshot_id,
            access_scope="writer_safe",
            information_label=unit.information_label,
            derivation_taint=unit.derivation_taint,
            entity_ids=unit.entity_ids,
            predicate=unit.predicate,
            narrative_start=unit.narrative_start,
            narrative_end=unit.narrative_end,
            truth_class=unit.truth_class.value if unit.truth_class is not None else None,
            support_status=unit.support_status,
            mandatory=unit.mandatory,
        )
        for category, units in sections
        for unit in units
    )
    return WriterContextSnapshot(
        context_id=context.context_id,
        base_commit=context.base_commit,
        snapshot_id=context.snapshot_id,
        task_contract=context.task_contract,
        items=items,
        unresolved_gaps=context.unresolved_gaps,
        budget_report={
            "token_budget": context.budget_report.token_budget,
            "mandatory_tokens": context.budget_report.mandatory_tokens,
            "optional_tokens": context.budget_report.optional_tokens,
        },
    )


@dataclass(slots=True)
class _SchemeAggregates:
    case_count: int = 0
    completed: int = 0
    failed: int = 0
    failures: dict[Stage3FailureCategory, int] = field(default_factory=dict)
    editor_verdicts: dict[EditorialVerdict, int] = field(default_factory=dict)
    repair_count: int = 0
    rewrite_count: int = 0
    reconciliation: dict[ReconciliationVerdict, int] = field(default_factory=dict)
    rule_passed: int = 0
    rule_total: int = 0
    evaluator_scored_dimensions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


def build_summary(
    report: Stage3EvaluationReport,
    *,
    limitations: tuple[str, ...] = (),
) -> Stage3RunSummary:
    """Aggregate scheme results into one explainable run summary."""

    aggregates: dict[ContextScheme, _SchemeAggregates] = {
        scheme: _SchemeAggregates() for scheme in ContextScheme
    }
    failure_totals: dict[Stage3FailureCategory, int] = {}
    for case_result in report.cases:
        for scheme_result in case_result.schemes:
            aggregate = aggregates[scheme_result.scheme]
            aggregate.case_count += 1
            if scheme_result.status is Stage3FailureCategory.COMPLETED:
                aggregate.completed += 1
                assert scheme_result.editorial is not None
                assert scheme_result.reconciliation is not None
                assert scheme_result.writer is not None
                editorial = scheme_result.editorial
                aggregate.editor_verdicts[editorial.verdict] = (
                    aggregate.editor_verdicts.get(editorial.verdict, 0) + 1
                )
                aggregate.repair_count += editorial.repair_count
                aggregate.rewrite_count += editorial.rewrite_count
                for item in scheme_result.reconciliation.items:
                    aggregate.reconciliation[item.verdict] = (
                        aggregate.reconciliation.get(item.verdict, 0) + 1
                    )
                if scheme_result.rules is not None:
                    aggregate.rule_passed += scheme_result.rules.passed_count
                    aggregate.rule_total += len(scheme_result.rules.checks)
                aggregate.evaluator_scored_dimensions += sum(
                    1 for score in scheme_result.evaluator_scores if score.score is not None
                )
                metrics = scheme_result.writer.metrics
                aggregate.input_tokens += metrics.input_tokens
                aggregate.output_tokens += metrics.output_tokens
                aggregate.latency_ms += metrics.latency_ms
            else:
                aggregate.failed += 1
                aggregate.failures[scheme_result.status] = (
                    aggregate.failures.get(scheme_result.status, 0) + 1
                )
            failure_totals[scheme_result.status] = failure_totals.get(scheme_result.status, 0) + 1

    scheme_summaries = tuple(
        Stage3SchemeSummary(
            scheme=scheme,
            case_count=aggregate.case_count,
            completed=aggregate.completed,
            failed=aggregate.failed,
            failures=dict(aggregate.failures),
            editor_verdicts=dict(aggregate.editor_verdicts),
            repair_count=aggregate.repair_count,
            rewrite_count=aggregate.rewrite_count,
            reconciliation=dict(aggregate.reconciliation),
            rule_passed=aggregate.rule_passed,
            rule_total=aggregate.rule_total,
            evaluator_scored_dimensions=aggregate.evaluator_scored_dimensions,
            input_tokens=aggregate.input_tokens,
            output_tokens=aggregate.output_tokens,
            latency_ms=aggregate.latency_ms,
        )
        for scheme, aggregate in aggregates.items()
    )
    non_completed_totals: dict[Stage3FailureCategory, int] = {
        category: count
        for category, count in failure_totals.items()
        if category is not Stage3FailureCategory.COMPLETED
    }
    completed_total = sum(item.completed for item in scheme_summaries)
    failed_total = sum(item.failed for item in scheme_summaries)
    return Stage3RunSummary(
        report_id=report.report_id,
        case_count=len(report.cases),
        scheme_count=sum(item.case_count for item in scheme_summaries),
        completed=completed_total,
        failed=failed_total,
        schemes=scheme_summaries,
        failure_totals=non_completed_totals,
        limitations=limitations,
    )


def render_summary_markdown(summary: Stage3RunSummary, report: Stage3EvaluationReport) -> str:
    """Render the human-readable two-layer summary report."""

    lines = [
        "# Stage 3 Generation Evaluation Summary",
        "",
        "- report_id: " + summary.report_id.root,
        "- created_at: " + report.run_config.created_at.isoformat(),
        "- git_commit: "
        + report.run_config.git_commit
        + (" (dirty)" if report.run_config.git_dirty else ""),
        "- writer_model: " + report.run_config.writer_model,
        "- command: `" + report.run_config.command + "`",
        "- case_directory: " + report.run_config.case_directory,
        "- output_directory: " + report.run_config.output_directory,
        "",
        "## Totals",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| cases | {summary.case_count} |",
        f"| schemes | {summary.scheme_count} |",
        f"| completed | {summary.completed} |",
        f"| failed | {summary.failed} |",
        "",
        "## Failure breakdown",
        "",
    ]
    if summary.failure_totals:
        lines.append("| Category | Count |")
        lines.append("|---|---:|")
        for category in Stage3FailureCategory:
            count = summary.failure_totals.get(category)
            if count is not None:
                lines.append(f"| {category.value} | {count} |")
    else:
        lines.append("No failures.")
    lines.extend(
        [
            "",
            "## Per-scheme results",
            "",
            "| scheme | cases | completed | failed | rule pass | editor verdicts |",
            "| repairs | rewrites | reconciliation | evaluator dims | input | output | latency |",
            "|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for scheme_summary in summary.schemes:
        verdicts = _count_label(scheme_summary.editor_verdicts)
        reconciliation = _count_label(scheme_summary.reconciliation)
        rule_pass = (
            f"{scheme_summary.rule_passed}/{scheme_summary.rule_total}"
            if scheme_summary.rule_total
            else "-"
        )
        lines.append(
            "| "
            + " | ".join(
                (
                    scheme_summary.scheme.value,
                    str(scheme_summary.case_count),
                    str(scheme_summary.completed),
                    str(scheme_summary.failed),
                    rule_pass,
                    verdicts,
                    str(scheme_summary.repair_count),
                    str(scheme_summary.rewrite_count),
                    reconciliation,
                    str(scheme_summary.evaluator_scored_dimensions),
                    str(scheme_summary.input_tokens),
                    str(scheme_summary.output_tokens),
                    str(scheme_summary.latency_ms),
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Per-case detail",
            "",
            "| case | scheme | status | editorial | rule pass | failure |",
            "|---|---|---|---:|---:|---|",
        ]
    )
    for case_result in report.cases:
        for scheme_result in case_result.schemes:
            editorial = (
                scheme_result.editorial.verdict.value
                if scheme_result.editorial is not None
                else "-"
            )
            rule_pass = "-"
            if scheme_result.rules is not None:
                rule_pass = f"{scheme_result.rules.passed_count}/{len(scheme_result.rules.checks)}"
            failure = scheme_result.failure_detail or "-"
            lines.append(
                "| "
                + " | ".join(
                    (
                        case_result.case_id.root,
                        scheme_result.scheme.value,
                        scheme_result.status.value,
                        editorial,
                        rule_pass,
                        failure.replace("|", "/"),
                    )
                )
                + " |"
            )
    lines.extend(["", "## Limitations", ""])
    if summary.limitations:
        lines.extend(f"- {item}" for item in summary.limitations)
    else:
        lines.append("- none declared")
    return "\n".join(lines) + "\n"


def _count_label(counts: Mapping[Any, int]) -> str:
    if not counts:
        return "-"
    return "/".join(f"{key}:{value}" for key, value in counts.items())


def _validation_summary(error: ValidationError) -> str:
    return json.dumps(
        error.errors(include_url=False, include_input=False),
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )[:4096]


__all__ = [
    "CaseLoadError",
    "CollectionError",
    "HumanScoreImportError",
    "IndependentEvaluator",
    "PreparedWriterRun",
    "ScriptedEvaluator",
    "Stage3EvaluationError",
    "Stage3WriterRunBuilder",
    "assemble_scheme_result",
    "build_summary",
    "collect_editorial_report",
    "collect_reconciliation_result",
    "evaluate_rules",
    "evaluation_complete",
    "export_human_scoring_package",
    "import_human_scores",
    "load_case",
    "merge_evaluator_scores",
    "render_summary_markdown",
    "to_evaluator_scores",
]
