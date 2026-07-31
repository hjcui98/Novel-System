from __future__ import annotations

from pathlib import Path

import pytest

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.ids import StableId
from novel_agent.domain.memory_benchmark import BenchmarkInformationProfile
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.memory_benchmark_evaluation import (
    MemoryBenchmarkEvaluator,
    SemanticSupport,
)
from novel_agent.services.memory_benchmark_metric_contracts import GoldMetricContractBuilder
from tests.fixtures.stage2_memory_benchmark import resolved_public_comparison


@pytest.mark.integration
def test_frozen_arm_is_consumable_by_per_gold_evaluator_after_reveal(tmp_path: Path) -> None:
    _bundle, private_case, _public_case, _runner, comparison = resolved_public_comparison()
    package = comparison.deterministic.writer_context
    ledger = comparison.deterministic.evidence_ledger
    receipt = comparison.freeze_receipt
    assert package is not None and ledger is not None and receipt is not None

    gold_items = tuple(
        item
        for item in (
            *private_case.observed_use_gold,
            *private_case.operational_constraint_gold,
            *private_case.plan_obligation_gold,
        )
        if BenchmarkInformationProfile.VISIBLE_AT_CUTOFF in item.applicable_profiles
    )
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    builder = GoldMetricContractBuilder(repository)
    manifest_id = StableId("evaluator-manifest.integration.freeze-reveal")
    _manifest, manifest_ref = builder.build_manifest(
        gold_items=gold_items,
        evaluator_manifest_id=manifest_id,
    )
    descriptors = builder.build(
        gold_items=gold_items,
        evaluator_manifest_id=manifest_id,
        evaluator_manifest_hash=manifest_ref.artifact_id,
    )
    report = MemoryBenchmarkEvaluator(
        semantic_verifier=lambda _gold, _items: SemanticSupport.SUPPORTS
    ).evaluate(
        package=package,
        evidence_ledger=ledger,
        gold_items=gold_items,
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        freeze_receipt=receipt,
        evaluator_manifest_id=manifest_id,
        evaluator_manifest_ref=manifest_ref,
        evaluator_manifest_hash=manifest_ref.artifact_id,
        gold_metric_descriptors=descriptors,
    )
    assert report.comparisons
    assert all(item.explanation for item in report.comparisons)
