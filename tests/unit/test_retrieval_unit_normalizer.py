from __future__ import annotations

from typing import ClassVar

import pytest

from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import RetrievalUnit
from novel_agent.services.canonical_alias_registry import CanonicalAliasRegistry
from novel_agent.services.memory_pipeline import AnchorBuilder
from novel_agent.services.retrieval_unit_normalizer import RetrievalUnitNormalizer
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


def _units() -> tuple[RetrievalUnit, ...]:
    bundle = make_synthetic_bundle()
    history, _ = bundle.text_roots
    world = bundle.world_roots[0]
    return AnchorBuilder().build(
        world,
        history,
        bundle.plan_roots[0],
        snapshot_id=StableId("snapshot.normalizer"),
    )


def test_identical_duplicate_is_reported_and_evidence_is_deduplicated() -> None:
    unit = next(item for item in _units() if item.evidence_refs)
    repeated_evidence = unit.model_copy(
        update={"evidence_refs": (unit.evidence_refs[0], unit.evidence_refs[0])}
    )
    result = RetrievalUnitNormalizer().normalize((repeated_evidence, repeated_evidence))

    assert result.duplicate_unit_ids == (unit.unit_id,)
    assert len(result.units[0].evidence_refs) == 1


def test_same_unit_id_with_different_payload_fails_closed() -> None:
    unit = next(item for item in _units() if item.unit_kind.value == "event_anchor")
    with pytest.raises(ValueError, match="conflicting payload"):
        RetrievalUnitNormalizer().normalize((unit, unit.model_copy(update={"text": "different"})))


def test_tied_current_records_remain_visible_as_conflict() -> None:
    unit = next(item for item in _units() if item.predicate)
    tied = unit.model_copy(
        update={
            "unit_id": StableId("unit.tied-current"),
            "text": "contrary current value",
            "canonical_value_id": None,
            "canonicalizer_version": None,
        }
    )
    result = RetrievalUnitNormalizer().normalize((unit, tied))

    assert result.conflicts
    assert {item.unit_id for item in result.units} == {unit.unit_id, tied.unit_id}


def test_later_evidence_chapter_supersedes_earlier_current_record() -> None:
    unit = next(item for item in _units() if item.predicate and item.evidence_refs)
    earlier_ref = unit.evidence_refs[0].model_copy(
        update={"chapter_id": StableId("chapter.case.5")}
    )
    later_ref = unit.evidence_refs[0].model_copy(update={"chapter_id": StableId("chapter.case.20")})
    earlier = unit.model_copy(
        update={
            "unit_id": StableId("unit.earlier-current"),
            "text": "earlier value",
            "narrative_start": None,
            "narrative_end": None,
            "evidence_refs": (earlier_ref,),
        }
    )
    later = unit.model_copy(
        update={
            "unit_id": StableId("unit.later-current"),
            "text": "later value",
            "narrative_start": None,
            "narrative_end": None,
            "evidence_refs": (later_ref,),
        }
    )

    result = RetrievalUnitNormalizer().normalize((earlier, later))

    assert result.conflicts == ()
    assert result.units == (later,)
    assert result.superseded_unit_ids == (earlier.unit_id,)


def test_repeatable_result_records_do_not_form_a_false_current_conflict() -> None:
    unit = next(item for item in _units() if item.predicate and item.evidence_refs)
    first = unit.model_copy(
        update={
            "unit_id": StableId("anchor.state.exam-result.first-school"),
            "predicate": "exam_result",
            "text": '人物 exam_result "failed_first_school"',
        }
    )
    second = unit.model_copy(
        update={
            "unit_id": StableId("anchor.state.exam-result.second-school"),
            "predicate": "exam_result",
            "text": '人物 exam_result "failed_second_school"',
        }
    )

    result = RetrievalUnitNormalizer().normalize((first, second))

    assert result.conflicts == ()
    assert {item.unit_id for item in result.units} == {
        first.unit_id,
        second.unit_id,
    }


def test_same_evidence_near_synonym_labels_are_semantic_aliases() -> None:
    unit = next(item for item in _units() if item.predicate and item.evidence_refs)
    predicate = "attitude_toward_event"
    first = unit.model_copy(
        update={
            "unit_id": StableId("unit.attitude-short"),
            "predicate": predicate,
            "text": f'人物 {predicate} "indifferent_to_ivy_feast" 相同证据正文',
            "canonical_value_id": None,
            "canonicalizer_version": None,
            "narrative_start": None,
            "narrative_end": None,
        }
    )
    second = unit.model_copy(
        update={
            "unit_id": StableId("unit.attitude-long"),
            "predicate": predicate,
            "text": f'人物 {predicate} "indifferent_to_fame_from_ivy_feast"',
            "canonical_value_id": None,
            "canonicalizer_version": None,
            "narrative_start": None,
            "narrative_end": None,
        }
    )

    result = RetrievalUnitNormalizer().normalize((first, second))

    assert result.conflicts == ()
    assert len(result.units) == 1
    assert {result.units[0].unit_id, *result.superseded_unit_ids} == {
        first.unit_id,
        second.unit_id,
    }
    assert len(result.canonical_alias_receipts) == 1
    assert result.units[0].canonical_alias_receipt_ref is not None
    assert (
        result.units[0].canonical_value_id == result.canonical_alias_receipts[0].canonical_value_id
    )
    assert result.units[0].canonicalizer_version == "canonical_value.v1"


def test_same_evidence_and_value_merge_despite_different_rendered_tail() -> None:
    unit = next(item for item in _units() if item.predicate and item.evidence_refs)
    predicate = unit.predicate
    assert predicate is not None
    anchor = unit.model_copy(
        update={
            "unit_id": StableId("unit.anchor-rendering"),
            "text": f'人物 {predicate} "same_value" supporting prose',
            "canonical_value_id": None,
            "canonicalizer_version": None,
            "narrative_start": None,
            "narrative_end": None,
        }
    )
    r1 = unit.model_copy(
        update={
            "unit_id": StableId("unit.r1-rendering"),
            "text": f'entity.canonical {predicate} "same_value"',
            "canonical_value_id": None,
            "canonicalizer_version": None,
            "narrative_start": None,
            "narrative_end": None,
        }
    )

    result = RetrievalUnitNormalizer().normalize((anchor, r1))

    assert result.conflicts == ()
    assert len(result.units) == 1
    assert len(result.superseded_unit_ids) == 1
    assert result.canonical_alias_receipts == ()
    assert result.units[0].canonical_value_id is not None


def test_explicit_canonical_identity_merges_and_true_conflict_stays_visible() -> None:
    unit = next(item for item in _units() if item.predicate and item.evidence_refs)
    predicate = unit.predicate
    assert predicate is not None
    canonical_id = StableId("canonical-value.trusted.same")
    first = unit.model_copy(
        update={
            "unit_id": StableId("unit.canonical-first"),
            "text": f'人物 {predicate} "presentation_alpha"',
            "canonical_value_id": canonical_id,
            "canonicalizer_version": "canonical_value.trusted.v1",
        }
    )
    second = unit.model_copy(
        update={
            "unit_id": StableId("unit.canonical-second"),
            "text": f'人物 {predicate} "presentation_beta"',
            "canonical_value_id": canonical_id,
            "canonicalizer_version": "canonical_value.trusted.v1",
        }
    )

    merged = RetrievalUnitNormalizer().normalize((first, second))

    assert merged.conflicts == ()
    assert len(merged.units) == 1
    assert merged.units[0].canonical_value_id == canonical_id

    contrary = second.model_copy(
        update={
            "canonical_value_id": StableId("canonical-value.trusted.contrary"),
            "text": f'人物 {predicate} "hostile_to_ivy_feast"',
        }
    )
    conflicted = RetrievalUnitNormalizer().normalize((first, contrary))
    assert conflicted.conflicts
    assert {item.unit_id for item in conflicted.units} == {
        first.unit_id,
        contrary.unit_id,
    }


def test_content_and_unit_identity_fallbacks_are_stable() -> None:
    unit = next(item for item in _units() if item.unit_kind.value == "event_anchor")
    content_identity = RetrievalUnitNormalizer._canonical_identity(unit)
    no_content = unit.model_copy(
        update={"unit_id": StableId("unit.no-content"), "content_hash": None}
    )
    assert content_identity
    assert RetrievalUnitNormalizer._canonical_identity(no_content) == (
        "unit",
        "unit.no-content",
    )
    assert RetrievalUnitNormalizer._chapter_number("chapter.case.prelude") == 0
    assert RetrievalUnitNormalizer._chapter_number("prelude.case") == 0
    assert RetrievalUnitNormalizer._chapter_number("chapter.case.20") == 20
    assert RetrievalUnitNormalizer._chapter_number("chapter.case.unknown") is None


def test_semantic_alias_rejection_edges_are_explicit() -> None:
    unit = next(item for item in _units() if item.predicate and item.evidence_refs)
    predicate = unit.predicate
    assert predicate is not None
    assert not RetrievalUnitNormalizer._are_semantic_aliases([unit])
    assert not RetrievalUnitNormalizer._are_semantic_aliases(
        [unit.model_copy(update={"evidence_refs": ()}), unit]
    )
    assert (
        RetrievalUnitNormalizer._state_value_and_tail(unit.model_copy(update={"predicate": None}))
        is None
    )

    first = unit.model_copy(
        update={
            "unit_id": StableId("unit.alias-first"),
            "text": f'人物 {predicate} "alpha beta" first-tail',
            "canonical_value_id": None,
            "canonicalizer_version": None,
        }
    )
    different_tail = unit.model_copy(
        update={
            "unit_id": StableId("unit.alias-different-tail"),
            "text": f'人物 {predicate} "gamma delta" second-tail',
            "canonical_value_id": None,
            "canonicalizer_version": None,
        }
    )
    dissimilar = different_tail.model_copy(
        update={
            "unit_id": StableId("unit.alias-dissimilar"),
            "text": f'人物 {predicate} "gamma delta" first-tail',
        }
    )
    assert not RetrievalUnitNormalizer._are_semantic_aliases([first, different_tail])
    assert not RetrievalUnitNormalizer._are_semantic_aliases([first, dissimilar])

    different_predicate = different_tail.model_copy(update={"predicate": "different_predicate"})
    assert RetrievalUnitNormalizer()._canonical_merge([first, different_predicate]) is None

    registry = CanonicalAliasRegistry()
    assert registry.equivalent(predicate, "alpha", "beta") is None
    predicate_value: str = predicate

    class OneWayRegistry(CanonicalAliasRegistry):
        _ALIASES: ClassVar[dict[str, dict[str, str]]] = {predicate_value: {"short": "long"}}

    registry = OneWayRegistry()
    assert registry.equivalent(predicate, "short", "long") is None
    assert (
        RetrievalUnitNormalizer(registry)._canonical_merge(
            [
                first.model_copy(update={"text": f'人物 {predicate} "short" first-tail'}),
                first.model_copy(
                    update={
                        "unit_id": StableId("unit.alias-long-without-reverse-entry"),
                        "text": f'人物 {predicate} "long" first-tail',
                    }
                ),
            ]
        )
        is None
    )

    reference = unit.evidence_refs[0]
    assert RetrievalUnitNormalizer._evidence_identity(
        unit.model_copy(update={"evidence_refs": (reference.model_copy(update={"span": None}),)})
    )
