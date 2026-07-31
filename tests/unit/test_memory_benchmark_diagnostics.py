from __future__ import annotations

from novel_agent.domain.memory_benchmark import (
    BenchmarkInformationProfile,
    EvidenceLedger,
    EvidenceStageFailure,
)
from novel_agent.services.memory_benchmark_diagnostics import StageLossDiagnosticBuilder
from tests.fixtures.stage2_memory_benchmark import resolved_public_comparison


def test_stage_loss_diagnostics_cover_every_applicable_gold() -> None:
    _bundle, private_case, _public, _runner, comparison = resolved_public_comparison()
    context = comparison.deterministic.context
    ledger = comparison.deterministic.evidence_ledger
    assert ledger is not None
    gold_items = (
        *private_case.observed_use_gold,
        *private_case.operational_constraint_gold,
        *private_case.plan_obligation_gold,
    )

    diagnostics = StageLossDiagnosticBuilder().build(
        gold_items=gold_items,
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        stage1_context=context,
        writer_ledger=ledger,
    )

    assert {item.gold_id for item in diagnostics} == {
        item.gold_id
        for item in gold_items
        if BenchmarkInformationProfile.VISIBLE_AT_CUTOFF in item.applicable_profiles
    }
    assert all(
        item.writer_ledger.matched_reference_count <= item.writer_ledger.accepted_reference_count
        for item in diagnostics
    )
    inapplicable = StageLossDiagnosticBuilder().build(
        gold_items=(
            gold_items[0].model_copy(
                update={
                    "applicable_profiles": (BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,)
                }
            ),
        ),
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        stage1_context=context,
        writer_ledger=ledger,
    )
    assert inapplicable == ()


def test_stage_loss_classification_distinguishes_final_assembly_loss() -> None:
    _bundle, private_case, _public, _runner, comparison = resolved_public_comparison()
    context = comparison.deterministic.context
    ledger = comparison.deterministic.evidence_ledger
    assert ledger is not None
    gold_items = (
        *private_case.observed_use_gold,
        *private_case.operational_constraint_gold,
        *private_case.plan_obligation_gold,
    )
    baseline = StageLossDiagnosticBuilder().build(
        gold_items=gold_items,
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        stage1_context=context,
        writer_ledger=ledger,
    )
    complete = next(
        item
        for item in baseline
        if item.stage1_selected.complete_alternative_ids
        and item.writer_ledger.complete_alternative_ids
    )
    empty_ledger = EvidenceLedger(
        contract_version=ledger.contract_version,
        entries=(),
        rendered_tokens=0,
    )

    diagnostics = StageLossDiagnosticBuilder().build(
        gold_items=gold_items,
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        stage1_context=context,
        writer_ledger=empty_ledger,
    )
    lost = next(item for item in diagnostics if item.gold_id == complete.gold_id)

    assert lost.primary_failure is EvidenceStageFailure.F_ASSEMBLY
    assert lost.writer_ledger.complete_alternative_ids == ()
