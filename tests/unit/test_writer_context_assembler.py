from __future__ import annotations

from typing import Any
from unittest.mock import patch

from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import (
    ExpectedClaimScope,
    NeedFacet,
    NeedFacetKind,
    RequirementLevel,
    RetrievalUnitKind,
    Stage1QueryIntent,
)
from novel_agent.domain.memory_benchmark import (
    BenchmarkInformationProfile,
    ContextAssemblyStatus,
)
from novel_agent.domain.writer_context import WriterContextSection
from novel_agent.services.memory_benchmark_contract import build_safe_task_contract
from novel_agent.services.memory_pipeline import AnchorBuilder
from novel_agent.services.task_conditioned_need_generation import (
    TaskPlanConditionedNeedGenerator,
)
from novel_agent.services.writer_context_assembler import WriterContextAssembler
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


def _inputs() -> tuple[Any, ...]:
    bundle = make_synthetic_bundle()
    history, _future = bundle.text_roots
    world = bundle.world_roots[0]
    plan = bundle.plan_roots[0]
    task = build_safe_task_contract(
        case_id=bundle.case_manifests[0].case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
    )
    needs = TaskPlanConditionedNeedGenerator().generate(task, world, plan)
    units = AnchorBuilder().build(
        world,
        history,
        plan,
        snapshot_id=StableId("snapshot.stage2m"),
    )
    selected = tuple(
        item
        for item in units
        if item.unit_kind
        in {
            RetrievalUnitKind.STATE_ANCHOR,
            RetrievalUnitKind.RELATION_ANCHOR,
            RetrievalUnitKind.EVENT_ANCHOR,
            RetrievalUnitKind.PLAN_ANCHOR,
        }
    )[:8]
    return task, needs, selected, world.source_commit


def test_ready_writer_context_is_grounded_separated_and_byte_stable() -> None:
    task, needs, units, commit = _inputs()
    assembler = WriterContextAssembler()
    kwargs = {
        "task": task,
        "units": units,
        "needs": needs,
        "basis_commit_id": commit,
        "basis_snapshot_id": StableId("snapshot.stage2m"),
        "arm": "A",
        "writer_token_budget": 4000,
    }

    first = assembler.assemble(**kwargs)
    second = assembler.assemble(**kwargs)

    assert first == second
    assert first.status is ContextAssemblyStatus.READY
    assert (
        first.package.budget_report.actual_rendered_writer_tokens
        <= first.package.budget_report.configured_writer_token_budget
    )
    assert first.evidence_ledger.entries
    assert not any(
        item.unit_id in first.package.lineage.retrieval_unit_ids
        and item.unit_kind in {RetrievalUnitKind.GROUNDED_BLOCK, RetrievalUnitKind.GROUNDED_SPAN}
        for item in units
    )


def test_mandatory_overflow_returns_typed_failure() -> None:
    task, needs, units, commit = _inputs()
    source = next(item for item in units if item.evidence_refs)
    oversized = source.model_copy(
        update={"unit_id": StableId("unit.oversized"), "text": "甲" * 1000, "mandatory": True}
    )

    result = WriterContextAssembler().assemble(
        task=task,
        units=(oversized,),
        needs=needs,
        basis_commit_id=commit,
        basis_snapshot_id=StableId("snapshot.stage2m"),
        arm="A",
        writer_token_budget=16,
    )

    assert result.status is ContextAssemblyStatus.CONTEXT_BUDGET_INSUFFICIENT
    assert result.package.budget_report.final_status is result.status
    assert result.package.budget_report.actual_rendered_writer_tokens > 16


def test_claim_projection_does_not_copy_anchor_prose_into_writer_context() -> None:
    assert (
        WriterContextAssembler._clean_claim("structured conclusion\nraw supporting prose")
        == "structured conclusion"
    )
    frontmatter = "内容简介:未来剧透\n第一卷 开始\n正文历史"
    assert WriterContextAssembler._narrative_content_start(frontmatter) == frontmatter.index(
        "第一卷"
    )
    assert WriterContextAssembler._narrative_content_start("正文历史") == 0


def test_grounded_fallback_becomes_precise_bounded_extractive_claim() -> None:
    task, needs, units, commit = _inputs()
    source = next(item for item in units if item.evidence_refs)
    need = needs[0].model_copy(
        update={
            "need_id": StableId("need.grounded-callback"),
            "query_text": "关键线索 怪字 未解",
            "query_intent": Stage1QueryIntent.SEMANTIC_HISTORY,
            "allow_plan": False,
            "expected_section": WriterContextAssembler._section_for_unit(source),
            "requirement": RequirementLevel.OPTIONAL,
        }
    )
    raw = "甲" * 240 + "。关键线索是一千六百零一个怪字,当前仍然未解。" + "乙" * 240 + "。"
    evidence = source.evidence_refs[0]
    assert evidence.span is not None
    grounded = source.model_copy(
        update={
            "unit_id": StableId("grounded.long-block"),
            "unit_kind": RetrievalUnitKind.GROUNDED_BLOCK,
            "text": raw,
            "evidence_refs": (
                evidence.model_copy(
                    update={"span": evidence.span.model_copy(update={"start": 0, "end": len(raw)})}
                ),
            ),
        }
    )

    result = WriterContextAssembler().assemble(
        task=task,
        units=(grounded,),
        needs=(need,),
        basis_commit_id=commit,
        basis_snapshot_id=StableId("snapshot.stage2m"),
        arm="A",
        writer_token_budget=4000,
        unit_need_ids={grounded.unit_id: (need.need_id,)},
    )

    projected = tuple(
        item
        for section in (
            result.package.continuity_constraints,
            result.package.current_world_state,
            result.package.relationship_and_emotion,
            result.package.causal_history,
            result.package.knowledge_and_disclosure,
            result.package.plan_and_obligations,
            result.package.long_range_callbacks,
        )
        for item in section
    )
    assert len(projected) == 1
    assert "一千六百零一个怪字" in projected[0].claim
    assert len(projected[0].claim) < len(raw)
    refined = result.evidence_ledger.entries[0].evidence_refs
    assert refined
    assert all(item.span is not None and item.span.end - item.span.start <= 180 for item in refined)


def test_grounded_extraction_and_clipping_defensive_edges() -> None:
    _task, needs, units, _commit = _inputs()
    source = next(item for item in units if item.evidence_refs)
    need = needs[0].model_copy(update={"query_text": "x"})
    evidence = source.evidence_refs[0]
    assert evidence.span is not None

    whitespace = source.model_copy(
        update={
            "unit_kind": RetrievalUnitKind.GROUNDED_BLOCK,
            "text": "   \n",
            "evidence_refs": (
                evidence.model_copy(
                    update={"span": evidence.span.model_copy(update={"start": 0, "end": 4})}
                ),
            ),
        }
    )
    assert WriterContextAssembler._extract_grounded_claim(whitespace, (need,)) == ("", ())

    frontmatter = "内容简介:未来信息\n第一卷 开始\n正文线索。"
    bounded = whitespace.model_copy(
        update={
            "text": frontmatter,
            "evidence_refs": (
                evidence.model_copy(
                    update={
                        "span": evidence.span.model_copy(
                            update={"start": 0, "end": len(frontmatter)}
                        )
                    }
                ),
            ),
        }
    )
    claim, refs = WriterContextAssembler._extract_grounded_claim(bounded, (need,))
    assert "正文线索" in claim
    assert refs

    no_span = bounded.model_copy(
        update={"evidence_refs": (evidence.model_copy(update={"span": None}),)}
    )
    assert WriterContextAssembler._extract_grounded_claim(no_span, (need,)) == ("", ())

    with patch.object(WriterContextAssembler, "_clip_excerpt", return_value=(0, 0, "")):
        assert WriterContextAssembler._extract_grounded_claim(bounded, (need,)) == ("", ())

    long_text = "甲" * 120 + "关键" + "乙" * 120
    start, end, clipped = WriterContextAssembler._clip_excerpt(
        long_text,
        0,
        len(long_text),
        long_text,
        ("关键",),
    )
    assert 0 < start < end < len(long_text)
    assert len(clipped) == 180
    assert "关键" in clipped
    assert WriterContextAssembler._claim_query_relevance("claim", "!") == 0.0


def test_optional_claims_are_reduced_to_fit_evidence_ledger_budget() -> None:
    task, needs, units, commit = _inputs()
    traceable = tuple(item for item in units if item.evidence_refs)[:2]
    optional_needs = tuple(
        need.model_copy(update={"requirement": RequirementLevel.OPTIONAL}) for need in needs
    )
    assembler = WriterContextAssembler()
    one = assembler.assemble(
        task=task,
        units=traceable[:1],
        needs=optional_needs,
        basis_commit_id=commit,
        basis_snapshot_id=StableId("snapshot.stage2m"),
        arm="A",
        writer_token_budget=4000,
    )
    result = assembler.assemble(
        task=task,
        units=traceable,
        needs=optional_needs,
        basis_commit_id=commit,
        basis_snapshot_id=StableId("snapshot.stage2m"),
        arm="A",
        writer_token_budget=4000,
        evidence_ledger_token_budget=one.evidence_ledger.rendered_tokens + 10,
    )

    assert result.status is ContextAssemblyStatus.READY
    assert result.package.budget_report.dropped_optional_ids
    assert "evidence_ledger_token_budget" in set(
        result.package.budget_report.dropped_optional_reasons.values()
    )


def test_evidence_budget_counts_semantic_rendering_not_audit_hash_metadata() -> None:
    task, needs, units, commit = _inputs()
    result = WriterContextAssembler().assemble(
        task=task,
        units=tuple(item for item in units if item.evidence_refs)[:2],
        needs=needs,
        basis_commit_id=commit,
        basis_snapshot_id=StableId("snapshot.stage2m"),
        arm="A",
        writer_token_budget=4000,
    )

    assert result.evidence_ledger.rendered_tokens > 0
    assert result.evidence_ledger.rendered_tokens < WriterContextAssembler._default_token_count(
        result.evidence_ledger.model_dump_json()
    )


def test_need_matching_requires_both_section_and_entity_overlap() -> None:
    _task, needs, units, _commit = _inputs()
    unit = next(item for item in units if item.entity_ids)
    matching = next(
        need
        for need in needs
        if need.expected_section is WriterContextAssembler._section_for_unit(unit)
        and set(need.entity_ids).intersection(unit.entity_ids)
    )
    unrelated = matching.model_copy(update={"entity_ids": (StableId("entity.unrelated"),)})

    assert WriterContextAssembler._need_matches(matching, unit)
    assert not WriterContextAssembler._need_matches(unrelated, unit)
    assert WriterContextAssembler._need_matches(
        matching.model_copy(update={"entity_ids": ()}), unit
    )

    plan_need = matching.model_copy(update={"query_intent": Stage1QueryIntent.PLAN_NODE})
    plan_unit = next(item for item in units if item.unit_kind is RetrievalUnitKind.PLAN_ANCHOR)
    assert WriterContextAssembler._unit_is_legal_for_need(plan_need, plan_unit)
    assert not WriterContextAssembler._unit_is_legal_for_need(plan_need, unit)


def test_explicit_retrieval_lineage_controls_writer_section() -> None:
    task, needs, units, commit = _inputs()
    unit = next(
        item
        for item in units
        if item.unit_kind is RetrievalUnitKind.STATE_ANCHOR and item.evidence_refs
    )
    need = next(
        item
        for item in needs
        if item.expected_section is not WriterContextAssembler._section_for_unit(unit)
        and item.query_intent
        not in {Stage1QueryIntent.PLAN_NODE, Stage1QueryIntent.PLAN_OBLIGATION}
        and (not item.entity_ids or set(item.entity_ids).intersection(unit.entity_ids))
    )

    result = WriterContextAssembler().assemble(
        task=task,
        units=(unit,),
        needs=needs,
        basis_commit_id=commit,
        basis_snapshot_id=StableId("snapshot.stage2m"),
        arm="A",
        writer_token_budget=4000,
        unit_need_ids={unit.unit_id: (need.need_id,)},
    )

    projected = tuple(
        item
        for section in (
            result.package.continuity_constraints,
            result.package.current_world_state,
            result.package.relationship_and_emotion,
            result.package.causal_history,
            result.package.knowledge_and_disclosure,
            result.package.plan_and_obligations,
            result.package.long_range_callbacks,
        )
        for item in section
    )
    assert projected[0].section is need.expected_section
    assert projected[0].need_ids == (need.need_id,)

    plan_need = need.model_copy(update={"query_intent": Stage1QueryIntent.PLAN_NODE})
    blocked = WriterContextAssembler().assemble(
        task=task,
        units=(unit,),
        needs=(plan_need,),
        basis_commit_id=commit,
        basis_snapshot_id=StableId("snapshot.stage2m"),
        arm="A",
        writer_token_budget=4000,
        unit_need_ids={unit.unit_id: (plan_need.need_id,)},
    )
    assert blocked.package.rendered_context == ""


def test_partial_explicit_lineage_leaves_unmapped_units_for_default_matching() -> None:
    task, needs, units, commit = _inputs()
    mapped = next(item for item in units if item.unit_kind is RetrievalUnitKind.STATE_ANCHOR)
    unmapped = next(
        item
        for item in units
        if item.unit_id != mapped.unit_id
        and WriterContextAssembler._section_for_unit(item)
        is not WriterContextAssembler._section_for_unit(mapped)
    )
    need = next(
        item
        for item in needs
        if item.expected_section is WriterContextAssembler._section_for_unit(mapped)
        and item.query_intent
        not in {Stage1QueryIntent.PLAN_NODE, Stage1QueryIntent.PLAN_OBLIGATION}
    )

    result = WriterContextAssembler().assemble(
        task=task,
        units=(mapped, unmapped),
        needs=needs,
        basis_commit_id=commit,
        basis_snapshot_id=StableId("snapshot.stage2m"),
        arm="A",
        writer_token_budget=4000,
        unit_need_ids={mapped.unit_id: (need.need_id,)},
    )

    assert mapped.unit_id in result.package.lineage.retrieval_unit_ids


def test_strict_d9_generator_emits_no_plan_retrieval_need() -> None:
    _task, needs, _units, _commit = _inputs()
    assert all(
        item.query_intent not in {Stage1QueryIntent.PLAN_NODE, Stage1QueryIntent.PLAN_OBLIGATION}
        for item in needs
    )


def test_optional_selection_round_robins_needs_before_second_claim() -> None:
    _task, needs, units, commit = _inputs()
    assembler = WriterContextAssembler()
    source = next(
        item
        for item in units
        if item.unit_kind is RetrievalUnitKind.STATE_ANCHOR and item.evidence_refs
    )
    need_a = next(
        item
        for item in needs
        if item.expected_section is WriterContextAssembler._section_for_unit(source)
        and item.entity_ids
    ).model_copy(update={"requirement": RequirementLevel.OPTIONAL})
    entity_a = need_a.entity_ids[0]
    entity_b = StableId("entity.optional-round-robin-b")
    need_b = need_a.model_copy(
        update={
            "need_id": StableId("need.optional-round-robin-b"),
            "entity_ids": (entity_b,),
        }
    )
    candidates = (
        source.model_copy(
            update={
                "unit_id": StableId("anchor.state.optional-a-1"),
                "entity_ids": (entity_a,),
                "predicate": "optional-a-1",
            }
        ),
        source.model_copy(
            update={
                "unit_id": StableId("anchor.state.optional-a-2"),
                "entity_ids": (entity_a,),
                "predicate": "optional-a-2",
            }
        ),
        source.model_copy(
            update={
                "unit_id": StableId("anchor.state.optional-b-1"),
                "entity_ids": (entity_b,),
                "predicate": "optional-b-1",
            }
        ),
    )
    normalized = assembler._normalizer.normalize(candidates)
    claims, ledger, _gaps = assembler._claims(
        normalized,
        (need_a, need_b),
        commit,
    )
    ordered = assembler._order_optional_by_marginal_value(
        claims,
        {need_a.need_id: need_a, need_b.need_id: need_b},
        {entry.ledger_id: entry for entry in ledger},
    )

    assert {ordered[0].need_ids, ordered[1].need_ids} == {
        (need_a.need_id,),
        (need_b.need_id,),
    }
    assert ordered[2].need_ids == (need_a.need_id,)


def test_evidence_helpers_handle_missing_ledger_and_unlocated_reference() -> None:
    _task, needs, units, commit = _inputs()
    assembler = WriterContextAssembler()
    source = next(item for item in units if item.evidence_refs)
    normalized = assembler._normalizer.normalize((source,))
    claims, ledger, _gaps = assembler._claims(normalized, needs, commit)
    item = claims[0].model_copy(
        update={
            "evidence_ledger_ids": (
                StableId("ledger.missing"),
                ledger[0].ledger_id,
            )
        }
    )
    unlocated = ledger[0].evidence_refs[0].model_copy(update={"chapter_id": None, "span": None})
    entry = ledger[0].model_copy(update={"evidence_refs": (unlocated,)})

    assert (
        assembler._item_evidence_chapter(
            item,
            {entry.ledger_id: entry},
        )
        == -1
    )
    unnumbered = unlocated.model_copy(update={"chapter_id": StableId("chapter.without-number")})
    unnumbered_entry = entry.model_copy(update={"evidence_refs": (unnumbered,)})
    assert (
        assembler._item_evidence_chapter(
            item,
            {unnumbered_entry.ledger_id: unnumbered_entry},
        )
        == -1
    )
    prelude = unlocated.model_copy(update={"chapter_id": StableId("prelude.synthetic")})
    prelude_entry = entry.model_copy(update={"evidence_refs": (prelude,)})
    assert (
        assembler._item_evidence_chapter(
            item,
            {prelude_entry.ledger_id: prelude_entry},
        )
        == 0
    )
    assert assembler._render_evidence_citation(unlocated).endswith("@no-chapter")


def test_receipt_bound_assembly_reports_spec_missing_group_optional_and_ledger_budget() -> None:
    from tests.unit.test_claim_support_selection import _selection

    task, capability, unit, assembler, selection = _selection()
    common: Any = {
        "task": task,
        "claim_variants": selection.claim_variants,
        "support_receipts": selection.support_receipts,
        "cutoff_attestations": selection.cutoff_attestations,
        "needs": (capability,),
        "basis_commit_id": unit.source_commit,
        "basis_snapshot_id": unit.snapshot_id,
        "arm": "A",
    }
    missing = assembler.assemble_from_spec(
        **common,
        assembly_spec=selection.context_assembly_spec,
        support_groups=(),
    )
    assert missing.status is ContextAssemblyStatus.POLICY_BLOCKED
    assert "ASSEMBLY_SPEC_SUPPORT_GROUP_SET_MISMATCH" in missing.diagnostic_codes
    assert any(code.startswith("SUPPORT_GROUP_MISSING:") for code in missing.diagnostic_codes)

    unknown_optional = selection.context_assembly_spec.model_copy(
        update={"ordered_optional_support_group_ids": (StableId("support-group.optional.missing"),)}
    )
    optional = assembler.assemble_from_spec(
        **common,
        assembly_spec=unknown_optional,
        support_groups=selection.support_groups,
    )
    assert optional.package.current_world_state or optional.package.continuity_constraints

    tiny_ledger = selection.context_assembly_spec.model_copy(
        update={"evidence_ledger_token_budget": 1}
    )
    ledger_blocked = assembler.assemble_from_spec(
        **common,
        assembly_spec=tiny_ledger,
        support_groups=selection.support_groups,
    )
    assert ledger_blocked.status is ContextAssemblyStatus.EVIDENCE_INSUFFICIENT
    assert "MANDATORY_SUPPORT_GROUPS_EXCEED_LEDGER_BUDGET" in (ledger_blocked.diagnostic_codes)


def test_optional_sort_and_grounded_extraction_cover_long_range_budget_edges() -> None:
    _task, needs, units, commit = _inputs()
    assembler = WriterContextAssembler()
    source = next(item for item in units if item.evidence_refs)
    claims, ledger, _gaps = assembler._claims(
        assembler._normalizer.normalize((source,)),
        needs,
        commit,
    )
    callback = claims[0].model_copy(update={"section": WriterContextSection.LONG_RANGE_CALLBACKS})
    sort_key = assembler._optional_item_order(
        callback,
        {item.ledger_id: item for item in ledger},
        {item.need_id: item for item in needs},
    )
    assert isinstance(sort_key, tuple)
    matching_need = next(item for item in needs if item.need_id in claims[0].need_ids)
    continuity_need = matching_need.model_copy(update={"need_type": "continuity_constraint"})
    continuity_item = claims[0].model_copy(
        update={"section": WriterContextSection.CURRENT_WORLD_STATE}
    )
    continuity_sort_key = assembler._optional_item_order(
        continuity_item,
        {item.ledger_id: item for item in ledger},
        {continuity_need.need_id: continuity_need},
    )
    assert isinstance(continuity_sort_key, tuple)
    assert assembler._validity_from_facets((StableId("need-facet.unknown"),), {}).value == (
        "uncertain"
    )
    for index, (scope, expected) in enumerate(
        (
            (ExpectedClaimScope.PLANNED, "planned"),
            (ExpectedClaimScope.HISTORICAL, "historical"),
            (ExpectedClaimScope.CURRENT, "current"),
        )
    ):
        facet = NeedFacet(
            need_facet_id=StableId(f"need-facet.validity.{index}"),
            need_id=needs[0].need_id,
            facet_kind=NeedFacetKind.CURRENT_STATE,
            expected_claim_scope=scope,
            derivation_refs=(StableId("derivation.validity"),),
            producer="test",
            producer_version="v1",
            information_scope="test",
        )
        assert (
            assembler._validity_from_facets(
                (facet.need_facet_id,), {facet.need_facet_id: facet}
            ).value
            == expected
        )

    evidence = source.evidence_refs[0]
    assert evidence.span is not None
    short_segments = "".join(f"关键线索{i}。" for i in range(12))
    short_unit = source.model_copy(
        update={
            "unit_kind": RetrievalUnitKind.GROUNDED_BLOCK,
            "text": short_segments,
            "evidence_refs": (
                evidence.model_copy(
                    update={
                        "span": evidence.span.model_copy(
                            update={"start": 0, "end": len(short_segments)}
                        )
                    }
                ),
            ),
        }
    )
    query_need = needs[0].model_copy(update={"query_text": "关键线索"})
    claim, refs = assembler._extract_grounded_claim(short_unit, (query_need,))
    assert claim and len(refs) == 10

    long_segments = "".join(("关键" + "甲" * 200 + "。") for _ in range(8))
    long_unit = short_unit.model_copy(
        update={
            "text": long_segments,
            "evidence_refs": (
                evidence.model_copy(
                    update={
                        "span": evidence.span.model_copy(
                            update={"start": 0, "end": len(long_segments)}
                        )
                    }
                ),
            ),
        }
    )
    claim, refs = assembler._extract_grounded_claim(long_unit, (query_need,))
    assert claim and len(refs) == 4
    assert len(claim) <= 140
