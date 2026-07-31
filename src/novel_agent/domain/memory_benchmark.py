"""Stage 2M public task, writer context, and per-Gold evaluation contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from novel_agent.domain import writer_context as _public
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.text import EvidenceRef


class GoldType(StrEnum):
    CURRENT_STATE = "CURRENT_STATE"
    RELATIONSHIP_EMOTION = "RELATIONSHIP_EMOTION"
    CAUSAL_HISTORY = "CAUSAL_HISTORY"
    KNOWLEDGE_BOUNDARY = "KNOWLEDGE_BOUNDARY"
    PLAN_OBLIGATION = "PLAN_OBLIGATION"
    LONG_RANGE_CALLBACK = "LONG_RANGE_CALLBACK"
    OBJECT_CONTINUITY = "OBJECT_CONTINUITY"


class EvidenceSet(DomainModel):
    """One accepted, conjunctive evidence alternative for a Gold item."""

    evidence_set_id: StableId
    evidence_refs: tuple[EvidenceRef, ...] = ()
    plan_node_ids: tuple[StableId, ...] = ()
    component_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_support(self) -> EvidenceSet:
        if not self.evidence_refs and not self.plan_node_ids:
            raise ValueError("accepted evidence set cannot be empty")
        return self


BenchmarkInformationProfile = _public.BenchmarkInformationProfile
BenchmarkTaskContract = _public.BenchmarkTaskContract
PublicCheckpointPayload = _public.PublicCheckpointPayload
WriterContextSection = _public.WriterContextSection
WriterContextValidity = _public.WriterContextValidity
WriterContextItem = _public.WriterContextItem
ContextGap = _public.ContextGap
EvidenceLedgerEntry = _public.EvidenceLedgerEntry
EvidenceLedger = _public.EvidenceLedger
ContextAssemblyStatus = _public.ContextAssemblyStatus
WriterContextBudgetReport = _public.WriterContextBudgetReport
ContextLineage = _public.ContextLineage
WriterContextPackage = _public.WriterContextPackage
FreezeReceipt = _public.FreezeReceipt


class GoldMatchStatus(StrEnum):
    HIT = "HIT"
    PARTIAL = "PARTIAL"
    MISS = "MISS"
    CONTRADICTS = "CONTRADICTS"
    UNTRACEABLE = "UNTRACEABLE"


class AcceptedEvidenceContract(DomainModel):
    """Evaluator-only immutable alternatives used by the locked evidence matcher."""

    contract_version: str = Field(min_length=1)
    gold_id: StableId
    matcher_version: str = Field(min_length=1)
    alternatives: tuple[EvidenceSet, ...]

    @model_validator(mode="after")
    def validate_alternatives(self) -> AcceptedEvidenceContract:
        if not self.alternatives:
            raise ValueError("accepted evidence contract requires at least one alternative")
        if len({item.evidence_set_id for item in self.alternatives}) != len(self.alternatives):
            raise ValueError("accepted evidence contract alternatives must be unique")
        return self


class GoldMetricContract(DomainModel):
    """Evaluator-only immutable Gold fields required to recompute Gate M4."""

    contract_version: str = Field(min_length=1)
    gold_id: StableId
    gold_type: GoldType
    gold_kind: Literal["observed_use", "operational_constraint", "plan_obligation"]
    weight: float = Field(gt=0)
    mandatory: bool
    applicable_profiles: tuple[BenchmarkInformationProfile, ...]
    accepted_evidence_contract_ref: ArtifactRef
    accepted_evidence_contract_hash: ArtifactId

    @model_validator(mode="after")
    def validate_identity(self) -> GoldMetricContract:
        if not self.applicable_profiles:
            raise ValueError("Gold metric contract requires an applicable profile")
        if len(self.applicable_profiles) != len(set(self.applicable_profiles)):
            raise ValueError("Gold metric contract profiles must be unique")
        if self.accepted_evidence_contract_ref.artifact_id != self.accepted_evidence_contract_hash:
            raise ValueError("accepted evidence contract ref/hash mismatch")
        return self


class GoldMetricDescriptor(DomainModel):
    """Content-addressed evaluator descriptor bound to one manifest and Gold contract."""

    descriptor_version: str = Field(min_length=1)
    gold_id: StableId
    gold_contract_ref: ArtifactRef
    gold_contract_hash: ArtifactId
    gold_type: GoldType
    gold_kind: Literal["observed_use", "operational_constraint", "plan_obligation"]
    weight: float = Field(gt=0)
    mandatory: bool
    applicable_profiles: tuple[BenchmarkInformationProfile, ...]
    accepted_evidence_contract_ref: ArtifactRef
    accepted_evidence_contract_hash: ArtifactId
    evaluator_manifest_id: StableId
    evaluator_manifest_hash: ArtifactId

    @model_validator(mode="after")
    def validate_refs(self) -> GoldMetricDescriptor:
        if self.gold_contract_ref.artifact_id != self.gold_contract_hash:
            raise ValueError("Gold contract ref/hash mismatch")
        if self.accepted_evidence_contract_ref.artifact_id != self.accepted_evidence_contract_hash:
            raise ValueError("accepted evidence contract ref/hash mismatch")
        if not self.applicable_profiles:
            raise ValueError("Gold metric descriptor requires an applicable profile")
        if len(self.applicable_profiles) != len(set(self.applicable_profiles)):
            raise ValueError("Gold metric descriptor profiles must be unique")
        return self


class EvaluatorManifestContract(DomainModel):
    """Content-addressed identity of the private Gold set revealed after freeze."""

    manifest_version: str = Field(min_length=1)
    evaluator_manifest_id: StableId
    gold_ids: tuple[StableId, ...]

    @model_validator(mode="after")
    def validate_gold_ids(self) -> EvaluatorManifestContract:
        if not self.gold_ids:
            raise ValueError("evaluator manifest requires at least one Gold id")
        if len(self.gold_ids) != len(set(self.gold_ids)):
            raise ValueError("evaluator manifest Gold ids must be unique")
        return self


class ContentAddressedGoldMetricDescriptor(DomainModel):
    descriptor: GoldMetricDescriptor
    descriptor_ref: ArtifactRef
    descriptor_hash: ArtifactId

    @model_validator(mode="after")
    def validate_ref(self) -> ContentAddressedGoldMetricDescriptor:
        if self.descriptor_ref.artifact_id != self.descriptor_hash:
            raise ValueError("Gold metric descriptor ref/hash mismatch")
        return self


class PerGoldComparison(DomainModel):
    gold_id: StableId
    status: GoldMatchStatus
    weight: float = Field(gt=0)
    mandatory: bool
    gold_metric_descriptor_ref: ArtifactRef
    gold_metric_descriptor_hash: ArtifactId
    matched_context_item_ids: tuple[StableId, ...] = ()
    matched_evidence_ledger_ids: tuple[StableId, ...] = ()
    supported_components: tuple[str, ...] = ()
    missing_components: tuple[str, ...] = ()
    explanation: str = Field(min_length=1)
    verifier_receipt_ref: ArtifactRef | None = None

    @model_validator(mode="after")
    def validate_descriptor_ref(self) -> PerGoldComparison:
        if self.gold_metric_descriptor_ref.artifact_id != self.gold_metric_descriptor_hash:
            raise ValueError("per-Gold descriptor ref/hash mismatch")
        return self


class EvidenceStageCoverage(DomainModel):
    accepted_reference_count: int = Field(ge=0)
    matched_reference_count: int = Field(ge=0)
    complete_alternative_ids: tuple[StableId, ...] = ()
    partial_alternative_ids: tuple[StableId, ...] = ()

    @model_validator(mode="after")
    def validate_counts(self) -> EvidenceStageCoverage:
        if self.matched_reference_count > self.accepted_reference_count:
            raise ValueError("matched evidence references cannot exceed accepted references")
        return self


class EvidenceStageFailure(StrEnum):
    COMPLETE = "COMPLETE"
    F_ASSEMBLY = "F-ASSEMBLY"
    F_RANK = "F-RANK"
    F_NEED_ROUTE_RETRIEVE = "F-NEED_ROUTE_RETRIEVE"


class PerGoldStageLossDiagnostic(DomainModel):
    """Evaluator-only accepted-evidence coverage across the frozen retrieval stages."""

    gold_id: StableId
    candidate: EvidenceStageCoverage
    rank_selected: EvidenceStageCoverage
    stage1_selected: EvidenceStageCoverage
    writer_ledger: EvidenceStageCoverage
    primary_failure: EvidenceStageFailure


class MemoryBenchmarkEvaluationReport(DomainModel):
    evaluator_version: str = Field(min_length=1)
    profile: BenchmarkInformationProfile
    comparisons: tuple[PerGoldComparison, ...]
    weighted_coverage: float = Field(ge=0.0, le=1.0)
    mandatory_hit_rate: float = Field(ge=0.0, le=1.0)
    contradiction_rate: float = Field(ge=0.0, le=1.0)
    untraceable_rate: float = Field(ge=0.0, le=1.0)
    freeze_receipt_id: StableId
    evaluator_manifest_id: StableId
    evaluator_manifest_ref: ArtifactRef
    evaluator_manifest_hash: ArtifactId
    gate_metric_formula_version: str = Field(min_length=1)
    gate_metric_formula_hash: ArtifactId
    stage_loss_diagnostics: tuple[PerGoldStageLossDiagnostic, ...] = ()
    schema_version: SchemaVersion = SchemaVersion("1.0.0")

    @model_validator(mode="after")
    def validate_manifest_ref(self) -> MemoryBenchmarkEvaluationReport:
        if self.evaluator_manifest_ref.artifact_id != self.evaluator_manifest_hash:
            raise ValueError("evaluator manifest ref/hash mismatch")
        return self


class MemoryBenchmarkCaseArmReport(DomainModel):
    case_id: StableId
    checkpoint_chapter: int = Field(ge=0)
    arm: Literal["A", "B", "C"]
    code_version: str = Field(min_length=1)
    run_config_hash: ArtifactId
    benchmark_contract_hash: ArtifactId
    matcher_version: str = Field(min_length=1)
    writer_token_budget: int = Field(ge=1)
    evidence_ledger_token_budget: int = Field(ge=1)
    assembly_status: ContextAssemblyStatus
    writer_tokens: int = Field(ge=0)
    evidence_tokens: int = Field(ge=0)
    selected_unit_count: int = Field(ge=0)
    comparable: bool
    writer_evidence_ledger_ref: ArtifactRef
    evaluation: MemoryBenchmarkEvaluationReport


class GateMetricAxis(StrEnum):
    CURRENT_STATE_ACCURACY = "current_state_accuracy"
    OPERATIONAL_PLAN_COVERAGE = "operational_plan_coverage"
    KEY_HISTORICAL_EVIDENCE_RECALL = "key_historical_evidence_recall"


class GateMetricAxisResult(DomainModel):
    axis: GateMetricAxis
    item_count: int = Field(ge=0)
    denominator_weight: float = Field(gt=0)
    weighted_score_sum: float = Field(ge=0)
    value: float = Field(ge=0.0, le=1.0)
    threshold: float = Field(ge=0.0, le=1.0)
    passed: bool

    @model_validator(mode="after")
    def validate_value(self) -> GateMetricAxisResult:
        expected = self.weighted_score_sum / self.denominator_weight
        if abs(self.value - expected) > 1e-12:
            raise ValueError("Gate metric value does not match its weighted numerator")
        if self.passed != (self.value >= self.threshold):
            raise ValueError("Gate metric pass flag does not match its threshold")
        return self


class CheckpointGateMetricReport(DomainModel):
    case_id: StableId
    checkpoint_chapter: int = Field(ge=0)
    arm: Literal["A", "B", "C"]
    assembly_status: ContextAssemblyStatus
    typed_failure: bool
    applicable_gold_count: int = Field(ge=0)
    current_state: GateMetricAxisResult
    operational_plan: GateMetricAxisResult
    historical: GateMetricAxisResult
    mandatory_status_counts: dict[GoldMatchStatus, int]


class MemoryBenchmarkUnifiedReport(DomainModel):
    report_version: str = Field(min_length=1)
    profile: BenchmarkInformationProfile
    cases: tuple[MemoryBenchmarkCaseArmReport, ...]
    case_count: int = Field(ge=0)
    ready_count: int = Field(ge=0)
    comparable_count: int = Field(ge=0)
    macro_weighted_coverage: float = Field(ge=0.0, le=1.0)
    macro_mandatory_hit_rate: float = Field(ge=0.0, le=1.0)
    contradiction_rate: float = Field(ge=0.0, le=1.0)
    untraceable_rate: float = Field(ge=0.0, le=1.0)
    gate_metric_formula_version: str = Field(min_length=1)
    gate_metric_formula_hash: ArtifactId
    gate_contract_version: str = Field(min_length=1)
    gate_contract_hash: ArtifactId
    formal_contract_validated: bool
    current_state: GateMetricAxisResult
    operational_plan: GateMetricAxisResult
    historical: GateMetricAxisResult
    checkpoint_metrics: tuple[CheckpointGateMetricReport, ...]
    trace_complete: bool
    contradiction_free: bool
    gate_passed: bool

    @model_validator(mode="after")
    def validate_gate_signature(self) -> MemoryBenchmarkUnifiedReport:
        if self.gate_passed and not self.formal_contract_validated:
            raise ValueError("Gate M4 cannot pass without the formal five-checkpoint contract")
        return self
