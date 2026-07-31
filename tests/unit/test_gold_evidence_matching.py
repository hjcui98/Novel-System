from __future__ import annotations

import pytest

from novel_agent.domain.benchmark import PlanEvidenceRef
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.memory_benchmark import EvidenceSet
from novel_agent.services.gold_evidence_matching import GoldEvidenceMatcher
from tests.fixtures.stage2_memory_benchmark import (
    frozen_evaluation_inputs,
    resolved_public_comparison,
)


def test_matcher_rejects_invalid_span_threshold() -> None:
    for value in (0.0, 1.1):
        with pytest.raises(ValueError, match="span coverage"):
            GoldEvidenceMatcher(minimum_span_coverage=value)


def test_plan_alternative_matches_exact_plan_provenance() -> None:
    gold, _package, ledger, _receipt = frozen_evaluation_inputs()
    entry = ledger.entries[0].model_copy(update={"plan_node_ids": (StableId("plan.accepted"),)})
    plan_gold = gold.model_copy(
        update={
            "accepted_evidence_sets": (
                EvidenceSet(
                    evidence_set_id=StableId("accepted.plan"),
                    plan_node_ids=(StableId("plan.accepted"),),
                    component_ids=("plan",),
                ),
            )
        }
    )
    match = GoldEvidenceMatcher().match(plan_gold, ledger.model_copy(update={"entries": (entry,)}))
    assert match.matched
    assert match.supported_components == ("plan",)


def test_missing_plan_does_not_match_and_block_namespace_is_not_identity() -> None:
    gold, _package, ledger, _receipt = frozen_evaluation_inputs()
    missing_plan = gold.model_copy(
        update={
            "accepted_evidence_sets": (
                EvidenceSet(
                    evidence_set_id=StableId("accepted.missing-plan"),
                    plan_node_ids=(StableId("plan.missing"),),
                ),
            )
        }
    )
    assert not GoldEvidenceMatcher().match(missing_plan, ledger).matched

    expected = gold.evidence_refs[0]
    actual = ledger.entries[0].evidence_refs[0]
    if expected.span is not None and actual.span is not None:
        wrong = actual.model_copy(
            update={
                "evidence_id": StableId("evidence.wrong-block"),
                "span": actual.span.model_copy(update={"block_id": StableId("block.wrong")}),
            }
        )
        assert GoldEvidenceMatcher()._ref_matches(expected, wrong)


def test_same_object_requires_precise_spans() -> None:
    gold, _package, ledger, _receipt = frozen_evaluation_inputs()
    expected = gold.evidence_refs[0].model_copy(
        update={
            "evidence_id": StableId("evidence.other-id"),
            "object_hash": ledger.entries[0].evidence_refs[0].object_hash,
            "span": None,
        }
    )
    assert not GoldEvidenceMatcher()._ref_matches(expected, ledger.entries[0].evidence_refs[0])
    different_object = expected.model_copy(update={"object_hash": ArtifactId("sha256:" + "1" * 64)})
    assert not GoldEvidenceMatcher()._ref_matches(
        different_object, ledger.entries[0].evidence_refs[0]
    )


def test_precise_child_span_matches_reviewed_broad_span() -> None:
    _gold, _package, ledger, _receipt = frozen_evaluation_inputs()
    actual = ledger.entries[0].evidence_refs[0]
    assert actual.span is not None
    expected = actual.model_copy(
        update={
            "evidence_id": StableId("evidence.reviewed-broad-span"),
            "span": actual.span.model_copy(
                update={
                    "start": 0,
                    "end": actual.span.end + 100,
                }
            ),
        }
    )

    assert GoldEvidenceMatcher()._ref_matches(expected, actual)


def test_incomplete_conjunctive_alternative_exposes_only_partial_provenance() -> None:
    gold, _package, ledger, _receipt = frozen_evaluation_inputs()
    present = ledger.entries[0].evidence_refs[0]
    absent = present.model_copy(
        update={
            "evidence_id": StableId("evidence.absent"),
            "object_hash": ArtifactId("sha256:" + "2" * 64),
        }
    )
    partial_gold = gold.model_copy(
        update={
            "accepted_evidence_sets": (
                EvidenceSet(
                    evidence_set_id=StableId("accepted.conjunctive"),
                    evidence_refs=(present, absent),
                ),
            )
        }
    )

    match = GoldEvidenceMatcher().match(partial_gold, ledger)
    coverage = GoldEvidenceMatcher().coverage(partial_gold, ledger)

    assert not match.matched
    assert match.partially_matched
    assert match.matched_ledger_ids == (ledger.entries[0].ledger_id,)
    assert coverage.accepted_reference_count == 2
    assert coverage.matched_reference_count == 1
    assert coverage.complete_alternative_ids == ()
    assert coverage.partial_alternative_ids == (StableId("accepted.conjunctive"),)


def test_legacy_plan_evidence_is_promoted_to_an_accepted_alternative() -> None:
    bundle, private_case, _public, _runner, comparison = resolved_public_comparison()
    gold = private_case.plan_obligation_gold[0]
    ledger = comparison.deterministic.evidence_ledger
    assert ledger is not None
    plan = bundle.plan_roots[0]
    goal_id = plan.chapter_goals[0].goal_id
    plan_ref = PlanEvidenceRef(
        evidence_id=StableId("evidence.plan.legacy"),
        plan_root_hash=plan.root_hash,
        goal_id=goal_id,
        object_hash=plan.root_hash,
    )
    entry = ledger.entries[0].model_copy(update={"plan_node_ids": (goal_id,)})
    match = GoldEvidenceMatcher().match(
        gold.model_copy(
            update={
                "accepted_evidence_sets": (),
                "plan_evidence_refs": (plan_ref,),
            }
        ),
        ledger.model_copy(update={"entries": (entry,)}),
    )
    assert match.matched


def test_historical_recall_requires_text_and_plan_coverage_tracks_missing_nodes() -> None:
    gold, _package, ledger, _receipt = frozen_evaluation_inputs()
    matcher = GoldEvidenceMatcher()
    with pytest.raises(ValueError, match="no text evidence"):
        matcher.text_reference_recall(
            EvidenceSet(
                evidence_set_id=StableId("accepted.plan-only-recall"),
                plan_node_ids=(StableId("plan.only"),),
            ),
            ledger,
        )
    plan_gold = gold.model_copy(
        update={
            "accepted_evidence_sets": (
                EvidenceSet(
                    evidence_set_id=StableId("accepted.plan-missing-coverage"),
                    plan_node_ids=(StableId("plan.missing"),),
                ),
            )
        }
    )
    coverage = matcher.coverage(plan_gold, ledger)
    assert coverage.accepted_reference_count == 1
    assert coverage.matched_reference_count == 0
