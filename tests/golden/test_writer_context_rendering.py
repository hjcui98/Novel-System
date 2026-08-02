from __future__ import annotations

import pytest
from tests.fixtures.stage2_memory_benchmark import writer_context_inputs

from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import RequirementLevel, RetrievalUnitKind
from novel_agent.domain.memory_benchmark import ContextAssemblyStatus
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.writer_context_assembler import WriterContextAssembler


def test_rendering_is_byte_identical_and_has_fixed_section_headers() -> None:
    task, needs, all_units, commit = writer_context_inputs()
    units = tuple(
        item
        for item in all_units
        if item.unit_kind
        in {
            RetrievalUnitKind.STATE_ANCHOR,
            RetrievalUnitKind.RELATION_ANCHOR,
            RetrievalUnitKind.EVENT_ANCHOR,
            RetrievalUnitKind.PLAN_ANCHOR,
        }
    )[:8]
    kwargs = {
        "task": task,
        "units": units,
        "needs": needs,
        "basis_commit_id": commit,
        "basis_snapshot_id": StableId("snapshot.golden"),
        "arm": "A",
        "writer_token_budget": 4000,
    }
    first = WriterContextAssembler().assemble(**kwargs)
    second = WriterContextAssembler().assemble(**kwargs)
    assert first.package.rendered_context.encode() == second.package.rendered_context.encode()
    assert "[当前世界状态]" in first.package.rendered_context
    assert "[计划与未决义务]" in first.package.rendered_context


def test_invalid_configuration_and_call_parameters_fail_closed() -> None:
    with pytest.raises(ValueError, match="reduction rounds"):
        WriterContextAssembler(max_reduction_rounds=-1)
    task, needs, units, commit = writer_context_inputs()
    common = {
        "task": task,
        "units": units,
        "needs": needs,
        "basis_commit_id": commit,
        "basis_snapshot_id": StableId("snapshot.invalid"),
        "writer_token_budget": 100,
    }
    with pytest.raises(ValueError, match="arm must"):
        WriterContextAssembler().assemble(**common, arm="D")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="budgets must"):
        WriterContextAssembler().assemble(
            **(common | {"writer_token_budget": 0}),
            arm="A",
        )


def test_mandatory_overflow_drops_optional_and_evidence_overflow_is_typed() -> None:
    task, needs, units, commit = writer_context_inputs()
    source = next(item for item in units if item.evidence_refs)
    mandatory = source.model_copy(update={"mandatory": True, "text": "甲" * 800})
    optional = source.model_copy(
        update={
            "unit_id": StableId("unit.optional-overflow"),
            "mandatory": False,
            "text": "optional",
        }
    )
    optional_needs = tuple(
        need.model_copy(update={"requirement": RequirementLevel.OPTIONAL}) for need in needs
    )
    overflow = WriterContextAssembler(max_reduction_rounds=0).assemble(
        task=task,
        units=(mandatory, optional),
        needs=optional_needs,
        basis_commit_id=commit,
        basis_snapshot_id=StableId("snapshot.overflow"),
        arm="A",
        writer_token_budget=10,
    )
    assert overflow.status is ContextAssemblyStatus.CONTEXT_BUDGET_INSUFFICIENT
    assert "mandatory_overflow" in overflow.package.budget_report.dropped_optional_reasons.values()

    evidence_overflow = WriterContextAssembler().assemble(
        task=task,
        units=(source.model_copy(update={"mandatory": True}),),
        needs=(),
        basis_commit_id=commit,
        basis_snapshot_id=StableId("snapshot.evidence-overflow"),
        arm="A",
        writer_token_budget=4000,
        evidence_ledger_token_budget=1,
    )
    assert evidence_overflow.status is ContextAssemblyStatus.EVIDENCE_INSUFFICIENT
    assert "EVIDENCE_LEDGER_BUDGET_EXCEEDED" in evidence_overflow.diagnostic_codes


def test_raw_grounded_and_untraceable_units_become_evidence_gap_not_claims() -> None:
    task, needs, units, commit = writer_context_inputs()
    grounded = next(
        item
        for item in units
        if item.unit_kind in {RetrievalUnitKind.GROUNDED_BLOCK, RetrievalUnitKind.GROUNDED_SPAN}
    )
    untraceable = grounded.model_copy(
        update={
            "unit_id": StableId("unit.untraceable"),
            "unit_kind": RetrievalUnitKind.FACT_ANCHOR,
            "evidence_refs": (),
            "content_hash": None,
            "text": "untraceable fact",
        }
    )
    result = WriterContextAssembler().assemble(
        task=task,
        units=(grounded, untraceable),
        needs=needs,
        basis_commit_id=commit,
        basis_snapshot_id=StableId("snapshot.gaps"),
        arm="A",
        writer_token_budget=1000,
    )
    assert not result.package.continuity_constraints
    assert any(
        gap.gap_id == StableId("gap.untraceable.unit.untraceable") for gap in result.package.gaps
    )


def test_relation_and_knowledge_predicates_route_to_distinct_sections() -> None:
    _task, _needs, units, _commit = writer_context_inputs()
    base = units[0]
    relation = base.model_copy(update={"unit_kind": RetrievalUnitKind.RELATION_ANCHOR})
    knowledge = base.model_copy(
        update={"unit_kind": RetrievalUnitKind.FACT_ANCHOR, "predicate": "secret_knows"}
    )
    fallback = base.model_copy(
        update={"unit_kind": RetrievalUnitKind.FACT_ANCHOR, "predicate": "continuity"}
    )
    assert WriterContextAssembler._section_for_unit(relation).value == "relationship_and_emotion"
    assert WriterContextAssembler._section_for_unit(knowledge).value == "knowledge_and_disclosure"
    assert WriterContextAssembler._section_for_unit(fallback).value == "continuity_constraints"


def test_conflicting_current_records_return_evidence_insufficient() -> None:
    task, needs, units, commit = writer_context_inputs()
    current = next(item for item in units if item.predicate)
    contrary = current.model_copy(
        update={
            "unit_id": StableId("unit.current-contrary"),
            "text": "contrary value",
            "canonical_value_id": None,
            "canonicalizer_version": None,
        }
    )
    result = WriterContextAssembler().assemble(
        task=task,
        units=(current, contrary),
        needs=needs,
        basis_commit_id=commit,
        basis_snapshot_id=StableId("snapshot.conflict"),
        arm="A",
        writer_token_budget=4000,
    )
    assert result.status is ContextAssemblyStatus.EVIDENCE_INSUFFICIENT
    assert "UNRESOLVED_CURRENT_RECORD_CONFLICT" in result.diagnostic_codes


def test_registered_current_value_alias_persists_receipt_in_writer_lineage() -> None:
    task, needs, units, commit = writer_context_inputs()
    current = next(item for item in units if item.predicate and item.evidence_refs)
    predicate = "attitude_toward_event"
    anchor = current.model_copy(
        update={
            "unit_id": StableId("unit.alias.anchor"),
            "predicate": predicate,
            "text": f'人物 {predicate} "indifferent_to_ivy_feast" 证据摘要',
            "canonical_value_id": None,
            "canonicalizer_version": None,
        }
    )
    r1 = current.model_copy(
        update={
            "unit_id": StableId("unit.alias.r1"),
            "predicate": predicate,
            "text": f'人物 {predicate} "indifferent_to_fame_from_ivy_feast"',
            "canonical_value_id": None,
            "canonicalizer_version": None,
        }
    )

    result = WriterContextAssembler().assemble(
        task=task,
        units=(anchor, r1),
        needs=needs,
        basis_commit_id=commit,
        basis_snapshot_id=StableId("snapshot.alias-receipt"),
        arm="A",
        writer_token_budget=4000,
    )

    assert result.status is ContextAssemblyStatus.READY
    assert "UNRESOLVED_CURRENT_RECORD_CONFLICT" not in result.diagnostic_codes
    assert len(result.package.lineage.canonical_alias_receipts) == 1
    assert len(result.package.lineage.canonical_alias_receipt_refs) == 1
    receipt = result.package.lineage.canonical_alias_receipts[0]
    assert receipt.predicate == predicate
    assert result.package.lineage.canonical_alias_receipt_refs[0].artifact_id == sha256_id(
        canonical_json_bytes(receipt.model_dump(mode="json"))
    )
