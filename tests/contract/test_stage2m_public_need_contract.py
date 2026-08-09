from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import NeedFacetKind
from novel_agent.domain.writer_context import BenchmarkInformationProfile
from novel_agent.services.memory_benchmark_contract import build_safe_task_contract
from novel_agent.services.need_completion import (
    NeedCompletionEvaluator,
    NeedCompletionStatus,
    NeedFacetClosureState,
)
from novel_agent.services.task_conditioned_need_generation import (
    TaskPlanConditionedNeedGenerator,
)
from tests.fixtures.stage1_synthetic import make_synthetic_bundle
from tests.fixtures.stage2_memory_benchmark import resolved_public_comparison

ROOT = Path(__file__).parents[2]
RUNTIME_FILES = (
    ROOT / "src/novel_agent/domain/memory.py",
    ROOT / "src/novel_agent/domain/writer_context.py",
    ROOT / "src/novel_agent/runtime/memory_controller.py",
    ROOT / "src/novel_agent/services/paired_controller.py",
    ROOT / "src/novel_agent/services/retrieval.py",
    ROOT / "src/novel_agent/services/retrieval_routing.py",
    ROOT / "src/novel_agent/services/task_focus.py",
    ROOT / "src/novel_agent/services/task_conditioned_need_generation.py",
    ROOT / "src/novel_agent/services/writer_context_assembler.py",
)
FORBIDDEN_EVALUATOR_NAMES = {
    "AcceptedEvidenceContract",
    "EvidenceSet",
    "GoldItem",
    "GoldMetricContract",
    "GoldMetricDescriptor",
    "GoldType",
    "PerGoldComparison",
}
FORBIDDEN_SERIALIZED_KEYS = {
    "accepted_evidence_sets",
    "component_ids",
    "gold_id",
    "target_components",
}


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_keys(item) for item in value.values()), set())
    if isinstance(value, (list, tuple)):
        return set().union(*(_keys(item) for item in value), set())
    return set()


def test_runtime_modules_do_not_import_evaluator_gold_contracts() -> None:
    for path in RUNTIME_FILES:
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert imported.isdisjoint(FORBIDDEN_EVALUATOR_NAMES), path


def test_vac_need_facets_are_public_and_never_plan_or_gold_derived() -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    task = build_safe_task_contract(
        case_id=case.case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    generator = TaskPlanConditionedNeedGenerator()
    result = generator.generate_with_lineage(task, bundle.world_roots[0], None)

    assert result.need_completion_spec_version == "need_completion_spec.v1"
    assert result.needs
    assert all(need.completion_spec is not None for need in result.needs)
    assert all(not need.allow_plan for need in result.needs)
    assert all(
        facet.information_scope == "cutoff_safe" and facet.facet_kind is not NeedFacetKind.PLAN_NODE
        for need in result.needs
        for facet in need.need_facets
    )
    payload = [need.model_dump(mode="json") for need in result.needs]
    assert _keys(payload).isdisjoint(FORBIDDEN_SERIALIZED_KEYS)

    with pytest.raises(ValueError, match="cannot receive a future PlanRoot"):
        generator.generate_with_lineage(task, bundle.world_roots[0], bundle.plan_roots[0])


def test_task_intent_only_profile_uses_intent_but_never_plan() -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    task = build_safe_task_contract(
        case_id=case.case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.TASK_INTENT_ONLY,
        task_intent="准备 林澈 的历史记忆与伤势状态",
    )
    assert task.task_intent == "准备 林澈 的历史记忆与伤势状态"
    assert "任务意图" in task.task_text or "任务意图" in task.task_text
    assert "不得使用作者计划节点" in task.task_text

    with pytest.raises(ValueError, match="cannot receive a future PlanRoot"):
        TaskPlanConditionedNeedGenerator().generate(
            task,
            bundle.world_roots[0],
            bundle.plan_roots[0],
        )

    needs = TaskPlanConditionedNeedGenerator().generate(task, bundle.world_roots[0], None)
    assert needs
    entity = bundle.world_roots[0].entities[0]
    task_focus_needs = tuple(need for need in needs if entity.entity_id in need.entity_ids)
    assert task_focus_needs
    assert all(not need.retrieval_may_return_plan for need in needs)
    assert all(not need.claim_may_cite_plan for need in needs)
    assert all(not need.planner_may_read_plan for need in needs)


def test_mandatory_irreducible_facets_require_complete_public_closure() -> None:
    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    task = build_safe_task_contract(
        case_id=case.case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
    )
    needs = TaskPlanConditionedNeedGenerator().generate(
        task,
        bundle.world_roots[0],
        bundle.plan_roots[0],
    )
    need = next(item for item in needs if item.need_type == "capability_boundary")
    spec = need.completion_spec
    assert spec is not None
    assert len(spec.required_need_facet_ids) == 2
    assert spec.irreducible_need_facet_ids == spec.required_need_facet_ids
    first, second = spec.required_need_facet_ids
    source = StableId("source.public.closure")
    chapter = StableId("chapter.public.20")

    partial = NeedCompletionEvaluator().evaluate(
        spec,
        NeedFacetClosureState(
            need_id=need.need_id,
            verified_need_facet_ids=(first,),
            evidence_source_ids_by_facet={first.root: (source,)},
            evidence_chapter_ids_by_facet={first.root: (chapter,)},
            current_claim_facet_ids=(first,),
        ),
    )
    assert partial.status is NeedCompletionStatus.PARTIAL
    assert partial.missing_irreducible_need_facet_ids == (second,)

    complete = NeedCompletionEvaluator().evaluate(
        spec,
        NeedFacetClosureState(
            need_id=need.need_id,
            verified_need_facet_ids=(first, second),
            evidence_source_ids_by_facet={
                first.root: (source,),
                second.root: (source,),
            },
            evidence_chapter_ids_by_facet={
                first.root: (chapter,),
                second.root: (chapter,),
            },
            current_claim_facet_ids=(first, second),
        ),
    )
    assert complete.status is NeedCompletionStatus.REQUIRED_FACETS_CLOSED
    assert complete.missing_required_need_facet_ids == ()


def test_frozen_production_context_carries_only_public_completion_contracts() -> None:
    _bundle, _case, _public, _runner, comparison = resolved_public_comparison()
    context = comparison.deterministic.context

    assert context.need_facets
    assert context.need_completion_specs
    assert {item.need_id for item in context.need_facets} == {
        item.need_id for item in context.need_completion_specs
    }
    assert _keys(context.model_dump(mode="json")).isdisjoint(FORBIDDEN_SERIALIZED_KEYS)
