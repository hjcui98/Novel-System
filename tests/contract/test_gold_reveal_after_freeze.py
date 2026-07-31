from __future__ import annotations

from novel_agent.domain.memory_benchmark import BenchmarkInformationProfile
from novel_agent.services.benchmark_importer import content_id
from tests.fixtures.stage2_memory_benchmark import resolved_public_comparison


def test_private_gold_is_revealed_only_after_all_arm_hashes_are_frozen() -> None:
    _bundle, private_case, public_case, runner, comparison = resolved_public_comparison()
    public_payload = public_case.model_dump(mode="json")
    assert not any("gold" in key.casefold() for key in public_payload)
    receipt = comparison.freeze_receipt
    assert receipt is not None and receipt.frozen_before_reveal
    assert set(receipt.arm_artifact_hashes) == {"A", "B", "C"}
    assert comparison.deterministic.writer_context is not None
    assert receipt.arm_artifact_hashes["A"] == content_id(
        comparison.deterministic.writer_context.model_dump(mode="json")
    )
    assert comparison.arm_c_writer_context is not None
    assert receipt.arm_artifact_hashes["C"] == content_id(
        comparison.arm_c_writer_context.model_dump(mode="json")
    )

    scored = runner.score_comparison(
        private_case,
        BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        comparison,
    )
    assert scored.case_id == private_case.case_id
    assert scored.deterministic_metrics.gold_evidence_recall >= 0.0
