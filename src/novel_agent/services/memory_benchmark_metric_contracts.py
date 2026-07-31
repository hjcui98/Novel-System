"""Evaluator-only content-addressed contracts and locked Gate M4 formula identity."""

from __future__ import annotations

from collections.abc import Iterable

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import GoldItem
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.memory_benchmark import (
    AcceptedEvidenceContract,
    ContentAddressedGoldMetricDescriptor,
    EvaluatorManifestContract,
    GoldMetricContract,
    GoldMetricDescriptor,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.services.gold_evidence_matching import GoldEvidenceMatcher

VERSION = SchemaVersion("1.0.0")
GATE_METRIC_FORMULA_VERSION = "gate_metric_formula.v1"
GATE_METRIC_THRESHOLDS: dict[str, float] = {
    "current_state_accuracy": 0.95,
    "operational_plan_coverage": 0.95,
    "key_historical_evidence_recall": 0.90,
}
GATE_METRIC_FORMULA = {
    "version": GATE_METRIC_FORMULA_VERSION,
    "status_score": {
        "HIT": 1.0,
        "PARTIAL": 0.5,
        "MISS": 0.0,
        "UNTRACEABLE": 0.0,
        "CONTRADICTS": 0.0,
    },
    "current_state_gold_types": [
        "CURRENT_STATE",
        "RELATIONSHIP_EMOTION",
        "KNOWLEDGE_BOUNDARY",
        "OBJECT_CONTINUITY",
    ],
    "operational_gold_kinds": ["operational_constraint", "plan_obligation"],
    "operational_gold_types": ["PLAN_OBLIGATION"],
    "historical_gold_types": ["CAUSAL_HISTORY", "LONG_RANGE_CALLBACK"],
    "aggregation": "per-profile-weight-micro",
    "historical_alternative_recall": "max-matched-text-ref-ratio",
    "typed_failure": "all-applicable-gold-zero",
    "hard_veto": ["UNTRACEABLE", "CONTRADICTS", "incomplete-trace"],
    "empty_denominator": "fail-closed-without-content-addressed-na-declaration",
    "thresholds": GATE_METRIC_THRESHOLDS,
}
GATE_METRIC_FORMULA_HASH = content_id(GATE_METRIC_FORMULA)


class GoldMetricContractBuilder:
    """Persist one self-contained evaluator contract bundle per applicable Gold."""

    descriptor_version = "gold_metric_descriptor.v1"
    gold_contract_version = "gold_metric_contract.v1"
    accepted_evidence_contract_version = "accepted_evidence_contract.v1"
    evaluator_manifest_version = "evaluator_manifest.v1"

    def __init__(
        self,
        artifacts: ArtifactRepository,
        *,
        matcher: GoldEvidenceMatcher | None = None,
    ) -> None:
        self._artifacts = artifacts
        self._matcher = matcher or GoldEvidenceMatcher()

    def build_manifest(
        self,
        *,
        gold_items: Iterable[GoldItem],
        evaluator_manifest_id: StableId,
    ) -> tuple[EvaluatorManifestContract, ArtifactRef]:
        gold_ids = tuple(item.gold_id for item in gold_items)
        manifest = EvaluatorManifestContract(
            manifest_version=self.evaluator_manifest_version,
            evaluator_manifest_id=evaluator_manifest_id,
            gold_ids=gold_ids,
        )
        ref = self._artifacts.put(
            canonical_json_bytes(manifest.model_dump(mode="json")),
            "application/vnd.novel-agent.evaluator-manifest+json",
            VERSION,
        )
        return manifest, ref

    def build(
        self,
        *,
        gold_items: Iterable[GoldItem],
        evaluator_manifest_id: StableId,
        evaluator_manifest_hash: ArtifactId,
    ) -> dict[StableId, ContentAddressedGoldMetricDescriptor]:
        result: dict[StableId, ContentAddressedGoldMetricDescriptor] = {}
        for gold in gold_items:
            if gold.gold_type is None:
                raise ValueError(f"Gate Gold has no GoldType: {gold.gold_id.root}")
            accepted_contract = AcceptedEvidenceContract(
                contract_version=self.accepted_evidence_contract_version,
                gold_id=gold.gold_id,
                matcher_version=self._matcher.version,
                alternatives=self._matcher.accepted_alternatives(gold),
            )
            accepted_ref = self._artifacts.put(
                canonical_json_bytes(accepted_contract.model_dump(mode="json")),
                "application/vnd.novel-agent.accepted-evidence-contract+json",
                VERSION,
            )
            gold_contract = GoldMetricContract(
                contract_version=self.gold_contract_version,
                gold_id=gold.gold_id,
                gold_type=gold.gold_type,
                gold_kind=gold.kind.value,
                weight=gold.weight,
                mandatory=gold.mandatory,
                applicable_profiles=gold.applicable_profiles,
                accepted_evidence_contract_ref=accepted_ref,
                accepted_evidence_contract_hash=accepted_ref.artifact_id,
            )
            gold_contract_ref = self._artifacts.put(
                canonical_json_bytes(gold_contract.model_dump(mode="json")),
                "application/vnd.novel-agent.gold-metric-contract+json",
                VERSION,
            )
            descriptor = GoldMetricDescriptor(
                descriptor_version=self.descriptor_version,
                gold_id=gold.gold_id,
                gold_contract_ref=gold_contract_ref,
                gold_contract_hash=gold_contract_ref.artifact_id,
                gold_type=gold.gold_type,
                gold_kind=gold.kind.value,
                weight=gold.weight,
                mandatory=gold.mandatory,
                applicable_profiles=gold.applicable_profiles,
                accepted_evidence_contract_ref=accepted_ref,
                accepted_evidence_contract_hash=accepted_ref.artifact_id,
                evaluator_manifest_id=evaluator_manifest_id,
                evaluator_manifest_hash=evaluator_manifest_hash,
            )
            descriptor_ref = self._artifacts.put(
                canonical_json_bytes(descriptor.model_dump(mode="json")),
                "application/vnd.novel-agent.gold-metric-descriptor+json",
                VERSION,
            )
            result[gold.gold_id] = ContentAddressedGoldMetricDescriptor(
                descriptor=descriptor,
                descriptor_ref=descriptor_ref,
                descriptor_hash=descriptor_ref.artifact_id,
            )
        return result


__all__ = [
    "GATE_METRIC_FORMULA",
    "GATE_METRIC_FORMULA_HASH",
    "GATE_METRIC_FORMULA_VERSION",
    "GATE_METRIC_THRESHOLDS",
    "GoldMetricContractBuilder",
]
