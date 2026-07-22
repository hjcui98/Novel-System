"""Translate an audited Stage 1 model benchmark into Evaluation Ledger records."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from novel_agent.domain.benchmark import Stage1BenchmarkResult
from novel_agent.domain.evaluation import BenchmarkRunConfig, EvaluationParameter
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.model_calls import ModelRole, RetrievalInferenceStatus
from novel_agent.domain.runtime import EvaluationDecision, EvaluationEntry, EvaluationMetric

EVALUATOR_VERSION = "stage1-native-bge-v1"


def build_stage1_evaluation_records(
    result: Stage1BenchmarkResult,
    dataset_hash: ArtifactId,
    *,
    created_at: datetime,
) -> tuple[BenchmarkRunConfig, tuple[EvaluationEntry, ...]]:
    if result.run_id is None or not result.retrieval_model_calls:
        raise ValueError("model benchmark evaluation requires an audited run and model calls")
    if any(
        call.run_id != result.run_id
        or call.model_role is not ModelRole.BATCH_TEST
        or call.status is not RetrievalInferenceStatus.SUCCEEDED
        for call in result.retrieval_model_calls
    ):
        raise ValueError("model benchmark evaluation contains invalid model audit records")

    config = BenchmarkRunConfig(
        config_id=StableId(f"evaluation-config.{result.run_id.root}"),
        benchmark_id=f"{result.bundle_id.root}:{result.case_id.root}:{result.track.value}",
        dataset_hash=dataset_hash,
        run_id=result.run_id,
        code_version=EVALUATOR_VERSION,
        random_seed=result.config.random_seed,
        parameters=(
            EvaluationParameter(name="snapshot_id", value=result.snapshot_id.root),
            EvaluationParameter(name="token_budget", value=result.config.token_budget),
            EvaluationParameter(
                name="per_channel_candidate_limit",
                value=result.config.per_channel_candidate_limit,
            ),
            EvaluationParameter(
                name="fused_candidate_limit", value=result.config.fused_candidate_limit
            ),
            EvaluationParameter(name="rrf_k", value=result.config.rrf_k),
            EvaluationParameter(name="embedding_profile", value=result.config.embedding_profile),
            EvaluationParameter(name="reranker_profile", value=result.config.reranker_profile),
            EvaluationParameter(name="need_profile", value=result.config.need_profile),
            EvaluationParameter(name="query_condition", value=result.config.query_condition.value),
        ),
        model_required=True,
        model_role=ModelRole.BATCH_TEST,
    )
    endpoints = ";".join(
        sorted({f"{call.operation.value}={call.endpoint}" for call in result.retrieval_model_calls})
    )
    versions = ";".join(
        sorted(
            {
                f"{call.operation.value}={call.model}@{call.revision}#{call.runtime_fingerprint}"
                for call in result.retrieval_model_calls
            }
        )
    )
    cost = sum(
        (call.usage.cost_usd for call in result.retrieval_model_calls),
        start=Decimal("0"),
    )
    latency = sum(call.latency_ms for call in result.retrieval_model_calls)
    entries = tuple(
        EvaluationEntry(
            evaluation_id=StableId(f"evaluation.{result.run_id.root}.{index:02d}"),
            run_id=result.run_id,
            candidate_id=StableId(
                f"benchmark-profile.{re.sub(r'[^A-Za-z0-9._:-]', '-', profile.profile)}"
            ),
            commit_id=result.base_commit,
            evaluator="stage1-benchmark-runner",
            evaluator_version=EVALUATOR_VERSION,
            model_role=ModelRole.BATCH_TEST,
            model_endpoint=endpoints,
            model_version=versions,
            model_cost_usd=cost,
            model_latency_ms=latency,
            rubric_version=result.config.config_version.root,
            metrics=tuple(
                EvaluationMetric(
                    name=name,
                    value=float(value),
                    unit=_metric_unit(name),
                )
                for name, value in profile.metrics.model_dump(mode="python").items()
                if value is not None
            ),
            failure_codes=tuple(category.value for category in profile.failure_categories),
            decision=(
                EvaluationDecision.SELECTED
                if not profile.failure_categories
                else EvaluationDecision.REJECTED
            ),
            created_at=created_at,
        )
        for index, profile in enumerate(result.profile_results, start=1)
    )
    return config, entries


def _metric_unit(name: str) -> str:
    if name in {"l0_evidence_tokens_read", "reranker_pair_tokens"}:
        return "tokens"
    if name.startswith("average_"):
        return "count"
    return "ratio"
