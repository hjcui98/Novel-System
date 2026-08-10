from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.generation import (
    DraftArtifact,
    WriterArtifactBasis,
    WriterBudget,
    WriterContextSnapshot,
    WriterDraftPayload,
    WriterExecutionMetrics,
    WriterExecutionResult,
    WriterFailureCode,
    WriterInvocation,
    WriterRuntimeFingerprints,
    WriterTerminalStatus,
    WritingLengthPolicy,
    WritingTaskContract,
)
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import ContextBudgetReport, Stage1ContextPackage
from novel_agent.domain.model_calls import (
    ModelCallPurpose,
    ModelCallRecord,
    ModelRole,
    ModelUsage,
)
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentType,
    ContractRef,
    ExecutionStatus,
    FutureIsolationAttestation,
)
from novel_agent.domain.stage3_evaluation import (
    CaseInputStatus,
    CollectedEditorialIssue,
    CollectedEditorialReport,
    CollectedReconciliationItem,
    CollectedReconciliationResult,
    ContextScheme,
    EditorialVerdict,
    EvaluatorDimension,
    EvaluatorScore,
    ReconciliationVerdict,
    RuleAssessment,
    RuleCheckKind,
    RuleCheckResult,
    Stage3CaseContextInput,
    Stage3CaseResult,
    Stage3EvaluationCase,
    Stage3EvaluationReport,
    Stage3FailureCategory,
    Stage3RunConfig,
    Stage3RunSummary,
    Stage3SchemeResult,
    Stage3SchemeSummary,
)
from novel_agent.domain.writer_context import WriterContextPackage

NOW = datetime(2026, 7, 31, tzinfo=UTC)
VERSION = SchemaVersion("1.0.0")
CASE_FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "stage3_evaluation"
    / "cases"
    / "enter_tower"
    / "case.json"
)


def _hash(digit: str) -> ArtifactId:
    return ArtifactId("sha256:" + digit * 64)


def _stable(value: str) -> StableId:
    return StableId(value)


def _context_package(context_id: str = "context.test") -> Stage1ContextPackage:
    return Stage1ContextPackage(
        context_id=_stable(context_id),
        base_commit=CommitId(_hash("a").root),
        snapshot_id=_stable("snapshot.test"),
        task_contract="task.test",
        budget_report=ContextBudgetReport(
            token_budget=100,
            mandatory_tokens=0,
            optional_tokens=0,
            full_chapter_read_count=0,
        ),
    )


def _context_snapshot(context_id: str = "context.test") -> WriterContextSnapshot:
    return WriterContextSnapshot(
        context_id=_stable(context_id),
        base_commit=CommitId(_hash("a").root),
        snapshot_id=_stable("snapshot.test"),
        task_contract="writing-contract.test",
        budget_report={"token_budget": 100},
    )


def _writer_context_package() -> WriterContextPackage:
    payload = json.loads(CASE_FIXTURE.read_text(encoding="utf-8"))
    return WriterContextPackage.model_validate_json(
        json.dumps(payload["inputs"][2]["writer_context_package"], ensure_ascii=False)
    )


def _case_input(
    scheme: ContextScheme,
    *,
    ready: bool = True,
) -> Stage3CaseContextInput:
    if not ready:
        return Stage3CaseContextInput(scheme=scheme, input_status=CaseInputStatus.MISSING)
    if scheme is ContextScheme.WRITER_CONTEXT_PACKAGE:
        return Stage3CaseContextInput(
            scheme=scheme,
            input_status=CaseInputStatus.READY,
            writer_context_package=_writer_context_package(),
            entry="fixture",
        )
    return Stage3CaseContextInput(
        scheme=scheme,
        input_status=CaseInputStatus.READY,
        context_package=_context_package(f"context.{scheme.value}"),
        entry="fixture",
    )


def _task() -> WritingTaskContract:
    return WritingTaskContract(
        contract_id=_stable("writing-contract.test"),
        target_chapter=21,
        target_scenes=(_stable("scene.test.21.1"),),
        pov="LIN-CHE",
        narrative_person="THIRD-PERSON",
        chapter_goal="TEST GOAL",
        mandatory_constraints=("CONSTRAINT-ALPHA, KEEP ME.",),
        forbidden_reveals=("REVEAL-SECRET",),
        preserve_requirements=("STAY-CAUTIOUS",),
        length_policy=WritingLengthPolicy(
            minimum_characters=1,
            target_characters=10,
            maximum_characters=1000,
        ),
    )


def _case(**updates: Any) -> Stage3EvaluationCase:
    values: dict[str, Any] = {
        "case_id": _stable("case.test"),
        "writing_task": _task(),
        "inputs": tuple(_case_input(scheme) for scheme in ContextScheme),
        "evaluator_instructions": ("SCORE FROM THE PROSE ONLY",),
    }
    values.update(updates)
    return Stage3EvaluationCase(**values)


def _artifact(digit: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=_hash(digit),
        media_type="application/json",
        byte_length=1,
        schema_version=VERSION,
    )


def _basis() -> WriterArtifactBasis:
    attestation = FutureIsolationAttestation(
        attestation_id=_stable("attestation.test"),
        checkpoint_chapter=20,
        canonical_source_ids=(),
        evaluator_only_source_ids=(),
        passed=True,
        configuration_fingerprint=_hash("9"),
    )
    return WriterArtifactBasis(
        project_id=ProjectId("project.test"),
        base_commit=CommitId(_hash("a").root),
        snapshot_id=_stable("snapshot.test"),
        context_id=_stable("context.test"),
        context_artifact=_artifact("1"),
        context_fingerprint=_hash("1"),
        writing_contract_artifact=_artifact("2"),
        plan_artifact=_artifact("3"),
        project_profile_artifact=_artifact("4"),
        configuration_fingerprint=_hash("8"),
        model_configuration_fingerprint=_hash("7"),
        future_isolation_attestation=attestation,
    )


def _invocation() -> WriterInvocation:
    basis = _basis()
    return WriterInvocation(
        invocation_id=_stable("invocation.test"),
        run_id=RunId("run.test"),
        task_id=TaskId("task.test"),
        mode=AgentMode.DRAFT,
        basis=basis,
        writing_task=_task(),
        context_package=_context_snapshot(),
        input_artifacts=(
            basis.context_artifact,
            basis.writing_contract_artifact,
            basis.plan_artifact,
            basis.project_profile_artifact,
        ),
        budget=WriterBudget(
            max_model_calls=1,
            input_token_limit=1000,
            output_token_limit=1000,
        ),
    )


def _writer_result(
    *,
    completed: bool = True,
    failure: WriterTerminalStatus = WriterTerminalStatus.MODEL_UNAVAILABLE,
) -> WriterExecutionResult:
    basis = _basis()
    invocation = _invocation()
    run_id = RunId("run.test")
    task_id = TaskId("task.test")
    fingerprints = WriterRuntimeFingerprints(
        agent_spec_fingerprint=_hash("2"),
        prompt_fingerprint=_hash("3"),
        skill_fingerprints=(_hash("4"),),
        tool_policy_fingerprint=_hash("5"),
        configuration_fingerprint=basis.configuration_fingerprint,
        model_configuration_fingerprint=basis.model_configuration_fingerprint,
    )
    metrics = WriterExecutionMetrics(
        model_called=True,
        model_call_count=1,
        input_tokens=10,
        output_tokens=5,
        cost_usd=Decimal("0"),
        latency_ms=3,
    )
    if not completed:
        return WriterExecutionResult(
            result_id=_stable("result.test"),
            invocation_id=invocation.invocation_id,
            run_id=run_id,
            task_id=task_id,
            status=failure,
            basis=basis,
            fingerprints=fingerprints,
            metrics=metrics,
            retry_safe=False,
            failure_code=WriterFailureCode(failure.value),
            failure_detail="injected writer failure",
        )
    WriterDraftPayload(
        draft_text="CONSTRAINT-ALPHA, KEEP ME. This is a long enough draft body.",
        declared_memory_hints=(),
    )
    call = ModelCallRecord(
        request_id=_stable("request.test"),
        run_id=run_id,
        task_id=task_id,
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id="trace-test",
        endpoint="fake",
        model="fake",
        model_version="v1",
        usage=ModelUsage(input_tokens=10, output_tokens=5, cost_usd=Decimal("0")),
        latency_ms=3,
        started_at=NOW,
        completed_at=NOW,
    )
    output_artifacts = (_artifact("a"), _artifact("b"), _artifact("c"))
    receipt = AgentExecutionReceipt(
        receipt_id=_stable("receipt.test"),
        run_id=run_id,
        task_id=task_id,
        agent_spec=ContractRef(
            contract_id=_stable("spec.test"),
            version=VERSION,
            content_hash=_hash("e"),
        ),
        agent_type=AgentType.WRITER,
        agent_mode=AgentMode.DRAFT,
        prompt_fingerprint=_hash("f"),
        configuration_fingerprint=basis.configuration_fingerprint,
        base_commit=basis.base_commit,
        input_artifacts=invocation.input_artifacts,
        output_artifacts=output_artifacts,
        model_call_ids=(call.request_id,),
        status=ExecutionStatus.SUCCEEDED,
        started_at=NOW,
        completed_at=NOW,
        latency_ms=3,
    )
    draft = DraftArtifact(
        draft_id=_hash("d"),
        mode=AgentMode.DRAFT,
        basis=basis,
        text_artifact=output_artifacts[0],
        sidecar_artifact=output_artifacts[1],
        raw_output_artifact=output_artifacts[2],
        writer_receipt=receipt,
        model_call_ids=(call.request_id,),
        created_at=NOW,
    )
    return WriterExecutionResult(
        result_id=_stable("result.test"),
        invocation_id=invocation.invocation_id,
        run_id=run_id,
        task_id=task_id,
        status=WriterTerminalStatus.COMPLETED,
        basis=basis,
        draft=draft,
        receipt=receipt,
        artifacts=output_artifacts,
        fingerprints=fingerprints,
        metrics=metrics,
        retry_safe=True,
    )


def _editorial(verdict: EditorialVerdict = EditorialVerdict.PASS) -> CollectedEditorialReport:
    return CollectedEditorialReport(
        report_id="editorial.test",
        draft_id=_hash("d").root,
        verdict=verdict,
        issues=(
            CollectedEditorialIssue(
                issue_type="continuity",
                severity="high",
                location="scene.21.1",
                description="conflict",
            ),
        ),
        repair_count=1 if verdict is EditorialVerdict.LOCAL_REPAIR else 0,
        rewrite_count=1 if verdict is EditorialVerdict.MAJOR_REWRITE else 0,
    )


def _reconciliation() -> CollectedReconciliationResult:
    return CollectedReconciliationResult(
        result_id="reconciliation.test",
        draft_id=_hash("d").root,
        items=(
            CollectedReconciliationItem(
                verdict=ReconciliationVerdict.MATCHED,
                subject="GATE",
            ),
        ),
    )


def _rule_check(passed: bool) -> RuleCheckResult:
    return RuleCheckResult(
        check_id=_stable("check.test"),
        kind=RuleCheckKind.FORBIDDEN_REVEAL_ABSENT,
        passed=passed,
        reference="REVEAL-SECRET",
        detail="detail",
    )


class TestCaseInputContracts:
    def test_ready_input_requires_context_package(self) -> None:
        with pytest.raises(ValidationError, match="requires a context package"):
            Stage3CaseContextInput(
                scheme=ContextScheme.RECENT_PROSE,
                input_status=CaseInputStatus.READY,
            )

    def test_missing_input_rejects_context_package(self) -> None:
        with pytest.raises(ValidationError, match="cannot carry a context package"):
            Stage3CaseContextInput(
                scheme=ContextScheme.RECENT_PROSE,
                input_status=CaseInputStatus.MISSING,
                context_package=_case_input(ContextScheme.RECENT_PROSE).context_package,
            )

    def test_context_scheme_rejects_the_other_package_shape(self) -> None:
        baseline = _case_input(ContextScheme.RECENT_PROSE).context_package
        formal = _case_input(ContextScheme.WRITER_CONTEXT_PACKAGE).writer_context_package
        with pytest.raises(ValidationError, match="requires the formal package"):
            Stage3CaseContextInput(
                scheme=ContextScheme.WRITER_CONTEXT_PACKAGE,
                input_status=CaseInputStatus.READY,
                context_package=baseline,
            )
        with pytest.raises(ValidationError, match="baseline schemes"):
            Stage3CaseContextInput(
                scheme=ContextScheme.RECENT_PROSE,
                input_status=CaseInputStatus.READY,
                writer_context_package=formal,
            )

    def test_duplicate_scheme_inputs_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be unique"):
            _case(
                inputs=(
                    _case_input(ContextScheme.RECENT_PROSE),
                    _case_input(ContextScheme.RECENT_PROSE),
                    _case_input(ContextScheme.SIMPLE_RETRIEVAL),
                )
            )

    def test_input_for_returns_none_for_unknown_scheme(self) -> None:
        case = _case()
        assert case.input_for(ContextScheme.WRITER_CONTEXT_PACKAGE) is not None
        assert case.input_for(ContextScheme.RECENT_PROSE) is not None

    def test_input_for_returns_none_when_scheme_absent(self) -> None:
        case = _case(
            inputs=(
                _case_input(ContextScheme.RECENT_PROSE),
                _case_input(ContextScheme.SIMPLE_RETRIEVAL),
            )
        )
        assert case.input_for(ContextScheme.WRITER_CONTEXT_PACKAGE) is None


class TestCollectedReportContracts:
    def test_local_repair_requires_repair_count(self) -> None:
        with pytest.raises(ValidationError, match="at least one repair"):
            CollectedEditorialReport(
                report_id="editorial.test",
                draft_id="draft.test",
                verdict=EditorialVerdict.LOCAL_REPAIR,
                repair_count=0,
                rewrite_count=0,
            )

    def test_major_rewrite_requires_rewrite_count(self) -> None:
        with pytest.raises(ValidationError, match="at least one rewrite"):
            CollectedEditorialReport(
                report_id="editorial.test",
                draft_id="draft.test",
                verdict=EditorialVerdict.MAJOR_REWRITE,
                repair_count=0,
                rewrite_count=0,
            )

    def test_collected_reports_tolerate_unknown_fields(self) -> None:
        report = CollectedEditorialReport.model_validate_json(
            '{"report_id": "x", "draft_id": "y", "verdict": "pass", "future_field": 1}'
        )
        assert report.verdict is EditorialVerdict.PASS
        result = CollectedReconciliationResult.model_validate_json(
            '{"result_id": "x", "draft_id": "y", "future_field": "z"}'
        )
        assert result.items == ()

    def test_editorial_verdict_enum(self) -> None:
        with pytest.raises(ValidationError, match="'pass', 'local_repair'"):
            CollectedEditorialReport.model_validate_json(
                '{"report_id": "x", "draft_id": "y", "verdict": "unknown"}'
            )


class TestRuleAssessment:
    def test_counts_and_passed_for(self) -> None:
        assessment = RuleAssessment(
            checks=(
                _rule_check(True),
                _rule_check(False),
                _rule_check(True),
            )
        )
        assert assessment.passed_count == 2
        assert assessment.failed_count == 1
        assert assessment.passed_for(RuleCheckKind.FORBIDDEN_REVEAL_ABSENT) is False

    def test_passed_for_returns_none_when_kind_absent(self) -> None:
        assessment = RuleAssessment(checks=())
        assert assessment.passed_for(RuleCheckKind.FORBIDDEN_REVEAL_ABSENT) is None


class TestSchemeResultContracts:
    def test_input_not_ready_carries_no_writer_artifacts(self) -> None:
        with pytest.raises(ValidationError, match="cannot carry Writer artifacts"):
            Stage3SchemeResult(
                case_id=_stable("case.test"),
                scheme=ContextScheme.RECENT_PROSE,
                status=Stage3FailureCategory.INPUT_NOT_READY,
                writer=_writer_result(),
                failure_detail="missing",
            )

    def test_input_not_ready_requires_failure_detail(self) -> None:
        with pytest.raises(ValidationError, match="requires failure detail"):
            Stage3SchemeResult(
                case_id=_stable("case.test"),
                scheme=ContextScheme.RECENT_PROSE,
                status=Stage3FailureCategory.INPUT_NOT_READY,
            )

    def test_writer_failed_requires_failed_writer_without_draft(self) -> None:
        with pytest.raises(ValidationError, match="requires only a failed Writer result"):
            Stage3SchemeResult(
                case_id=_stable("case.test"),
                scheme=ContextScheme.RECENT_PROSE,
                status=Stage3FailureCategory.WRITER_FAILED,
                failure_detail="boom",
            )
        with pytest.raises(ValidationError, match="completed Writer result"):
            Stage3SchemeResult(
                case_id=_stable("case.test"),
                scheme=ContextScheme.RECENT_PROSE,
                status=Stage3FailureCategory.WRITER_FAILED,
                writer=_writer_result(),
                failure_detail="boom",
            )
        with pytest.raises(ValidationError, match="requires failure detail"):
            Stage3SchemeResult(
                case_id=_stable("case.test"),
                scheme=ContextScheme.RECENT_PROSE,
                status=Stage3FailureCategory.WRITER_FAILED,
                writer=_writer_result(completed=False),
            )
        result = Stage3SchemeResult(
            case_id=_stable("case.test"),
            scheme=ContextScheme.RECENT_PROSE,
            status=Stage3FailureCategory.WRITER_FAILED,
            writer=_writer_result(completed=False),
            failure_detail="boom",
        )
        assert result.draft is None

    def test_non_input_statuses_require_writer_and_draft(self) -> None:
        with pytest.raises(ValidationError, match="require Writer result and draft"):
            Stage3SchemeResult(
                case_id=_stable("case.test"),
                scheme=ContextScheme.RECENT_PROSE,
                status=Stage3FailureCategory.EDITOR_FAILED,
                failure_detail="missing",
            )

    def test_completed_requires_collected_editor_and_reconciliation(self) -> None:
        writer = _writer_result()
        with pytest.raises(ValidationError, match="requires Editorial and reconciliation"):
            Stage3SchemeResult(
                case_id=_stable("case.test"),
                scheme=ContextScheme.RECENT_PROSE,
                status=Stage3FailureCategory.COMPLETED,
                writer=writer,
                draft=writer.draft,
            )
        with pytest.raises(ValidationError, match="cannot carry failure detail"):
            Stage3SchemeResult(
                case_id=_stable("case.test"),
                scheme=ContextScheme.RECENT_PROSE,
                status=Stage3FailureCategory.COMPLETED,
                writer=writer,
                draft=writer.draft,
                editorial=_editorial(),
                reconciliation=_reconciliation(),
                failure_detail="boom",
            )

    def test_editor_failed_rejects_editorial_report(self) -> None:
        writer = _writer_result()
        with pytest.raises(ValidationError, match="cannot carry an Editorial report"):
            Stage3SchemeResult(
                case_id=_stable("case.test"),
                scheme=ContextScheme.RECENT_PROSE,
                status=Stage3FailureCategory.EDITOR_FAILED,
                writer=writer,
                draft=writer.draft,
                editorial=_editorial(),
                failure_detail="missing",
            )
        with pytest.raises(ValidationError, match="requires failure detail"):
            Stage3SchemeResult(
                case_id=_stable("case.test"),
                scheme=ContextScheme.RECENT_PROSE,
                status=Stage3FailureCategory.EDITOR_FAILED,
                writer=writer,
                draft=writer.draft,
            )

    def test_reconciliation_failed_requires_editorial(self) -> None:
        writer = _writer_result()
        with pytest.raises(ValidationError, match="requires a collected Editorial report"):
            Stage3SchemeResult(
                case_id=_stable("case.test"),
                scheme=ContextScheme.RECENT_PROSE,
                status=Stage3FailureCategory.RECONCILIATION_FAILED,
                writer=writer,
                draft=writer.draft,
                failure_detail="missing",
            )
        with pytest.raises(ValidationError, match="cannot carry a reconciliation result"):
            Stage3SchemeResult(
                case_id=_stable("case.test"),
                scheme=ContextScheme.RECENT_PROSE,
                status=Stage3FailureCategory.RECONCILIATION_FAILED,
                writer=writer,
                draft=writer.draft,
                editorial=_editorial(),
                reconciliation=_reconciliation(),
                failure_detail="missing",
            )
        with pytest.raises(ValidationError, match="requires failure detail"):
            Stage3SchemeResult(
                case_id=_stable("case.test"),
                scheme=ContextScheme.RECENT_PROSE,
                status=Stage3FailureCategory.RECONCILIATION_FAILED,
                writer=writer,
                draft=writer.draft,
                editorial=_editorial(),
            )

    def test_evaluation_failed_rejects_complete_scores(self) -> None:
        writer = _writer_result()
        scores = tuple(
            EvaluatorScore(
                case_id=_stable("case.test"),
                scheme=ContextScheme.RECENT_PROSE,
                dimension=dimension,
                score=0.5,
            )
            for dimension in EvaluatorDimension
        )
        with pytest.raises(ValidationError, match="has complete evaluation"):
            Stage3SchemeResult(
                case_id=_stable("case.test"),
                scheme=ContextScheme.RECENT_PROSE,
                status=Stage3FailureCategory.EVALUATION_FAILED,
                writer=writer,
                draft=writer.draft,
                editorial=_editorial(),
                reconciliation=_reconciliation(),
                evaluator_scores=scores,
                failure_detail="missing",
            )
        with pytest.raises(ValidationError, match="requires collected Editor"):
            Stage3SchemeResult(
                case_id=_stable("case.test"),
                scheme=ContextScheme.RECENT_PROSE,
                status=Stage3FailureCategory.EVALUATION_FAILED,
                writer=writer,
                draft=writer.draft,
                failure_detail="missing",
            )

    def test_evaluation_failed_accepts_partial_scores(self) -> None:
        writer = _writer_result()
        partial = (
            EvaluatorScore(
                case_id=_stable("case.test"),
                scheme=ContextScheme.RECENT_PROSE,
                dimension=EvaluatorDimension.PLAN_FOLLOWING,
                score=0.5,
            ),
        )
        result = Stage3SchemeResult(
            case_id=_stable("case.test"),
            scheme=ContextScheme.RECENT_PROSE,
            status=Stage3FailureCategory.EVALUATION_FAILED,
            writer=writer,
            draft=writer.draft,
            editorial=_editorial(),
            reconciliation=_reconciliation(),
            evaluator_scores=partial,
            failure_detail="missing",
        )
        assert result.status is Stage3FailureCategory.EVALUATION_FAILED

    def test_evaluation_failed_requires_failure_detail(self) -> None:
        writer = _writer_result()
        with pytest.raises(ValidationError, match="requires failure detail"):
            Stage3SchemeResult(
                case_id=_stable("case.test"),
                scheme=ContextScheme.RECENT_PROSE,
                status=Stage3FailureCategory.EVALUATION_FAILED,
                writer=writer,
                draft=writer.draft,
                editorial=_editorial(),
                reconciliation=_reconciliation(),
            )

    def test_validator_rejects_unsupported_status_via_direct_call(self) -> None:
        completed = _writer_result()
        bypassed = Stage3SchemeResult.model_construct(
            case_id=_stable("case.test"),
            scheme=ContextScheme.RECENT_PROSE,
            status=EditorialVerdict.PASS,  # type: ignore[arg-type]
            writer=completed,
            draft=completed.draft,
        )
        with pytest.raises(ValueError, match="unsupported scheme status"):
            Stage3SchemeResult.validate_consistency(bypassed)  # type: ignore[operator]

    def test_draft_must_match_writer_result(self) -> None:
        writer = _writer_result()
        assert writer.draft is not None
        foreign_draft = writer.draft.model_copy(update={"draft_id": _hash("0")})
        with pytest.raises(ValidationError, match="differs from its Writer result draft"):
            Stage3SchemeResult(
                case_id=_stable("case.test"),
                scheme=ContextScheme.RECENT_PROSE,
                status=Stage3FailureCategory.COMPLETED,
                writer=writer,
                draft=foreign_draft,
                editorial=_editorial(),
                reconciliation=_reconciliation(),
            )

    def test_scheme_result_json_round_trip_keeps_typed_status(self) -> None:
        result = Stage3SchemeResult(
            case_id=_stable("case.test"),
            scheme=ContextScheme.RECENT_PROSE,
            status=Stage3FailureCategory.WRITER_FAILED,
            writer=_writer_result(completed=False),
            failure_detail="boom",
        )
        restored = Stage3SchemeResult.model_validate_json(result.model_dump_json())
        assert restored.status is Stage3FailureCategory.WRITER_FAILED
        assert restored.writer is not None


class TestCaseAndReportContracts:
    def test_case_result_rejects_foreign_scheme_results(self) -> None:
        with pytest.raises(ValidationError, match="belongs to another case"):
            Stage3CaseResult(
                case_id=_stable("case.test"),
                schemes=(
                    Stage3SchemeResult(
                        case_id=_stable("case.other"),
                        scheme=ContextScheme.RECENT_PROSE,
                        status=Stage3FailureCategory.WRITER_FAILED,
                        writer=_writer_result(completed=False),
                        failure_detail="boom",
                    ),
                ),
            )

    def test_case_result_rejects_duplicate_schemes(self) -> None:
        writer = _writer_result()
        first = Stage3SchemeResult(
            case_id=_stable("case.test"),
            scheme=ContextScheme.RECENT_PROSE,
            status=Stage3FailureCategory.COMPLETED,
            writer=writer,
            draft=writer.draft,
            editorial=_editorial(),
            reconciliation=_reconciliation(),
        )
        with pytest.raises(ValidationError, match="must be unique"):
            Stage3CaseResult(case_id=_stable("case.test"), schemes=(first, first))

    def test_report_rejects_duplicate_cases(self) -> None:
        config = Stage3RunConfig(
            git_commit="deadbeef",
            git_dirty=False,
            writer_model="fake",
            command="run",
            created_at=NOW,
            case_directory="cases",
            output_directory="out",
        )
        scheme = Stage3SchemeResult(
            case_id=_stable("case.test"),
            scheme=ContextScheme.RECENT_PROSE,
            status=Stage3FailureCategory.INPUT_NOT_READY,
            failure_detail="missing",
        )
        with pytest.raises(ValidationError, match="case ids must be unique"):
            Stage3EvaluationReport(
                report_id=_stable("report.test"),
                run_config=config,
                cases=(
                    Stage3CaseResult(case_id=_stable("case.test"), schemes=(scheme,)),
                    Stage3CaseResult(case_id=_stable("case.test"), schemes=(scheme,)),
                ),
            )

    def test_run_summary_round_trip(self) -> None:
        summary = Stage3RunSummary(
            report_id=_stable("report.test"),
            case_count=1,
            scheme_count=3,
            completed=1,
            failed=2,
            schemes=(
                Stage3SchemeSummary(
                    scheme=ContextScheme.RECENT_PROSE,
                    case_count=1,
                    completed=1,
                    failed=0,
                ),
            ),
        )
        restored = Stage3RunSummary.model_validate_json(summary.model_dump_json())
        assert restored.completed == 1
        assert restored.schemes[0].scheme is ContextScheme.RECENT_PROSE
