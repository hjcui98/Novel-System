"""Formal full-chain Stage 3 evaluation manifest and report contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.model_calls import ModelCallLedgerAggregate, ModelCostAvailability
from novel_agent.domain.stage3_evaluation import ContextScheme, EvaluatorScore, RuleAssessment
from novel_agent.domain.writing_loop import WritingLoopResult


class Stage3FormalManifest(DomainModel):
    manifest_id: StableId
    git_commit: str = Field(min_length=1)
    source_fingerprint: ArtifactId
    stage2_base_commit: str = Field(min_length=1)
    stage2_configuration_fingerprint: ArtifactId
    memory_gateway_policy_identity: str = Field(min_length=1)
    writer_model_identity: str = Field(min_length=1)
    editor_model_identity: str = Field(min_length=1)
    observer_model_identity: str = Field(min_length=1)
    evaluator_model_identity: str = Field(min_length=1)
    rubric_artifact: ArtifactRef
    threshold_artifact: ArtifactRef
    case_artifacts: tuple[ArtifactRef, ...] = Field(min_length=1)
    created_at: datetime
    thresholds_frozen_before_run: Literal[True] = True
    evaluator_hidden_until_context_freeze: Literal[True] = True
    full_chain_required: Literal[True] = True
    fixture_verdicts_allowed: Literal[False] = False


class Stage3FullChainSchemeResult(DomainModel):
    case_id: StableId
    scheme: ContextScheme
    loop_result: WritingLoopResult
    deterministic_rules: RuleAssessment | None = None
    evaluator_scores: tuple[EvaluatorScore, ...] = ()
    context_revision_count: int = Field(ge=0)
    compaction_count: int = Field(ge=0)
    memory_request_count: int = Field(ge=0)
    evidence_added_count: int = Field(ge=0)
    repair_count: int = Field(ge=0)
    rewrite_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    model_cost_usd: Decimal | None = Field(default=None, ge=Decimal("0"))
    model_cost_availability: ModelCostAvailability = ModelCostAvailability.UNKNOWN
    model_call_aggregates: tuple[ModelCallLedgerAggregate, ...] = ()

    @model_validator(mode="after")
    def validate_binding(self) -> Stage3FullChainSchemeResult:
        if self.loop_result.run_id.root.find(self.case_id.root) < 0:
            raise ValueError("full-chain loop run id must contain its case id")
        return self


class Stage3FullChainCaseResult(DomainModel):
    case_id: StableId
    schemes: tuple[Stage3FullChainSchemeResult, ...] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_schemes(self) -> Stage3FullChainCaseResult:
        if any(item.case_id != self.case_id for item in self.schemes):
            raise ValueError("full-chain scheme belongs to another case")
        if {item.scheme for item in self.schemes} != set(ContextScheme):
            raise ValueError("full-chain case must contain all three Context schemes")
        return self


class Stage3FullChainEvaluationReport(DomainModel):
    report_id: StableId
    manifest: Stage3FormalManifest
    cases: tuple[Stage3FullChainCaseResult, ...] = Field(min_length=1)
    generated_at: datetime
    semantic_pass_issued: Literal[False] = False


__all__ = [
    "Stage3FormalManifest",
    "Stage3FullChainCaseResult",
    "Stage3FullChainEvaluationReport",
    "Stage3FullChainSchemeResult",
]
