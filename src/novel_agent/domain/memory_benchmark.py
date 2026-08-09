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
    F_CLAIM_EVALUATOR = "F-CLAIM-EVALUATOR"


class PerGoldStageLossDiagnostic(DomainModel):
    """Evaluator-only accepted-evidence coverage across the frozen retrieval stages."""

    gold_id: StableId
    candidate: EvidenceStageCoverage
    rank_selected: EvidenceStageCoverage
    stage1_selected: EvidenceStageCoverage
    writer_ledger: EvidenceStageCoverage
    primary_failure: EvidenceStageFailure


class GoldBlindness(StrEnum):
    """Gold recoverability classification (semantic evaluation §5.7)."""

    BLIND_RECOVERABLE = "blind_recoverable"
    PLAN_DEPENDENT = "plan_dependent"
    HINDSIGHT_ONLY = "hindsight_only"


class GoldNeedSpec(DomainModel):
    """Evaluator-side specification of the needs a Gold conclusion requires.

    Need Recall is computed deterministically against these components
    (scope / entity labels / facet kinds), never by another LLM judgment.
    """

    gold_id: StableId
    blindness: GoldBlindness = GoldBlindness.BLIND_RECOVERABLE
    required_need_scopes: tuple[str, ...] = ()
    required_entities: tuple[str, ...] = ()
    required_facets: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_spec(self) -> GoldNeedSpec:
        if (
            not self.required_need_scopes
            and not self.required_entities
            and not self.required_facets
        ):
            raise ValueError("Gold need spec requires at least one component")
        if len(self.required_need_scopes) != len(set(self.required_need_scopes)):
            raise ValueError("Gold need spec scopes must be unique")
        if len(self.required_entities) != len(set(self.required_entities)):
            raise ValueError("Gold need spec entities must be unique")
        if len(self.required_facets) != len(set(self.required_facets)):
            raise ValueError("Gold need spec facets must be unique")
        return self


class SegmentAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class GoldNeedBinding(DomainModel):
    """Evaluator-only, deterministic Gold -> one Need -> its Ledger subset."""

    profile: BenchmarkInformationProfile
    gold_id: StableId
    blindness: GoldBlindness | None = None
    spec_hash: ArtifactId | None = None
    selected_need_id: StableId | None = None
    scope_hits: tuple[str, ...] = ()
    scope_misses: tuple[str, ...] = ()
    entity_hits: tuple[str, ...] = ()
    entity_misses: tuple[str, ...] = ()
    facet_hits: tuple[str, ...] = ()
    facet_misses: tuple[str, ...] = ()
    eligible_ledger_ids: tuple[StableId, ...] = ()
    full_need_match: bool = False
    tie_break_evidence: tuple[str, ...] = ()
    availability: SegmentAvailability = SegmentAvailability.AVAILABLE
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def validate_availability(self) -> GoldNeedBinding:
        if self.availability is SegmentAvailability.UNAVAILABLE:
            if self.unavailable_reason is None or self.selected_need_id is not None:
                raise ValueError("unavailable Gold binding requires reason and no selected Need")
        elif self.unavailable_reason is not None:
            raise ValueError("available Gold binding cannot carry unavailable reason")
        return self


class FiveSegmentReport(DomainModel):
    """Five-segment diagnostic view: goals, needs, evidence, completion, leakage.

    Leakage is reported separately and never folded into accuracy segments.
    ``planner_fallback_rate`` and ``grounding_success_rate`` are the Gate 1
    planning-health segments: the fallback share of Need-generation runs and
    the GROUNDED share of all grounded entity mentions.
    """

    plan_goals_total: int = Field(ge=0)
    plan_goals_covered: int = Field(ge=0)
    plan_goal_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    plan_goal_availability: SegmentAvailability = SegmentAvailability.AVAILABLE
    need_recall_total: int = Field(ge=0)
    need_recall_matched: int = Field(ge=0)
    need_recall: float | None = Field(default=None, ge=0.0, le=1.0)
    need_recall_availability: SegmentAvailability = SegmentAvailability.AVAILABLE
    evidence_recall: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_recall_total: int = Field(default=0, ge=0)
    evidence_recall_matched: int = Field(default=0, ge=0)
    evidence_recall_availability: SegmentAvailability = SegmentAvailability.AVAILABLE
    completion_accuracy: float | None = Field(default=None, ge=0.0, le=1.0)
    completion_gold_total: int = Field(default=0, ge=0)
    completion_weight_total: float = Field(default=0.0, ge=0.0)
    completion_availability: SegmentAvailability = SegmentAvailability.AVAILABLE
    future_leakage_count: int = Field(ge=0)
    plan_citation_count: int = Field(ge=0)
    plan_leakage_count: int = Field(ge=0)
    planner_fallback_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    grounding_success_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    planner_artifact_ref: ArtifactRef | None = None
    planner_fallback_reason: str | None = None
    grounded_status_counts: tuple[int, int, int] = (0, 0, 0)
    bindings: tuple[GoldNeedBinding, ...] = ()
    missing_spec_gold_ids: tuple[StableId, ...] = ()
    legacy_plan_obligation_coverage: float | None = None
    legacy_plan_obligation_unavailable_reason: str = "NOT_APPLICABLE_STRICT_D9"

    @model_validator(mode="after")
    def validate_consistency(self) -> FiveSegmentReport:
        if self.planner_fallback_rate == 1.0 and self.planner_fallback_reason is None:
            raise ValueError("Planner fallback report requires its typed reason")
        if self.planner_fallback_rate == 0.0 and self.planner_fallback_reason is not None:
            raise ValueError("non-fallback Planner report cannot carry a fallback reason")
        grounded_total = sum(self.grounded_status_counts)
        expected_grounding = (
            self.grounded_status_counts[0] / grounded_total if grounded_total else 1.0
        )
        if abs(self.grounding_success_rate - expected_grounding) > 1e-12:
            raise ValueError("Planner grounding rate/counts are inconsistent")
        if self.plan_goals_covered > self.plan_goals_total:
            raise ValueError("covered plan goals cannot exceed total plan goals")
        if self.need_recall_matched > self.need_recall_total:
            raise ValueError("matched need components cannot exceed total components")
        if self.plan_leakage_count > self.plan_citation_count:
            raise ValueError("plan leakage cannot exceed plan citations")
        if self.evidence_recall_matched > self.evidence_recall_total:
            raise ValueError("matched evidence cannot exceed evidence denominator")
        if self.completion_gold_total == 0 and self.completion_weight_total != 0.0:
            raise ValueError("completion denominator count/weight are inconsistent")
        availability_pairs = (
            (self.plan_goal_coverage, self.plan_goal_availability),
            (self.need_recall, self.need_recall_availability),
            (self.evidence_recall, self.evidence_recall_availability),
            (self.completion_accuracy, self.completion_availability),
        )
        if any(
            (metric is None) != (availability is SegmentAvailability.UNAVAILABLE)
            for metric, availability in availability_pairs
        ):
            raise ValueError("segment metric availability is inconsistent")
        return self


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
    five_segments: FiveSegmentReport | None = None
    schema_version: SchemaVersion = SchemaVersion("2.0.0")

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
    aggregation_manifest_ref: ArtifactRef | None = None
    aggregation_manifest_hash: ArtifactId | None = None
    current_state: GateMetricAxisResult
    operational_plan: GateMetricAxisResult
    historical: GateMetricAxisResult
    checkpoint_metrics: tuple[CheckpointGateMetricReport, ...]
    trace_complete: bool
    contradiction_free: bool
    gate_passed: bool

    @model_validator(mode="after")
    def validate_gate_signature(self) -> MemoryBenchmarkUnifiedReport:
        if (self.aggregation_manifest_ref is None) != (self.aggregation_manifest_hash is None):
            raise ValueError("aggregation manifest ref/hash must appear together")
        if (
            self.aggregation_manifest_ref is not None
            and self.aggregation_manifest_ref.artifact_id != self.aggregation_manifest_hash
        ):
            raise ValueError("aggregation manifest ref/hash mismatch")
        if self.gate_passed and not self.formal_contract_validated:
            raise ValueError("Gate M4 cannot pass without the formal five-checkpoint contract")
        return self
