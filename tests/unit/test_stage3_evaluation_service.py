from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.agents import build_writer_contract_bundle
from novel_agent.domain.generation import (
    DeclaredMemoryHint,
    WriterBudget,
    WriterExecutionResult,
    WriterSidecar,
    WriterSourceBinding,
    WriterTerminalStatus,
)
from novel_agent.domain.ids import CommitId, ProjectId, RunId, StableId
from novel_agent.domain.memory import (
    ContextBudgetReport,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1ContextPackage,
)
from novel_agent.domain.stage2 import AgentMode, FutureIsolationAttestation
from novel_agent.domain.stage3_evaluation import (
    CaseInputStatus,
    ContextScheme,
    EditorialVerdict,
    EvaluatorDimension,
    EvaluatorScore,
    HumanScoreEntry,
    ReconciliationVerdict,
    RuleAssessment,
    RuleCheckKind,
    Stage3CaseContextInput,
    Stage3CaseResult,
    Stage3EvaluationCase,
    Stage3EvaluationReport,
    Stage3FailureCategory,
    Stage3RunConfig,
    Stage3SchemeResult,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import content_id
from novel_agent.services.stage3_evaluation import (
    CaseLoadError,
    CollectionError,
    HumanScoreImportError,
    ScriptedEvaluator,
    Stage3EvaluationError,
    Stage3WriterRunBuilder,
    assemble_scheme_result,
    build_summary,
    collect_editorial_report,
    collect_reconciliation_result,
    evaluate_rules,
    evaluation_complete,
    export_human_scoring_package,
    import_human_scores,
    load_case,
    merge_evaluator_scores,
    render_summary_markdown,
    to_evaluator_scores,
)
from tests.unit.test_stage3_evaluation_domain import (
    _case,
    _editorial,
    _reconciliation,
    _task,
    _writer_context_package,
    _writer_result,
)

ROOT = Path(__file__).parents[2]

_RULE_BASE_COMMIT = "sha256:1111111111111111111111111111111111111111111111111111111111111111"


def _context_for_case_input() -> Stage1ContextPackage:
    return Stage1ContextPackage(
        context_id=StableId("context.rule"),
        base_commit=CommitId(_RULE_BASE_COMMIT),
        snapshot_id=StableId("snapshot.rule"),
        task_contract="task.test",
        budget_report=ContextBudgetReport(
            token_budget=100,
            mandatory_tokens=0,
            optional_tokens=0,
            full_chapter_read_count=0,
        ),
    )


FIXTURE_CASE_DIRECTORY = ROOT / "tests" / "fixtures" / "stage3_evaluation" / "cases" / "enter_tower"
NOW = datetime(2026, 7, 31, tzinfo=UTC)


class TestCaseLoading:
    def test_load_case_from_fixture(self) -> None:
        case = load_case(FIXTURE_CASE_DIRECTORY / "case.json")
        assert case.case_id.root == "case.stage3.eval.enter-tower"
        assert len(case.inputs) == 3
        assert len(case.evaluator_instructions) == 3

    def test_load_case_rejects_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(CaseLoadError, match="cannot read case JSON"):
            load_case(tmp_path / "missing.json")

    def test_load_case_rejects_invalid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "case.json"
        path.write_text("{not-json", encoding="utf-8")
        with pytest.raises(CaseLoadError, match="invalid evaluation case"):
            load_case(path)

    def test_load_case_rejects_invalid_case(self, tmp_path: Path) -> None:
        path = tmp_path / "case.json"
        path.write_text('{"case_id": "x"}', encoding="utf-8")
        with pytest.raises(CaseLoadError, match="invalid evaluation case"):
            load_case(path)


class TestCollection:
    def test_collect_editorial_report_missing_returns_none(self, tmp_path: Path) -> None:
        assert collect_editorial_report(tmp_path / "missing.json") is None

    def test_collect_editorial_report_reads_tolerant_payload(self, tmp_path: Path) -> None:
        path = tmp_path / "editorial.json"
        path.write_text(
            json.dumps(
                {
                    "report_id": "editorial.x",
                    "draft_id": "draft.x",
                    "verdict": "pass",
                    "extra_future_field": 1,
                }
            ),
            encoding="utf-8",
        )
        report = collect_editorial_report(path)
        assert report is not None
        assert report.verdict is EditorialVerdict.PASS

    def test_collect_editorial_report_rejects_malformed(self, tmp_path: Path) -> None:
        path = tmp_path / "editorial.json"
        path.write_text("{bad", encoding="utf-8")
        with pytest.raises(CollectionError, match="cannot read editorial report"):
            collect_editorial_report(path)
        path.write_text('{"report_id": 1}', encoding="utf-8")
        with pytest.raises(CollectionError, match="invalid editorial report"):
            collect_editorial_report(path)

    def test_collect_reconciliation_missing_and_invalid(self, tmp_path: Path) -> None:
        assert collect_reconciliation_result(tmp_path / "missing.json") is None
        path = tmp_path / "reconciliation.json"
        path.write_text('{"result_id": 5}', encoding="utf-8")
        with pytest.raises(CollectionError, match="invalid reconciliation result"):
            collect_reconciliation_result(path)


class TestRules:
    def test_rules_match_fixture_drafts(self) -> None:
        case = load_case(FIXTURE_CASE_DIRECTORY / "case.json")
        drafts = json.loads(
            (FIXTURE_CASE_DIRECTORY / "draft_outputs.json").read_text(encoding="utf-8")
        )
        expected = {
            "recent_prose": (2, 6),
            "simple_retrieval": (8, 8),
            "writer_context_package": (10, 10),
        }
        for scheme_value, (passed, total) in expected.items():
            scheme = next(item.scheme for item in case.inputs if item.scheme.value == scheme_value)
            payload = drafts[scheme_value]
            hints = tuple(
                DeclaredMemoryHint.model_validate_json(json.dumps(hint, ensure_ascii=False))
                for hint in payload["declared_memory_hints"]
            )
            sidecar = WriterSidecar(declared_memory_hints=hints)
            assessment = evaluate_rules(case, scheme, payload["draft_text"], sidecar)
            assert (assessment.passed_count, len(assessment.checks)) == (passed, total)

    def test_rules_skip_context_when_input_missing(self) -> None:
        case = _case(
            inputs=(
                Stage3CaseContextInput(
                    scheme=ContextScheme.RECENT_PROSE,
                    input_status=CaseInputStatus.MISSING,
                ),
                Stage3CaseContextInput(
                    scheme=ContextScheme.SIMPLE_RETRIEVAL,
                    input_status=CaseInputStatus.READY,
                    context_package=_context_for_case_input(),
                ),
                Stage3CaseContextInput(
                    scheme=ContextScheme.WRITER_CONTEXT_PACKAGE,
                    input_status=CaseInputStatus.READY,
                    writer_context_package=_writer_context_package(),
                ),
            )
        )
        assessment = evaluate_rules(case, ContextScheme.RECENT_PROSE, "正文")
        kinds = {check.kind for check in assessment.checks}
        assert RuleCheckKind.PLAN_OBLIGATION_PRESENT not in kinds
        assert RuleCheckKind.MANDATORY_CONSTRAINT_PRESENT in kinds

    def test_verbatim_clause_detects_presence(self) -> None:
        case = _case()
        assessment = evaluate_rules(
            case,
            ContextScheme.RECENT_PROSE,
            "This body explicitly says: CONSTRAINT-ALPHA, KEEP ME. Stay cautious elsewhere.",
        )
        assert (
            assessment.passed_for(
                __import__(
                    "novel_agent.domain.stage3_evaluation", fromlist=["RuleCheckKind"]
                ).RuleCheckKind.MANDATORY_CONSTRAINT_PRESENT
            )
            is True
        )

    def test_forbidden_reveal_detected_via_bigram_coverage(self) -> None:
        case = _case()
        assessment = evaluate_rules(
            case,
            ContextScheme.RECENT_PROSE,
            "The body leaks part of REVEAL-SECRET, for example the word SECRET.",
        )
        assert (
            assessment.passed_for(
                __import__(
                    "novel_agent.domain.stage3_evaluation", fromlist=["RuleCheckKind"]
                ).RuleCheckKind.FORBIDDEN_REVEAL_ABSENT
            )
            is False
        )

    def test_hint_evidence_checks(self) -> None:
        case = _case()
        sidecar = WriterSidecar(
            declared_memory_hints=(
                __import__(
                    "novel_agent.domain.generation", fromlist=["DeclaredMemoryHint"]
                ).DeclaredMemoryHint(
                    subject_hint="石门",
                    change_kind=__import__(
                        "novel_agent.domain.generation", fromlist=["MemoryHintChangeKind"]
                    ).MemoryHintChangeKind.CHANGE,
                    evidence_quote="石门向里退开",
                    confidence=0.9,
                ),
            ),
        )
        assessment = evaluate_rules(case, ContextScheme.RECENT_PROSE, "石门向里退开", sidecar)
        kind = __import__(
            "novel_agent.domain.stage3_evaluation", fromlist=["RuleCheckKind"]
        ).RuleCheckKind.DECLARED_HINT_EVIDENCE_PRESENT
        assert assessment.passed_for(kind) is True
        missing = evaluate_rules(case, ContextScheme.RECENT_PROSE, "没有这句", sidecar)
        assert missing.passed_for(kind) is False

    def test_length_check_uses_policy(self) -> None:
        case = _case()
        assessment = evaluate_rules(case, ContextScheme.RECENT_PROSE, "")
        kind = __import__(
            "novel_agent.domain.stage3_evaluation", fromlist=["RuleCheckKind"]
        ).RuleCheckKind.DRAFT_LENGTH_IN_POLICY
        assert assessment.passed_for(kind) is False

    def test_no_plan_check_without_plan_units(self) -> None:
        case = _case()
        assessment = evaluate_rules(case, ContextScheme.RECENT_PROSE, "正文")
        kind = __import__(
            "novel_agent.domain.stage3_evaluation", fromlist=["RuleCheckKind"]
        ).RuleCheckKind.PLAN_OBLIGATION_PRESENT
        assert assessment.passed_for(kind) is None

    def test_reference_without_content_terms_never_matches(self) -> None:
        case = _case(writing_task=_task().model_copy(update={"preserve_requirements": ("!!",)}))
        assessment = evaluate_rules(case, ContextScheme.RECENT_PROSE, "PLAIN BODY")
        kind = __import__(
            "novel_agent.domain.stage3_evaluation", fromlist=["RuleCheckKind"]
        ).RuleCheckKind.PRESERVE_REQUIREMENT_PRESENT
        assert assessment.passed_for(kind) is False

    def test_repeated_bigrams_are_deduplicated(self) -> None:
        case = _case(writing_task=_task().model_copy(update={"preserve_requirements": ("HAAAA",)}))
        assessment = evaluate_rules(case, ContextScheme.RECENT_PROSE, "正文")
        kind = __import__(
            "novel_agent.domain.stage3_evaluation", fromlist=["RuleCheckKind"]
        ).RuleCheckKind.PRESERVE_REQUIREMENT_PRESENT
        assert assessment.passed_for(kind) is False

    def test_plan_check_fires_when_context_has_plan_units(self) -> None:
        unit = RetrievalUnit(
            unit_id=StableId("unit.plan"),
            unit_kind=RetrievalUnitKind.PLAN_ANCHOR,
            source_commit=CommitId(
                "sha256:1111111111111111111111111111111111111111111111111111111111111111"
            ),
            snapshot_id=StableId("snapshot.test"),
            text="NO-BRUTE-FORCE, FIND THE TOWER ENTRY.",
            access_scope="writer_safe",
            information_label="author_plan",
            mandatory=True,
        )
        context = Stage1ContextPackage(
            context_id=StableId("context.plan"),
            base_commit=CommitId(
                "sha256:1111111111111111111111111111111111111111111111111111111111111111"
            ),
            snapshot_id=StableId("snapshot.test"),
            task_contract="task.test",
            active_plan_obligations=(unit,),
            budget_report=ContextBudgetReport(
                token_budget=100,
                mandatory_tokens=0,
                optional_tokens=0,
                full_chapter_read_count=0,
            ),
        )
        case = _case(
            inputs=(
                Stage3CaseContextInput(
                    scheme=ContextScheme.RECENT_PROSE,
                    input_status=CaseInputStatus.READY,
                    context_package=context,
                ),
                Stage3CaseContextInput(
                    scheme=ContextScheme.SIMPLE_RETRIEVAL,
                    input_status=CaseInputStatus.MISSING,
                ),
                Stage3CaseContextInput(
                    scheme=ContextScheme.WRITER_CONTEXT_PACKAGE,
                    input_status=CaseInputStatus.MISSING,
                ),
            )
        )
        assessment = evaluate_rules(
            case,
            ContextScheme.RECENT_PROSE,
            "This is NO-BRUTE-FORCE, FIND THE TOWER ENTRY.",
        )
        kind = __import__(
            "novel_agent.domain.stage3_evaluation", fromlist=["RuleCheckKind"]
        ).RuleCheckKind.PLAN_OBLIGATION_PRESENT
        assert assessment.passed_for(kind) is True


class TestAssembleSchemeResult:
    def test_input_not_ready_when_writer_absent(self) -> None:
        result = assemble_scheme_result(
            case=_case(),
            scheme=ContextScheme.RECENT_PROSE,
            writer=None,
            editorial=None,
            reconciliation=None,
        )
        assert result.status is Stage3FailureCategory.INPUT_NOT_READY
        assert result.failure_detail == "scheme context input missing or not ready"

    def test_writer_failed_status(self) -> None:
        result = assemble_scheme_result(
            case=_case(),
            scheme=ContextScheme.RECENT_PROSE,
            writer=_writer_result(completed=False),
            editorial=None,
            reconciliation=None,
        )
        assert result.status is Stage3FailureCategory.WRITER_FAILED
        assert "model_unavailable" in (result.failure_detail or "")

    def test_editor_failed_when_editorial_missing(self) -> None:
        writer = _writer_result()
        result = assemble_scheme_result(
            case=_case(),
            scheme=ContextScheme.RECENT_PROSE,
            writer=writer,
            editorial=None,
            reconciliation=_reconciliation(),
        )
        assert result.status is Stage3FailureCategory.EDITOR_FAILED
        assert result.draft is writer.draft

    def test_reconciliation_failed_when_reconciliation_missing(self) -> None:
        result = assemble_scheme_result(
            case=_case(),
            scheme=ContextScheme.RECENT_PROSE,
            writer=_writer_result(),
            editorial=_editorial(),
            reconciliation=None,
        )
        assert result.status is Stage3FailureCategory.RECONCILIATION_FAILED

    def test_collected_results_must_belong_to_the_generated_draft(self) -> None:
        writer = _writer_result()
        editorial = _editorial().model_copy(update={"draft_id": "draft.other"})
        result = assemble_scheme_result(
            case=_case(),
            scheme=ContextScheme.RECENT_PROSE,
            writer=writer,
            editorial=editorial,
            reconciliation=_reconciliation(),
        )
        assert result.status is Stage3FailureCategory.EDITOR_FAILED

        reconciliation = _reconciliation().model_copy(update={"draft_id": "draft.other"})
        result = assemble_scheme_result(
            case=_case(),
            scheme=ContextScheme.RECENT_PROSE,
            writer=writer,
            editorial=_editorial(),
            reconciliation=reconciliation,
        )
        assert result.status is Stage3FailureCategory.RECONCILIATION_FAILED

    def test_evaluation_failed_when_required_and_incomplete(self) -> None:
        partial = (
            EvaluatorScore(
                case_id=StableId("case.test"),
                scheme=ContextScheme.RECENT_PROSE,
                dimension=EvaluatorDimension.PLAN_FOLLOWING,
                score=0.5,
            ),
        )
        result = assemble_scheme_result(
            case=_case(),
            scheme=ContextScheme.RECENT_PROSE,
            writer=_writer_result(),
            editorial=_editorial(),
            reconciliation=_reconciliation(),
            evaluator_scores=partial,
            evaluation_required=True,
        )
        assert result.status is Stage3FailureCategory.EVALUATION_FAILED
        assert result.evaluator_scores == partial

    def test_completed_when_all_pieces_present(self) -> None:
        result = assemble_scheme_result(
            case=_case(),
            scheme=ContextScheme.RECENT_PROSE,
            writer=_writer_result(),
            editorial=_editorial(),
            reconciliation=_reconciliation(),
        )
        assert result.status is Stage3FailureCategory.COMPLETED
        assert result.editorial is not None
        assert result.reconciliation is not None

    def test_completed_writer_without_draft_is_rejected(self) -> None:
        bypassed = _writer_result().model_copy(deep=True)
        bypassed = WriterExecutionResult.model_construct(
            result_id=bypassed.result_id,
            invocation_id=bypassed.invocation_id,
            run_id=bypassed.run_id,
            task_id=bypassed.task_id,
            status=WriterTerminalStatus.COMPLETED,
            basis=bypassed.basis,
            draft=None,
            receipt=bypassed.receipt,
            artifacts=bypassed.artifacts,
            fingerprints=bypassed.fingerprints,
            metrics=bypassed.metrics,
            retry_safe=True,
        )
        with pytest.raises(Stage3EvaluationError, match="missing its draft"):
            assemble_scheme_result(
                case=_case(),
                scheme=ContextScheme.RECENT_PROSE,
                writer=bypassed,
                editorial=_editorial(),
                reconciliation=_reconciliation(),
            )

    def test_writer_failure_detail_tolerates_missing_parts(self) -> None:
        failed = _writer_result(completed=False)
        bypassed = WriterExecutionResult.model_construct(
            result_id=failed.result_id,
            invocation_id=failed.invocation_id,
            run_id=failed.run_id,
            task_id=failed.task_id,
            status=failed.status,
            basis=failed.basis,
            fingerprints=failed.fingerprints,
            metrics=failed.metrics,
            retry_safe=False,
            failure_code=None,
            failure_detail=None,
        )
        from novel_agent.services.stage3_evaluation import _writer_failure_detail

        assert _writer_failure_detail(bypassed) == "model_unavailable"


class TestEvaluationScores:
    def test_evaluation_complete_requires_all_dimensions(self) -> None:
        case_id = StableId("case.test")
        scheme = ContextScheme.RECENT_PROSE
        complete = tuple(
            EvaluatorScore(
                case_id=case_id,
                scheme=scheme,
                dimension=dimension,
                score=0.5,
            )
            for dimension in EvaluatorDimension
        )
        assert evaluation_complete(complete) is True
        assert evaluation_complete(complete[:2]) is False
        null_scored = (
            *complete[:3],
            EvaluatorScore(
                case_id=case_id,
                scheme=scheme,
                dimension=EvaluatorDimension.LITERARY_QUALITY_DEGRADATION,
                score=None,
            ),
        )
        assert evaluation_complete(null_scored) is False
        assert evaluation_complete(()) is False

    def test_merge_evaluator_scores_human_wins(self) -> None:
        case_id = StableId("case.test")
        scheme = ContextScheme.RECENT_PROSE
        scripted = (
            EvaluatorScore(
                case_id=case_id,
                scheme=scheme,
                dimension=EvaluatorDimension.PLAN_FOLLOWING,
                score=0.2,
                source="scripted",
            ),
            EvaluatorScore(
                case_id=case_id,
                scheme=scheme,
                dimension=EvaluatorDimension.PLAN_FOLLOWING,
                score=0.9,
                source="human",
            ),
        )
        merged = merge_evaluator_scores(scripted, ())
        assert len(merged) == 2
        human_wins = merge_evaluator_scores(scripted[:1], scripted[1:])
        assert len(human_wins) == 1
        assert human_wins[0].source == "human"


class TestHumanScores:
    def _write(self, tmp_path: Path, payload: object) -> Path:
        path = tmp_path / "human.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def _entry(self, **updates: object) -> dict[str, object]:
        values: dict[str, object] = {
            "case_id": "case.test",
            "scheme": "recent_prose",
            "dimension": "plan_following",
            "score": 0.8,
            "rationale": "人工评分",
            "reviewer_label": "reviewer-1",
        }
        values.update(updates)
        return values

    def test_import_accepts_known_case(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, [self._entry()])
        entries = import_human_scores(path, (StableId("case.test"),))
        assert len(entries) == 1
        assert entries[0].score == 0.8
        assert entries[0].reviewer_label == "reviewer-1"

    def test_import_rejects_unknown_case(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, [self._entry()])
        with pytest.raises(HumanScoreImportError, match="unknown case"):
            import_human_scores(path, (StableId("case.other"),))

    def test_import_skips_null_scores(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            [
                self._entry(),
                self._entry(score=None, dimension="literary_quality_degradation"),
            ],
        )
        entries = import_human_scores(path, (StableId("case.test"),))
        assert len(entries) == 1

    def test_import_rejects_malformed_file(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "not-a-list")
        with pytest.raises(HumanScoreImportError, match="must be a JSON list"):
            import_human_scores(path, (StableId("case.test"),))
        path = self._write(tmp_path, [7])
        with pytest.raises(HumanScoreImportError, match="must be a JSON object"):
            import_human_scores(path, (StableId("case.test"),))
        path = self._write(tmp_path, [self._entry(score="high")])
        with pytest.raises(HumanScoreImportError, match="invalid human score entry"):
            import_human_scores(path, (StableId("case.test"),))
        unreadable = tmp_path / "unreadable.json"
        with pytest.raises(HumanScoreImportError, match="cannot read human scores"):
            import_human_scores(unreadable, (StableId("case.test"),))

    def test_to_evaluator_scores_marks_human_source(self) -> None:
        entries = (
            HumanScoreEntry(
                case_id=StableId("case.test"),
                scheme=ContextScheme.RECENT_PROSE,
                dimension=EvaluatorDimension.PLAN_FOLLOWING,
                score=0.7,
            ),
        )
        scores = to_evaluator_scores(entries)
        assert scores[0].source == "human"
        assert scores[0].score == 0.7

    def test_export_human_scoring_package(self, tmp_path: Path) -> None:
        case = _case()
        drafts = {
            case.case_id: {
                ContextScheme.RECENT_PROSE: "正文甲",
                ContextScheme.SIMPLE_RETRIEVAL: "正文乙",
            }
        }
        scores_path, instructions_path, drafts_path = export_human_scoring_package(
            (case,),
            tmp_path / "package",
            drafts,
        )
        entries = json.loads(scores_path.read_text(encoding="utf-8"))
        assert len(entries) == len(ContextScheme) * len(EvaluatorDimension)
        assert all(entry["score"] is None for entry in entries)
        instructions = json.loads(instructions_path.read_text(encoding="utf-8"))
        assert "SCORE FROM THE PROSE ONLY" in instructions["case.test"]["evaluator_instructions"]
        exported_drafts = json.loads(drafts_path.read_text(encoding="utf-8"))
        assert exported_drafts["case.test"]["recent_prose"] == "正文甲"


class TestScriptedEvaluator:
    def test_evaluate_returns_scores(self, tmp_path: Path) -> None:
        path = tmp_path / "scores.json"
        path.write_text(
            json.dumps(
                {
                    "case.test": {
                        "recent_prose": {
                            "plan_following": {"score": 0.4, "rationale": "部分使用"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        evaluator = ScriptedEvaluator(path)
        scores = evaluator.evaluate(_case(), ContextScheme.RECENT_PROSE)
        assert scores[0].dimension is EvaluatorDimension.PLAN_FOLLOWING
        assert scores[0].score == 0.4
        assert scores[0].source == "scripted"

    def test_evaluate_missing_keys_return_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "scores.json"
        path.write_text('{"case.other": {"recent_prose": {}}}', encoding="utf-8")
        assert ScriptedEvaluator(path).evaluate(_case(), ContextScheme.RECENT_PROSE) == ()
        path.write_text('{"case.test": {"simple_retrieval": {}}}', encoding="utf-8")
        assert ScriptedEvaluator(path).evaluate(_case(), ContextScheme.RECENT_PROSE) == ()
        path.write_text('{"case.test": {"recent_prose": 7}}', encoding="utf-8")
        assert ScriptedEvaluator(path).evaluate(_case(), ContextScheme.RECENT_PROSE) == ()

    def test_evaluate_skips_non_numeric_scores(self, tmp_path: Path) -> None:
        path = tmp_path / "scores.json"
        path.write_text(
            json.dumps(
                {
                    "case.test": {
                        "recent_prose": {
                            "plan_following": {"score": "high"},
                            "continuity_and_fact_conflict": {"score": 0.5},
                            "literary_quality_degradation": {"score": None},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        evaluator = ScriptedEvaluator(path)
        scores = evaluator.evaluate(_case(), ContextScheme.RECENT_PROSE)
        assert len(scores) == 2
        assert all(score.score == 0.5 or score.score is None for score in scores)

    def test_constructor_rejects_malformed_file(self, tmp_path: Path) -> None:
        path = tmp_path / "scores.json"
        path.write_text("[1, 2]", encoding="utf-8")
        with pytest.raises(Stage3EvaluationError, match="must be a JSON object"):
            ScriptedEvaluator(path)
        unreadable = tmp_path / "missing.json"
        with pytest.raises(Stage3EvaluationError, match="cannot read"):
            ScriptedEvaluator(unreadable)


class TestWriterRunBuilder:
    def _builder(
        self,
        tmp_path: Path,
        *,
        source_id: StableId | None = None,
    ) -> Stage3WriterRunBuilder:
        bundle = build_writer_contract_bundle(
            ROOT / "src" / "novel_agent",
            modes=(AgentMode.DRAFT,),
        )
        spec = bundle.agent_specs[0]
        resolved_source_id = source_id or StableId("source.test")
        artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path))
        attestation = FutureIsolationAttestation(
            attestation_id=StableId("attestation.builder"),
            checkpoint_chapter=20,
            canonical_source_ids=(resolved_source_id,),
            evaluator_only_source_ids=(),
            passed=True,
            configuration_fingerprint=content_id({"profile": "builder"}),
        )
        return Stage3WriterRunBuilder(
            artifacts=artifacts,
            schema_version=spec.version,
            project_id=ProjectId("project.builder"),
            run_id=RunId("run.builder"),
            writer_configuration_fingerprint=content_id(spec.model_dump(mode="json")),
            model_configuration_fingerprint=content_id({"model": "fake"}),
            attestation=attestation,
            source_binding=WriterSourceBinding(
                source_id=resolved_source_id,
                source_artifact=artifacts.put(
                    b'{"source": true}',
                    "application/vnd.novel-agent.writer-source+json",
                    spec.version,
                ),
            ),
            plan_payload=b'{"plan": true}',
            profile_payload=b'{"profile": true}',
            source_payload=b'{"source": true}',
            budget=WriterBudget(
                max_model_calls=1,
                input_token_limit=1000,
                output_token_limit=1000,
            ),
        )

    def test_prepare_builds_one_comparable_run_per_ready_scheme(self, tmp_path: Path) -> None:
        builder = self._builder(tmp_path)
        runs = builder.prepare(_case())
        assert [run.scheme for run in runs] == list(ContextScheme)
        run_ids = {run.invocation.invocation_id for run in runs}
        assert len(run_ids) == 3
        budgets = {run.invocation.budget for run in runs}
        assert len(budgets) == 1
        modes = {run.invocation.mode for run in runs}
        assert modes == {AgentMode.DRAFT}

    def test_prepare_skips_missing_inputs(self, tmp_path: Path) -> None:
        builder = self._builder(tmp_path)
        case = _case(
            inputs=(
                Stage3CaseContextInput(
                    scheme=ContextScheme.RECENT_PROSE,
                    input_status=CaseInputStatus.READY,
                    context_package=Stage1ContextPackage(
                        context_id=StableId("context.recent"),
                        base_commit=CommitId(
                            "sha256:1111111111111111111111111111111111111111111111111111111111111111"
                        ),
                        snapshot_id=StableId("snapshot.builder"),
                        task_contract="task.test",
                        budget_report=ContextBudgetReport(
                            token_budget=100,
                            mandatory_tokens=0,
                            optional_tokens=0,
                            full_chapter_read_count=0,
                        ),
                    ),
                ),
                Stage3CaseContextInput(
                    scheme=ContextScheme.SIMPLE_RETRIEVAL,
                    input_status=CaseInputStatus.MISSING,
                ),
                Stage3CaseContextInput(
                    scheme=ContextScheme.WRITER_CONTEXT_PACKAGE,
                    input_status=CaseInputStatus.MISSING,
                ),
            )
        )
        runs = builder.prepare(case)
        assert [run.scheme for run in runs] == [ContextScheme.RECENT_PROSE]
        assert runs[0].invocation.context_package.context_id.root == "context.recent"

    def test_prepare_requires_context_for_ready_input(self, tmp_path: Path) -> None:
        builder = self._builder(tmp_path)
        bypassed = Stage3CaseContextInput.model_construct(
            scheme=ContextScheme.RECENT_PROSE,
            input_status=CaseInputStatus.READY,
            context_package=None,
            entry="fixture",
        )
        case = Stage3EvaluationCase.model_construct(
            case_id=StableId("case.test"),
            writing_task=_task(),
            inputs=(bypassed,),
            evaluator_instructions=(),
        )
        with pytest.raises(Stage3EvaluationError, match="lacks a context package"):
            builder.prepare(case)


class TestSummary:
    def _report(
        self, *, schemes: tuple[ContextScheme, ...] = tuple(ContextScheme)
    ) -> Stage3EvaluationReport:
        config = Stage3RunConfig(
            git_commit="deadbeef",
            git_dirty=True,
            writer_model="fake",
            command="run --evaluator scripted",
            created_at=NOW,
            case_directory="cases",
            output_directory="out",
        )
        results: list[Stage3SchemeResult] = []
        for index, scheme in enumerate(schemes):
            if index == 0:
                writer = _writer_result()
                results.append(
                    Stage3SchemeResult(
                        case_id=StableId("case.test"),
                        scheme=scheme,
                        status=Stage3FailureCategory.COMPLETED,
                        writer=writer,
                        draft=writer.draft,
                        editorial=_editorial(EditorialVerdict.LOCAL_REPAIR),
                        reconciliation=_reconciliation(),
                        rules=RuleAssessment(
                            checks=(
                                __import__(
                                    "novel_agent.domain.stage3_evaluation",
                                    fromlist=["RuleCheckResult"],
                                ).RuleCheckResult(
                                    check_id=StableId(f"check.{index}.0"),
                                    kind=__import__(
                                        "novel_agent.domain.stage3_evaluation",
                                        fromlist=["RuleCheckKind"],
                                    ).RuleCheckKind.DRAFT_LENGTH_IN_POLICY,
                                    passed=True,
                                    reference="policy",
                                    detail="ok",
                                ),
                            )
                        ),
                        evaluator_scores=(
                            EvaluatorScore(
                                case_id=StableId("case.test"),
                                scheme=scheme,
                                dimension=EvaluatorDimension.PLAN_FOLLOWING,
                                score=0.5,
                            ),
                        ),
                    )
                )
            else:
                results.append(
                    Stage3SchemeResult(
                        case_id=StableId("case.test"),
                        scheme=scheme,
                        status=Stage3FailureCategory.WRITER_FAILED,
                        writer=_writer_result(completed=False),
                        failure_detail="boom",
                    )
                )
        return Stage3EvaluationReport(
            report_id=StableId("report.test"),
            run_config=config,
            cases=(Stage3CaseResult(case_id=StableId("case.test"), schemes=tuple(results)),),
        )

    def test_build_summary_aggregates_completed_and_failed(self) -> None:
        summary = build_summary(self._report())
        assert summary.case_count == 1
        assert summary.scheme_count == 3
        assert summary.completed == 1
        assert summary.failed == 2
        assert summary.failure_totals[Stage3FailureCategory.WRITER_FAILED] == 2
        assert Stage3FailureCategory.COMPLETED not in summary.failure_totals
        recent = next(item for item in summary.schemes if item.scheme is ContextScheme.RECENT_PROSE)
        assert recent.editor_verdicts[EditorialVerdict.LOCAL_REPAIR] == 1
        assert recent.repair_count == 1
        assert recent.reconciliation[ReconciliationVerdict.MATCHED] == 1
        assert recent.rule_passed == 1
        assert recent.rule_total == 1
        assert recent.evaluator_scored_dimensions == 1
        assert recent.input_tokens == 10
        assert recent.output_tokens == 5
        assert recent.latency_ms == 3
        failed = next(
            item for item in summary.schemes if item.scheme is ContextScheme.SIMPLE_RETRIEVAL
        )
        assert failed.failures[Stage3FailureCategory.WRITER_FAILED] == 1
        assert failed.completed == 0

    def test_build_summary_tolerates_completed_without_rules(self) -> None:
        writer = _writer_result()
        scheme_result = Stage3SchemeResult(
            case_id=StableId("case.test"),
            scheme=ContextScheme.RECENT_PROSE,
            status=Stage3FailureCategory.COMPLETED,
            writer=writer,
            draft=writer.draft,
            editorial=_editorial(),
            reconciliation=_reconciliation(),
        )
        config = Stage3RunConfig(
            git_commit="deadbeef",
            git_dirty=False,
            writer_model="fake",
            command="run",
            created_at=NOW,
            case_directory="cases",
            output_directory="out",
        )
        report = Stage3EvaluationReport(
            report_id=StableId("report.test"),
            run_config=config,
            cases=(
                Stage3CaseResult(
                    case_id=StableId("case.test"),
                    schemes=(scheme_result,),
                ),
            ),
        )
        summary = build_summary(report)
        recent = next(item for item in summary.schemes if item.scheme is ContextScheme.RECENT_PROSE)
        assert recent.rule_total == 0
        assert recent.completed == 1

    def test_build_summary_keeps_limitations(self) -> None:
        summary = build_summary(
            self._report(schemes=(ContextScheme.RECENT_PROSE,)),
            limitations=("限制一",),
        )
        assert summary.limitations == ("限制一",)

    def test_render_summary_markdown_contains_sections(self) -> None:
        summary = build_summary(self._report())
        rendered = render_summary_markdown(summary, self._report())
        assert "## Totals" in rendered
        assert "## Failure breakdown" in rendered
        assert "## Per-scheme results" in rendered
        assert "## Per-case detail" in rendered
        assert "## Limitations" in rendered
        assert "case.test" in rendered
        assert "recent_prose" in rendered
        assert "writer_failed" in rendered
        assert "deadbeef" in rendered

    def test_render_summary_markdown_handles_no_failures_and_explicit_limitations(self) -> None:
        report = self._report(schemes=(ContextScheme.RECENT_PROSE,))
        completed = (
            report.cases[0]
            .schemes[0]
            .model_copy(
                update={
                    "status": Stage3FailureCategory.COMPLETED,
                    "failure_detail": None,
                    "rules": None,
                }
            )
        )
        report = report.model_copy(
            update={"cases": (report.cases[0].model_copy(update={"schemes": (completed,)}),)}
        )
        summary = build_summary(report, limitations=("pilot only",))
        rendered = render_summary_markdown(summary, report)
        assert "No failures." in rendered
        assert "- pilot only" in rendered
