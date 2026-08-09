"""Map Stage 2 paired Pilot evidence into the independent Evaluation Ledger."""

from __future__ import annotations

from datetime import datetime

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.evaluation import BenchmarkRunConfig, EvaluationParameter
from novel_agent.domain.ids import RunId, StableId
from novel_agent.domain.runtime import EvaluationDecision, EvaluationEntry, EvaluationMetric
from novel_agent.domain.stage2 import PairedPilotCaseResult, Stage2PairedPilotReport


class Stage2PairedEvaluationBuilder:
    evaluator = "stage2-paired-pilot-evaluator"
    evaluator_version = "0.1.0"
    rubric_version = "stage2-controller-paired-v0.1"

    def build(
        self,
        report: Stage2PairedPilotReport,
        report_artifact: ArtifactRef,
        *,
        created_at: datetime,
    ) -> tuple[BenchmarkRunConfig, tuple[EvaluationEntry, ...]]:
        run_id = RunId(f"run.{report.report_id.root}")
        config = BenchmarkRunConfig(
            config_id=report.report_id,
            benchmark_id=report.report_id.root,
            dataset_hash=report.bundle_hash,
            run_id=run_id,
            code_version=self.evaluator_version,
            random_seed=0,
            parameters=(
                EvaluationParameter(
                    name="configuration_fingerprint",
                    value=report.configuration_fingerprint.root,
                ),
                EvaluationParameter(name="paired_results_count", value=report.paired_results_count),
                EvaluationParameter(
                    name="held_out_complex_gain_proven",
                    value=report.held_out_complex_gain_proven,
                ),
            ),
            model_required=False,
        )
        entries = tuple(
            self._entry(report, case, report_artifact, run_id, created_at) for case in report.cases
        )
        return config, entries

    def _entry(
        self,
        report: Stage2PairedPilotReport,
        case: PairedPilotCaseResult,
        report_artifact: ArtifactRef,
        run_id: RunId,
        created_at: datetime,
    ) -> EvaluationEntry:
        failure_codes = tuple(case.blockers)
        if not report.held_out_complex_gain_proven:
            failure_codes = (*failure_codes, "HELD_OUT_COMPLEX_GAIN_NOT_PROVEN")
        metrics = [
            EvaluationMetric(
                name="deterministic_gold_evidence_recall",
                value=case.deterministic_metrics.gold_evidence_recall,
            ),
            EvaluationMetric(
                name="deterministic_plan_obligation_coverage",
                value=case.deterministic_metrics.plan_obligation_coverage,
            ),
            EvaluationMetric(
                name="deterministic_retrieval_calls",
                value=float(case.deterministic_metrics.retrieval_call_count),
                unit="calls",
            ),
        ]
        if case.agentic_metrics is not None:
            metrics.extend(
                (
                    EvaluationMetric(
                        name="agentic_gold_evidence_recall",
                        value=case.agentic_metrics.gold_evidence_recall,
                    ),
                    EvaluationMetric(
                        name="agentic_plan_obligation_coverage",
                        value=case.agentic_metrics.plan_obligation_coverage,
                    ),
                    EvaluationMetric(
                        name="agentic_retrieval_calls",
                        value=float(case.agentic_metrics.retrieval_call_count),
                        unit="calls",
                    ),
                )
            )
        metrics.extend(
            (
                EvaluationMetric(
                    name="future_leakage_count",
                    value=float(
                        case.deterministic_metrics.future_leakage_count
                        + (
                            case.agentic_metrics.future_leakage_count
                            if case.agentic_metrics is not None
                            else 0
                        )
                    ),
                    unit="artifacts",
                ),
                EvaluationMetric(
                    name="safety_regression",
                    value=float(case.safety_regression is True),
                ),
                EvaluationMetric(name="comparable", value=float(case.comparable)),
            )
        )
        return EvaluationEntry(
            evaluation_id=StableId(
                f"evaluation.stage2-paired.{case.case_id.root}.{case.information_profile.value}"
            ),
            run_id=run_id,
            candidate_id=case.pair_id,
            evaluator=self.evaluator,
            evaluator_version=self.evaluator_version,
            rubric_version=self.rubric_version,
            metrics=tuple(metrics),
            failure_codes=failure_codes,
            evidence_artifacts=(report_artifact,),
            decision=EvaluationDecision.INFORMATIONAL,
            created_at=created_at,
        )
