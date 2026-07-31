"""Profile-separated, contract-bound Stage 2M Gate M4 aggregation."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.base import DomainModel
from novel_agent.domain.memory_benchmark import (
    AcceptedEvidenceContract,
    BenchmarkInformationProfile,
    CheckpointGateMetricReport,
    ContextAssemblyStatus,
    EvaluatorManifestContract,
    EvidenceLedger,
    GateMetricAxis,
    GateMetricAxisResult,
    GoldMatchStatus,
    GoldMetricContract,
    GoldMetricDescriptor,
    GoldType,
    MemoryBenchmarkCaseArmReport,
    MemoryBenchmarkUnifiedReport,
    PerGoldComparison,
)
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import content_id
from novel_agent.services.gold_evidence_matching import GoldEvidenceMatcher
from novel_agent.services.memory_benchmark_metric_contracts import (
    GATE_METRIC_FORMULA_HASH,
    GATE_METRIC_FORMULA_VERSION,
    GATE_METRIC_THRESHOLDS,
)

_STATUS_SCORE = {
    GoldMatchStatus.HIT: 1.0,
    GoldMatchStatus.PARTIAL: 0.5,
    GoldMatchStatus.MISS: 0.0,
    GoldMatchStatus.UNTRACEABLE: 0.0,
    GoldMatchStatus.CONTRADICTS: 0.0,
}
_CURRENT_TYPES = {
    GoldType.CURRENT_STATE,
    GoldType.RELATIONSHIP_EMOTION,
    GoldType.KNOWLEDGE_BOUNDARY,
    GoldType.OBJECT_CONTINUITY,
}
_HISTORICAL_TYPES = {GoldType.CAUSAL_HISTORY, GoldType.LONG_RANGE_CALLBACK}
_OPERATIONAL_KINDS = {"operational_constraint", "plan_obligation"}
_ModelT = TypeVar("_ModelT", bound=DomainModel)
_FORMAL_GATE_CONTRACT_VERSION = "stage2m_wp7_arm_a.v1"
_EXPECTED_CASE_BY_CHECKPOINT = {
    20: "ZTJ-P001",
    40: "ZTJ-P002",
    60: "ZTJ-P003",
    80: "ZTJ-P004",
    95: "ZTJ-P005",
}
_EXPECTED_DENOMINATORS = {
    BenchmarkInformationProfile.VISIBLE_AT_CUTOFF: {
        "applicable": (47, 0.0),
        GateMetricAxis.CURRENT_STATE_ACCURACY: (36, 100.0),
        GateMetricAxis.OPERATIONAL_PLAN_COVERAGE: (26, 71.0),
        GateMetricAxis.KEY_HISTORICAL_EVIDENCE_RECALL: (9, 29.0),
    },
    BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED: {
        "applicable": (72, 0.0),
        GateMetricAxis.CURRENT_STATE_ACCURACY: (36, 100.0),
        GateMetricAxis.OPERATIONAL_PLAN_COVERAGE: (51, 96.0),
        GateMetricAxis.KEY_HISTORICAL_EVIDENCE_RECALL: (9, 29.0),
    },
}
_FORMAL_GATE_CONTRACT_HASH = content_id(
    {
        "version": _FORMAL_GATE_CONTRACT_VERSION,
        "arm": "A",
        "case_by_checkpoint": _EXPECTED_CASE_BY_CHECKPOINT,
        "denominators": {
            profile.value: {
                str(axis.value if isinstance(axis, GateMetricAxis) else axis): value
                for axis, value in denominators.items()
            }
            for profile, denominators in _EXPECTED_DENOMINATORS.items()
        },
    }
)


@dataclass(frozen=True)
class _ResolvedGold:
    comparison: PerGoldComparison
    descriptor: GoldMetricDescriptor
    accepted: AcceptedEvidenceContract
    historical_recall: float


class MemoryBenchmarkReporter:
    version = "stage2m_unified_report.v2"

    def __init__(
        self,
        *,
        artifact_reader: Callable[[ArtifactRef], bytes],
        enforce_formal_contract: bool = True,
    ) -> None:
        self._read_artifact = artifact_reader
        self._matcher = GoldEvidenceMatcher()
        self._enforce_formal_contract = enforce_formal_contract

    def aggregate(
        self,
        *,
        profile: BenchmarkInformationProfile,
        cases: tuple[MemoryBenchmarkCaseArmReport, ...],
    ) -> MemoryBenchmarkUnifiedReport:
        if not cases:
            raise ValueError("Gate M4 aggregation requires at least one case")
        if any(item.evaluation.profile is not profile for item in cases):
            raise ValueError("unified report cannot mix information profiles")
        if self._enforce_formal_contract:
            self._validate_case_matrix(cases)
        if len({item.arm for item in cases}) != 1:
            raise ValueError("Gate M4 aggregation must be computed separately per arm")
        identities = {(item.case_id, item.arm) for item in cases}
        if len(identities) != len(cases):
            raise ValueError("unified report contains duplicate case/arm results")

        resolved_by_case: list[tuple[MemoryBenchmarkCaseArmReport, tuple[_ResolvedGold, ...]]] = []
        seen_gold: set[object] = set()
        for case in cases:
            evaluation = case.evaluation
            if (
                evaluation.gate_metric_formula_version != GATE_METRIC_FORMULA_VERSION
                or evaluation.gate_metric_formula_hash != GATE_METRIC_FORMULA_HASH
            ):
                raise ValueError("Gate metric formula version/hash mismatch")
            manifest = self._read_model(
                evaluation.evaluator_manifest_ref,
                EvaluatorManifestContract,
                expected_hash=evaluation.evaluator_manifest_hash,
            )
            if manifest.evaluator_manifest_id != evaluation.evaluator_manifest_id:
                raise ValueError("evaluator manifest id mismatch")
            ledger = self._read_model(case.writer_evidence_ledger_ref, EvidenceLedger)
            resolved = tuple(
                self._resolve_comparison(case, comparison, ledger)
                for comparison in evaluation.comparisons
            )
            if not resolved:
                raise ValueError("Gate M4 case has no applicable Gold comparisons")
            gold_ids = {item.comparison.gold_id for item in resolved}
            if len(gold_ids) != len(resolved):
                raise ValueError("Gate M4 case contains duplicate Gold comparisons")
            if set(manifest.gold_ids) != gold_ids:
                raise ValueError("evaluator manifest Gold ids do not match comparisons")
            if seen_gold.intersection(gold_ids):
                raise ValueError("Gate M4 report repeats a Gold id across checkpoints")
            seen_gold.update(gold_ids)
            expected_diagnostics = {item.gold_id for item in evaluation.stage_loss_diagnostics}
            if expected_diagnostics and expected_diagnostics != gold_ids:
                raise ValueError("stage-loss diagnostics do not cover the Gate comparison set")
            resolved_by_case.append((case, resolved))

        all_gold = tuple(item for _case, items in resolved_by_case for item in items)
        current = self._axis(
            GateMetricAxis.CURRENT_STATE_ACCURACY,
            tuple(
                item
                for item in all_gold
                if GateMetricAxis.CURRENT_STATE_ACCURACY
                in self.axes_for_descriptor(item.descriptor)
            ),
        )
        operational = self._axis(
            GateMetricAxis.OPERATIONAL_PLAN_COVERAGE,
            tuple(
                item
                for item in all_gold
                if GateMetricAxis.OPERATIONAL_PLAN_COVERAGE
                in self.axes_for_descriptor(item.descriptor)
            ),
        )
        historical = self._axis(
            GateMetricAxis.KEY_HISTORICAL_EVIDENCE_RECALL,
            tuple(
                item
                for item in all_gold
                if GateMetricAxis.KEY_HISTORICAL_EVIDENCE_RECALL
                in self.axes_for_descriptor(item.descriptor)
            ),
        )
        if self._enforce_formal_contract:
            self._validate_denominators(
                profile=profile,
                all_gold=all_gold,
                axes=(current, operational, historical),
            )
        checkpoint_metrics = tuple(
            self._checkpoint(case, resolved) for case, resolved in resolved_by_case
        )
        count = len(cases)
        contradiction_count = sum(
            item.comparison.status is GoldMatchStatus.CONTRADICTS for item in all_gold
        )
        untraceable_count = sum(
            item.comparison.status is GoldMatchStatus.UNTRACEABLE for item in all_gold
        )
        trace_complete = untraceable_count == 0
        contradiction_free = contradiction_count == 0
        return MemoryBenchmarkUnifiedReport(
            report_version=self.version,
            profile=profile,
            cases=tuple(sorted(cases, key=lambda item: (item.checkpoint_chapter, item.arm))),
            case_count=count,
            ready_count=sum(item.assembly_status is ContextAssemblyStatus.READY for item in cases),
            comparable_count=sum(item.comparable for item in cases),
            macro_weighted_coverage=(
                sum(item.evaluation.weighted_coverage for item in cases) / count
            ),
            macro_mandatory_hit_rate=(
                sum(item.evaluation.mandatory_hit_rate for item in cases) / count
            ),
            contradiction_rate=contradiction_count / len(all_gold),
            untraceable_rate=untraceable_count / len(all_gold),
            gate_metric_formula_version=GATE_METRIC_FORMULA_VERSION,
            gate_metric_formula_hash=GATE_METRIC_FORMULA_HASH,
            gate_contract_version=_FORMAL_GATE_CONTRACT_VERSION,
            gate_contract_hash=_FORMAL_GATE_CONTRACT_HASH,
            formal_contract_validated=self._enforce_formal_contract,
            current_state=current,
            operational_plan=operational,
            historical=historical,
            checkpoint_metrics=checkpoint_metrics,
            trace_complete=trace_complete,
            contradiction_free=contradiction_free,
            gate_passed=(
                self._enforce_formal_contract
                and current.passed
                and operational.passed
                and historical.passed
                and trace_complete
                and contradiction_free
            ),
        )

    @staticmethod
    def _validate_case_matrix(cases: tuple[MemoryBenchmarkCaseArmReport, ...]) -> None:
        if len(cases) != len(_EXPECTED_CASE_BY_CHECKPOINT):
            raise ValueError("formal Gate M4 report requires exactly five Arm A checkpoints")
        if any(case.arm != "A" for case in cases):
            raise ValueError("formal Gate M4 report accepts Arm A only")
        actual = {case.checkpoint_chapter: case.case_id.root for case in cases}
        if actual != _EXPECTED_CASE_BY_CHECKPOINT:
            raise ValueError("formal Gate M4 checkpoint/case identity mismatch")
        freeze_ids = {case.evaluation.freeze_receipt_id for case in cases}
        if len(freeze_ids) != len(cases):
            raise ValueError("formal Gate M4 cases must have distinct freeze receipts")
        immutable_run_identities = {
            (
                case.code_version,
                case.run_config_hash,
                case.benchmark_contract_hash,
                case.matcher_version,
                case.writer_token_budget,
                case.evidence_ledger_token_budget,
            )
            for case in cases
        }
        if len(immutable_run_identities) != 1:
            raise ValueError("formal Gate M4 cases do not share one frozen run identity")
        if cases[0].matcher_version != GoldEvidenceMatcher.version:
            raise ValueError("formal Gate M4 matcher identity mismatch")
        for case in cases:
            expected_manifest_id = (
                f"evaluator-manifest.{case.case_id.root}.{case.evaluation.profile.value}"
            )
            if case.evaluation.evaluator_manifest_id.root != expected_manifest_id:
                raise ValueError("formal Gate M4 evaluator manifest identity mismatch")

    @classmethod
    def _validate_denominators(
        cls,
        *,
        profile: BenchmarkInformationProfile,
        all_gold: tuple[_ResolvedGold, ...],
        axes: tuple[GateMetricAxisResult, ...],
    ) -> None:
        expected = _EXPECTED_DENOMINATORS[profile]
        if len(all_gold) != expected["applicable"][0]:
            raise ValueError("formal Gate M4 applicable-Gold denominator drift")
        by_axis = {item.axis: item for item in axes}
        for axis in GateMetricAxis:
            expected_count, expected_weight = expected[axis]
            actual = by_axis[axis]
            if (
                actual.item_count != expected_count
                or abs(actual.denominator_weight - expected_weight) > 1e-12
            ):
                raise ValueError(f"formal Gate M4 denominator drift: {axis.value}")

    def _resolve_comparison(
        self,
        case: MemoryBenchmarkCaseArmReport,
        comparison: PerGoldComparison,
        ledger: EvidenceLedger,
    ) -> _ResolvedGold:
        evaluation = case.evaluation
        descriptor = self._read_model(
            comparison.gold_metric_descriptor_ref,
            GoldMetricDescriptor,
            expected_hash=comparison.gold_metric_descriptor_hash,
        )
        if (
            descriptor.gold_id != comparison.gold_id
            or descriptor.weight != comparison.weight
            or descriptor.mandatory != comparison.mandatory
        ):
            raise ValueError("comparison and Gold metric descriptor disagree")
        if (
            descriptor.evaluator_manifest_id != evaluation.evaluator_manifest_id
            or descriptor.evaluator_manifest_hash != evaluation.evaluator_manifest_hash
        ):
            raise ValueError("Gold metric descriptor evaluator manifest identity mismatch")
        if evaluation.profile not in descriptor.applicable_profiles:
            raise ValueError("comparison is not applicable to the report profile")
        gold_contract = self._read_model(
            descriptor.gold_contract_ref,
            GoldMetricContract,
            expected_hash=descriptor.gold_contract_hash,
        )
        if (
            gold_contract.gold_id != descriptor.gold_id
            or gold_contract.gold_type is not descriptor.gold_type
            or gold_contract.gold_kind != descriptor.gold_kind
            or gold_contract.weight != descriptor.weight
            or gold_contract.mandatory != descriptor.mandatory
            or gold_contract.applicable_profiles != descriptor.applicable_profiles
            or gold_contract.accepted_evidence_contract_ref
            != descriptor.accepted_evidence_contract_ref
            or gold_contract.accepted_evidence_contract_hash
            != descriptor.accepted_evidence_contract_hash
        ):
            raise ValueError("Gold metric descriptor and Gold contract disagree")
        accepted = self._read_model(
            descriptor.accepted_evidence_contract_ref,
            AcceptedEvidenceContract,
            expected_hash=descriptor.accepted_evidence_contract_hash,
        )
        if accepted.gold_id != descriptor.gold_id:
            raise ValueError("accepted evidence contract Gold id mismatch")
        if accepted.matcher_version != self._matcher.version:
            raise ValueError("accepted evidence contract matcher version mismatch")
        recalls = tuple(
            self._matcher.text_reference_recall(alternative, ledger)
            for alternative in accepted.alternatives
            if alternative.evidence_refs
        )
        historical_recall = max(recalls) if recalls else 0.0
        if case.assembly_status is not ContextAssemblyStatus.READY:
            historical_recall = 0.0
            if comparison.status is not GoldMatchStatus.MISS:
                raise ValueError("typed failure comparisons must all be MISS")
        return _ResolvedGold(
            comparison=comparison,
            descriptor=descriptor,
            accepted=accepted,
            historical_recall=historical_recall,
        )

    def _read_model(
        self,
        ref: ArtifactRef,
        model: type[_ModelT],
        *,
        expected_hash: object | None = None,
    ) -> _ModelT:
        if expected_hash is not None and ref.artifact_id != expected_hash:
            raise ValueError("content-addressed ref/hash mismatch")
        payload = self._read_artifact(ref)
        if len(payload) != ref.byte_length or sha256_id(payload) != ref.artifact_id:
            raise ValueError("content-addressed evaluator artifact integrity failure")
        return model.model_validate_json(payload)

    @staticmethod
    def axes_for_descriptor(descriptor: GoldMetricDescriptor) -> tuple[GateMetricAxis, ...]:
        axes: list[GateMetricAxis] = []
        if descriptor.gold_type in _CURRENT_TYPES:
            axes.append(GateMetricAxis.CURRENT_STATE_ACCURACY)
        if (
            descriptor.gold_kind in _OPERATIONAL_KINDS
            or descriptor.gold_type is GoldType.PLAN_OBLIGATION
        ):
            axes.append(GateMetricAxis.OPERATIONAL_PLAN_COVERAGE)
        if descriptor.gold_type in _HISTORICAL_TYPES:
            axes.append(GateMetricAxis.KEY_HISTORICAL_EVIDENCE_RECALL)
        return tuple(axes)

    @staticmethod
    def _axis(
        axis: GateMetricAxis,
        items: tuple[_ResolvedGold, ...],
    ) -> GateMetricAxisResult:
        if not items:
            raise ValueError(f"Gate metric denominator is empty without N/A contract: {axis.value}")
        denominator = sum(item.descriptor.weight for item in items)
        numerator = sum(
            item.descriptor.weight
            * (
                item.historical_recall
                if axis is GateMetricAxis.KEY_HISTORICAL_EVIDENCE_RECALL
                else _STATUS_SCORE[item.comparison.status]
            )
            for item in items
        )
        threshold = GATE_METRIC_THRESHOLDS[axis.value]
        value = numerator / denominator
        return GateMetricAxisResult(
            axis=axis,
            item_count=len(items),
            denominator_weight=denominator,
            weighted_score_sum=numerator,
            value=value,
            threshold=threshold,
            passed=value >= threshold,
        )

    def _checkpoint(
        self,
        case: MemoryBenchmarkCaseArmReport,
        items: tuple[_ResolvedGold, ...],
    ) -> CheckpointGateMetricReport:
        mandatory_counts = Counter(
            item.comparison.status for item in items if item.comparison.mandatory
        )
        return CheckpointGateMetricReport(
            case_id=case.case_id,
            checkpoint_chapter=case.checkpoint_chapter,
            arm=case.arm,
            assembly_status=case.assembly_status,
            typed_failure=case.assembly_status is not ContextAssemblyStatus.READY,
            applicable_gold_count=len(items),
            current_state=self._axis(
                GateMetricAxis.CURRENT_STATE_ACCURACY,
                tuple(
                    item
                    for item in items
                    if GateMetricAxis.CURRENT_STATE_ACCURACY
                    in self.axes_for_descriptor(item.descriptor)
                ),
            ),
            operational_plan=self._axis(
                GateMetricAxis.OPERATIONAL_PLAN_COVERAGE,
                tuple(
                    item
                    for item in items
                    if GateMetricAxis.OPERATIONAL_PLAN_COVERAGE
                    in self.axes_for_descriptor(item.descriptor)
                ),
            ),
            historical=self._axis(
                GateMetricAxis.KEY_HISTORICAL_EVIDENCE_RECALL,
                tuple(
                    item
                    for item in items
                    if GateMetricAxis.KEY_HISTORICAL_EVIDENCE_RECALL
                    in self.axes_for_descriptor(item.descriptor)
                ),
            ),
            mandatory_status_counts=dict(mandatory_counts),
        )

    @staticmethod
    def cross_profile_delta(
        visible: MemoryBenchmarkUnifiedReport,
        planned: MemoryBenchmarkUnifiedReport,
    ) -> dict[str, object]:
        if visible.profile is not BenchmarkInformationProfile.VISIBLE_AT_CUTOFF:
            raise ValueError("visible report has the wrong profile")
        if planned.profile is not BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED:
            raise ValueError("planned report has the wrong profile")
        return {
            "report_version": "stage2m_cross_profile.v2",
            "visible_at_cutoff_case_count": visible.case_count,
            "author_plan_conditioned_case_count": planned.case_count,
            "current_state_accuracy_delta": planned.current_state.value
            - visible.current_state.value,
            "operational_plan_coverage_delta": planned.operational_plan.value
            - visible.operational_plan.value,
            "historical_evidence_recall_delta": planned.historical.value - visible.historical.value,
        }
