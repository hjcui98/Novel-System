"""Map deterministic Stage 2 retrieval-gate evidence into the Evaluation Ledger."""

from __future__ import annotations

from datetime import datetime

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.evaluation import BenchmarkRunConfig, EvaluationParameter
from novel_agent.domain.gates import (
    Stage2RetrievalCheckpointEvidence,
    Stage2RetrievalGateReport,
)
from novel_agent.domain.ids import ArtifactId, RunId, StableId
from novel_agent.domain.retrieval_routing import L2IndexKind
from novel_agent.domain.runtime import EvaluationDecision, EvaluationEntry, EvaluationMetric


class Stage2RetrievalGateEvaluationBuilder:
    evaluator = "stage2-deterministic-retrieval-gate"
    evaluator_version = "0.1.0"
    rubric_version = "stage2-retrieval-c20-c95-v0.1"

    def build(
        self,
        report: Stage2RetrievalGateReport,
        report_artifact: ArtifactRef,
        *,
        dataset_hash: ArtifactId,
        code_version: str,
        created_at: datetime,
    ) -> tuple[BenchmarkRunConfig, tuple[EvaluationEntry, ...]]:
        if report.status != "passed":
            raise ValueError("only a passed retrieval gate can enter the accepted evidence ledger")
        final_commit = report.checkpoints[-1].source_commit
        suffix = final_commit.root[-16:]
        run_id = RunId(f"run.stage2-retrieval-gate.{report.project_id.root}.{suffix}")
        config = BenchmarkRunConfig(
            config_id=StableId(f"config.stage2-retrieval-gate.{report.project_id.root}.{suffix}"),
            benchmark_id="stage2-deterministic-retrieval-c20-c95",
            dataset_hash=dataset_hash,
            run_id=run_id,
            code_version=code_version,
            random_seed=0,
            parameters=(
                EvaluationParameter(
                    name="retrieval_backend_profile",
                    value=report.retrieval_backend_profile.value,
                ),
                EvaluationParameter(
                    name="checkpoint_chapters",
                    value=[item.checkpoint for item in report.checkpoints],
                ),
                EvaluationParameter(name="immutable_index_targets", value=True),
            ),
            model_required=False,
        )
        entries = tuple(
            self._entry(checkpoint, report_artifact, run_id, created_at)
            for checkpoint in report.checkpoints
        )
        return config, entries

    def _entry(
        self,
        checkpoint: Stage2RetrievalCheckpointEvidence,
        report_artifact: ArtifactRef,
        run_id: RunId,
        created_at: datetime,
    ) -> EvaluationEntry:
        anchor_total = checkpoint.index_totals[L2IndexKind.ANCHOR]
        grounded_total = checkpoint.index_totals[L2IndexKind.GROUNDED]
        return EvaluationEntry(
            evaluation_id=StableId(
                f"evaluation.stage2-retrieval.C{checkpoint.checkpoint}."
                f"{checkpoint.source_commit.root[-16:]}"
            ),
            run_id=run_id,
            commit_id=checkpoint.source_commit,
            evaluator=self.evaluator,
            evaluator_version=self.evaluator_version,
            rubric_version=self.rubric_version,
            metrics=(
                EvaluationMetric(name="checkpoint_chapter", value=float(checkpoint.checkpoint)),
                EvaluationMetric(name="passed", value=float(checkpoint.passed)),
                EvaluationMetric(
                    name="r1_record_count",
                    value=float(checkpoint.r1_counts.records),
                    unit="records",
                ),
                EvaluationMetric(
                    name="r1_entity_association_count",
                    value=float(checkpoint.r1_counts.entity_associations),
                    unit="associations",
                ),
                EvaluationMetric(
                    name="graph_relation_edge_count",
                    value=float(checkpoint.r1_counts.relation_edges),
                    unit="edges",
                ),
                EvaluationMetric(
                    name="anchor_index_document_count",
                    value=float(anchor_total),
                    unit="documents",
                ),
                EvaluationMetric(
                    name="grounded_index_document_count",
                    value=float(grounded_total),
                    unit="documents",
                ),
                EvaluationMetric(
                    name="immutable_physical_index_target",
                    value=1.0,
                ),
                EvaluationMetric(
                    name="failure_count",
                    value=float(len(checkpoint.failures)),
                ),
            ),
            failure_codes=checkpoint.failures,
            evidence_artifacts=(report_artifact,),
            decision=EvaluationDecision.INFORMATIONAL,
            created_at=created_at,
        )
