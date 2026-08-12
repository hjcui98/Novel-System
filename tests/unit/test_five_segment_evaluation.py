from __future__ import annotations

from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.memory import (
    CandidatePool,
    NeedRisk,
    RequirementLevel,
    ResolutionPath,
    Stage1MemoryNeed,
    Stage1QueryIntent,
)
from novel_agent.domain.memory_benchmark import (
    BenchmarkInformationProfile,
    EvidenceLedger,
    EvidenceLedgerEntry,
    FiveSegmentReport,
    GoldBlindness,
    GoldNeedBinding,
    GoldNeedSpec,
    SegmentAvailability,
)
from novel_agent.services.memory_benchmark_contract import build_safe_task_contract
from novel_agent.services.memory_benchmark_evaluation import MemoryBenchmarkEvaluator
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


def _need(
    need_id: str,
    need_type: str,
    *,
    entity_ids: tuple[StableId, ...] = (),
    facets: tuple[str, ...] = (),
    trigger_chapters: tuple[int, ...] = (),
) -> Stage1MemoryNeed:
    return Stage1MemoryNeed(
        need_id=StableId(need_id),
        run_id=RunId(f"run.{need_id}"),
        task_id=TaskId(f"task.{need_id}"),
        base_commit=CommitId("sha256:" + "a" * 64),
        chapter_target=21,
        need_type=need_type,
        query_intent=Stage1QueryIntent.SEMANTIC_HISTORY,
        query_text="query",
        entity_ids=entity_ids,
        why_needed="test",
        risk_level=NeedRisk.MEDIUM,
        requirement=RequirementLevel.OPTIONAL,
        preferred_resolution_path=ResolutionPath.ANCHOR_FIRST,
        allowed_candidate_pools=(CandidatePool.ANCHOR,),
        stop_condition="done",
        trigger_plan_chapters=trigger_chapters,
    )


def _spec(
    gold_id: str,
    *,
    blindness: GoldBlindness = GoldBlindness.BLIND_RECOVERABLE,
    scopes: tuple[str, ...] = ("knowledge",),
    entities: tuple[str, ...] = ("陈长生",),
    facets: tuple[str, ...] = ("KNOWLEDGE_BOUNDARY",),
) -> GoldNeedSpec:
    return GoldNeedSpec(
        gold_id=StableId(gold_id),
        blindness=blindness,
        required_need_scopes=scopes,
        required_entities=entities,
        required_facets=facets,
    )


def _empty_ledger() -> EvidenceLedger:
    return EvidenceLedger(contract_version="v1", rendered_tokens=0)


def test_gold_need_spec_domain_edges() -> None:
    with pytest.raises(ValidationError, match="at least one component"):
        GoldNeedSpec(gold_id=StableId("g"))
    with pytest.raises(ValidationError, match="scopes must be unique"):
        _spec("g1", scopes=("current", "current"))
    with pytest.raises(ValidationError, match="entities must be unique"):
        _spec("g2", entities=("陈长生", "陈长生"))
    with pytest.raises(ValidationError, match="facets must be unique"):
        _spec("g3", facets=("CURRENT_STATE", "CURRENT_STATE"))
    with pytest.raises(ValidationError, match="blindness"):
        GoldNeedSpec(
            gold_id=StableId("g4"),
            blindness="unknown-kind",  # type: ignore[arg-type]
            required_need_scopes=("current",),
        )


def test_five_segment_report_consistency_edges() -> None:
    with pytest.raises(ValidationError, match="cannot exceed total plan goals"):
        FiveSegmentReport(
            plan_goals_total=1,
            plan_goals_covered=2,
            plan_goal_coverage=1.0,
            need_recall_total=0,
            need_recall_matched=0,
            need_recall=1.0,
            evidence_recall=1.0,
            completion_accuracy=1.0,
            future_leakage_count=0,
            plan_citation_count=0,
            plan_leakage_count=0,
        )
    with pytest.raises(ValidationError, match="matched need components"):
        FiveSegmentReport(
            plan_goals_total=0,
            plan_goals_covered=0,
            plan_goal_coverage=1.0,
            need_recall_total=1,
            need_recall_matched=2,
            need_recall=1.0,
            evidence_recall=1.0,
            completion_accuracy=1.0,
            future_leakage_count=0,
            plan_citation_count=0,
            plan_leakage_count=0,
        )
    with pytest.raises(ValidationError, match="cannot exceed plan citations"):
        FiveSegmentReport(
            plan_goals_total=0,
            plan_goals_covered=0,
            plan_goal_coverage=1.0,
            need_recall_total=0,
            need_recall_matched=0,
            need_recall=1.0,
            evidence_recall=1.0,
            completion_accuracy=1.0,
            future_leakage_count=0,
            plan_citation_count=1,
            plan_leakage_count=2,
        )
    base = {
        "plan_goals_total": 0,
        "plan_goals_covered": 0,
        "plan_goal_coverage": 1.0,
        "need_recall_total": 0,
        "need_recall_matched": 0,
        "need_recall": 1.0,
        "evidence_recall": 1.0,
        "completion_accuracy": 1.0,
        "future_leakage_count": 0,
        "plan_citation_count": 0,
        "plan_leakage_count": 0,
    }
    invalid_updates = (
        ({"planner_fallback_rate": 1.0}, "requires its typed reason"),
        ({"planner_fallback_reason": "unexpected"}, "cannot carry"),
        (
            {"grounded_status_counts": (1, 1, 0), "grounding_success_rate": 1.0},
            "grounding rate/counts",
        ),
        (
            {"evidence_recall_total": 0, "evidence_recall_matched": 1},
            "matched evidence",
        ),
        (
            {"completion_gold_total": 0, "completion_weight_total": 1.0},
            "completion denominator",
        ),
        (
            {
                "plan_goal_coverage": None,
                "plan_goal_availability": SegmentAvailability.AVAILABLE,
            },
            "metric availability",
        ),
    )
    for update, message in invalid_updates:
        with pytest.raises(ValidationError, match=message):
            FiveSegmentReport(**(base | update))


def test_gold_need_binding_availability_edges() -> None:
    base = {
        "profile": BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        "gold_id": StableId("gold.edge"),
    }
    with pytest.raises(ValidationError, match="requires reason and no selected Need"):
        GoldNeedBinding.model_validate(
            base
            | {
                "availability": SegmentAvailability.UNAVAILABLE,
                "selected_need_id": StableId("need.edge"),
                "unavailable_reason": "reason",
            }
        )
    with pytest.raises(ValidationError, match="cannot carry unavailable reason"):
        GoldNeedBinding.model_validate(base | {"unavailable_reason": "reason"})


def test_five_segment_evaluation_measures_goals_needs_and_leakage() -> None:
    from novel_agent.domain.memory_benchmark import BenchmarkInformationProfile
    from novel_agent.services.memory_benchmark_contract import build_safe_task_contract
    from novel_agent.services.task_conditioned_need_generation import (
        TaskPlanConditionedNeedGenerator,
    )

    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    plan = next(root for root in bundle.plan_roots if root.root_hash == case.input_plan_root)
    world = bundle.world_roots[0]
    entity = world.entities[0]
    task = build_safe_task_contract(
        case_id=case.case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
    )
    needs = TaskPlanConditionedNeedGenerator().generate(task, world, plan)
    knowledge = next(item for item in needs if item.need_type == "knowledge_boundary")
    assert knowledge.need_facets
    planner_goal_need = knowledge.model_copy(
        update={
            "need_id": StableId("need.planner.goal-copy"),
            "need_type": "plan_obligation",
            "semantic_question": plan.chapter_goals[0].summary,
            "trigger_plan_chapters": (plan.chapter_goals[0].chapter_index,),
            "trigger_plan_goal": plan.chapter_goals[0].summary,
            "planner_artifact_ref": ArtifactId("sha256:" + "b" * 64),
            "planned_draft_id": "goal-copy",
            "validated_need_set_hash": ArtifactId("sha256:" + "c" * 64),
        }
    )
    unmatched_plan_need = knowledge.model_copy(
        update={
            "need_id": StableId("need.planner.unmatched"),
            "need_type": "plan_obligation",
            "query_text": "与任何章节目标都不相同的计划文本",
            "semantic_question": "",
            "trigger_plan_chapters": (99,),
        }
    )
    needs = (*needs, planner_goal_need, unmatched_plan_need)
    validated_goal_need = planner_goal_need.model_copy(
        update={
            "semantic_question": "此前有哪些历史事实约束该目标?",
            "trigger_plan_goal": plan.chapter_goals[0].summary,
        }
    )
    needs = (*needs, validated_goal_need)
    plan_goals = tuple(
        goal
        for goal in plan.chapter_goals
        if case.target_range[0] <= goal.chapter_index <= case.target_range[1]
    )
    assert plan_goals
    specs = (
        _spec(
            "gold.a",
            scopes=("knowledge",),
            entities=(entity.internal_label,),
            facets=("KNOWLEDGE_BOUNDARY",),
        ),
        _spec(
            "gold.b",
            blindness=GoldBlindness.PLAN_DEPENDENT,
            scopes=("planned",),
            entities=("不存在的人",),
            facets=("PLAN_NODE",),
        ),
        _spec(
            "gold.c",
            scopes=("unknown-scope",),
            entities=("另一个不存在的人",),
            facets=("UNKNOWN_FACET",),
        ),
    )
    ledger = EvidenceLedger(
        contract_version="v1",
        rendered_tokens=0,
        entries=(
            EvidenceLedgerEntry(
                ledger_id=StableId("ledger.plan"),
                plan_node_ids=(StableId("goal.plan.1"),),
                claim_excerpt="计划义务",
                source_commit=CommitId("sha256:" + "a" * 64),
                information_scope="author_plan",
                need_ids=(knowledge.need_id,),
            ),
        ),
    )
    evaluator = MemoryBenchmarkEvaluator()
    assert validated_goal_need.planner_artifact_ref is not None
    report = evaluator.evaluate_five_segments(
        needs=needs,
        gold_need_specs=specs,
        plan_goals=plan_goals,
        gold_items=tuple(
            case.observed_use_gold[0].model_copy(update={"gold_id": StableId(f"gold.{suffix}")})
            for suffix in ("a", "b", "c")
        ),
        evidence_ledger=ledger,
        completion_accuracy=0.5,
        future_leakage_count=1,
        entity_id_by_label={entity.internal_label: entity.entity_id},
        planner_artifact_ref=ArtifactRef(
            artifact_id=validated_goal_need.planner_artifact_ref,
            media_type="application/vnd.novel-agent.planner-invocation+json",
            byte_length=1,
            schema_version=SchemaVersion("1.0.0"),
        ),
    )
    assert report.plan_goals_total == len(plan_goals)
    assert report.plan_goals_covered >= 1
    assert report.plan_goal_coverage is not None
    assert report.plan_goal_coverage > 0.0
    # Each Gold binds to one coherent Need. The plan-only annotation is not a
    # historical Need/claim denominator under strict D9.
    assert report.need_recall_total == 2
    assert report.need_recall_matched == 1
    assert report.need_recall == 0.5
    assert report.completion_accuracy == 0.5
    assert report.future_leakage_count == 1
    assert report.plan_citation_count == 1
    # The only plan citation belongs to the knowledge need (not a plan-channel
    # need), so it counts as plan leakage.
    assert report.plan_leakage_count == 1
    assert report.evidence_recall == 0.0
    assert report.legacy_plan_obligation_coverage is None


def test_five_segment_evaluation_is_empty_safe() -> None:
    evaluator = MemoryBenchmarkEvaluator()
    report = evaluator.evaluate_five_segments(
        needs=(),
        gold_need_specs=(),
        plan_goals=(),
        gold_items=(),
        evidence_ledger=EvidenceLedger(contract_version="v1", rendered_tokens=0),
        completion_accuracy=0.0,
        future_leakage_count=0,
    )
    assert report.plan_goals_total == 0
    assert report.plan_goal_coverage is None
    assert report.need_recall_total == 0
    assert report.need_recall is None
    assert report.evidence_recall is None
    assert report.completion_accuracy is None
    assert report.plan_citation_count == 0
    assert report.plan_leakage_count == 0


def test_five_segment_unavailable_bindings_are_typed_per_gold() -> None:
    bundle = make_synthetic_bundle()
    source = bundle.case_manifests[0].observed_use_gold[0]
    gold_items = tuple(
        source.model_copy(update={"gold_id": StableId(f"gold.unavailable.{index}")})
        for index in range(4)
    )
    specs = (
        _spec("gold.unavailable.0", blindness=GoldBlindness.HINDSIGHT_ONLY),
        _spec("gold.unavailable.1", blindness=GoldBlindness.PLAN_DEPENDENT),
        _spec("gold.unavailable.2"),
    )
    report = MemoryBenchmarkEvaluator().evaluate_five_segments(
        needs=(),
        gold_need_specs=specs,
        plan_goals=(),
        gold_items=gold_items,
        evidence_ledger=EvidenceLedger(contract_version="v1", rendered_tokens=0),
        completion_accuracy=0.0,
        future_leakage_count=0,
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    assert [binding.unavailable_reason for binding in report.bindings] == [
        "HINDSIGHT_ONLY",
        "PROFILE_BLINDNESS_NOT_APPLICABLE",
        "NO_GENERATED_NEEDS",
        "MISSING_GOLD_NEED_SPEC",
    ]
    assert report.missing_spec_gold_ids == (StableId("gold.unavailable.3"),)


def test_five_segment_binding_never_unions_needs_or_wrong_need_evidence() -> None:
    from novel_agent.domain.memory_benchmark import BenchmarkInformationProfile
    from novel_agent.services.memory_benchmark_contract import build_safe_task_contract
    from novel_agent.services.task_conditioned_need_generation import (
        TaskPlanConditionedNeedGenerator,
    )

    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    world = bundle.world_roots[0]
    plan = bundle.plan_roots[0]
    task = build_safe_task_contract(
        case_id=case.case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
    )
    generated = TaskPlanConditionedNeedGenerator().generate(task, world, plan)
    knowledge = next(item for item in generated if item.need_type == "knowledge_boundary")
    other = next(item for item in generated if item.need_id != knowledge.need_id)
    entity = world.entities[0]
    knowledge_without_entity = knowledge.model_copy(update={"entity_ids": ()})
    entity_without_facet = other.model_copy(
        update={"entity_ids": (entity.entity_id,), "need_facets": ()}
    )
    gold = case.observed_use_gold[0].model_copy(update={"gold_id": StableId("gold.bound")})
    spec = _spec(
        "gold.bound",
        scopes=("knowledge",),
        entities=(entity.internal_label,),
        facets=("KNOWLEDGE_BOUNDARY",),
    )
    wrong_need_ledger = EvidenceLedger(
        contract_version="v1",
        rendered_tokens=1,
        entries=(
            EvidenceLedgerEntry(
                ledger_id=StableId("ledger.wrong-need"),
                evidence_refs=gold.evidence_refs,
                claim_excerpt="wrong Need evidence",
                source_commit=world.source_commit,
                information_scope="cutoff_safe",
                need_ids=(entity_without_facet.need_id,),
            ),
        ),
    )
    report = MemoryBenchmarkEvaluator().evaluate_five_segments(
        needs=(knowledge_without_entity, entity_without_facet),
        gold_need_specs=(spec,),
        plan_goals=plan.chapter_goals,
        gold_items=(gold,),
        evidence_ledger=wrong_need_ledger,
        completion_accuracy=0.0,
        future_leakage_count=0,
        entity_id_by_label={entity.internal_label: entity.entity_id},
    )
    assert report.need_recall == 0.0
    assert report.bindings[0].full_need_match is False
    assert report.evidence_recall == 0.0

    correct_ledger = wrong_need_ledger.model_copy(
        update={
            "entries": (
                wrong_need_ledger.entries[0].model_copy(
                    update={"need_ids": (knowledge_without_entity.need_id,)}
                ),
            )
        }
    )
    matched = MemoryBenchmarkEvaluator().evaluate_five_segments(
        needs=(knowledge_without_entity,),
        gold_need_specs=(spec,),
        plan_goals=plan.chapter_goals,
        gold_items=(gold,),
        evidence_ledger=correct_ledger,
        completion_accuracy=0.0,
        future_leakage_count=0,
        entity_id_by_label={entity.internal_label: entity.entity_id},
    )
    assert matched.evidence_recall == 1.0

    no_evidence_gold = gold.model_copy(update={"evidence_refs": (), "accepted_evidence_sets": ()})
    no_evidence = MemoryBenchmarkEvaluator().evaluate_five_segments(
        needs=(knowledge_without_entity,),
        gold_need_specs=(spec,),
        plan_goals=plan.chapter_goals,
        gold_items=(no_evidence_gold,),
        evidence_ledger=EvidenceLedger(contract_version="v1", rendered_tokens=0),
        completion_accuracy=0.0,
        future_leakage_count=0,
        entity_id_by_label={entity.internal_label: entity.entity_id},
    )
    assert no_evidence.evidence_recall is None


def test_compiler_loads_gold_need_specs_from_sibling_yaml() -> None:
    from pathlib import Path

    from novel_agent.services.human_benchmark_compiler import (
        HumanBenchmarkCompileError,
        HumanBenchmarkCompiler,
    )

    pilot = Path(__file__).parents[2] / "benchmarks/private/ztj_memory_pilot_v0.1"
    compiler = HumanBenchmarkCompiler()
    bundle = compiler.compile(pilot)
    case = next(item for item in bundle.case_manifests if item.case_id.root == "ZTJ-P003")
    assert case.gold_need_specs
    by_id = {spec.gold_id: spec for spec in case.gold_need_specs}
    assert by_id[StableId("P003-G06")].blindness is GoldBlindness.PLAN_DEPENDENT
    assert "徐有容" in by_id[StableId("P003-G06")].required_entities
    assert "KNOWLEDGE_BOUNDARY" in by_id[StableId("P003-G06")].required_facets
    assert by_id[StableId("P003-G01")].blindness is GoldBlindness.BLIND_RECOVERABLE

    # Missing sibling spec file yields no specs.
    assert (
        compiler._gold_need_specs({"gold_file_private": "missing.yaml"}, {"items": []}, pilot) == ()
    )

    # Malformed spec files fail closed (tmp copies of the case directory).
    import shutil

    malformed_root = Path(__file__).parents[2] / "tmp" / "five-segment-malformed"
    case_dir = malformed_root / "cases" / "ZTJ-P003"
    shutil.rmtree(malformed_root, ignore_errors=True)
    case_dir.mkdir(parents=True)
    gold_path = Path("benchmarks/private/ztj_memory_pilot_v0.1/cases/ZTJ-P003/gold.yaml")
    shutil.copy(gold_path, case_dir / "gold.yaml")
    gold_data = yaml.safe_load(gold_path.read_text("utf-8"))
    manifest = {"gold_file_private": "cases/ZTJ-P003/gold.yaml"}

    (case_dir / "gold_need_spec.yaml").write_text("items: not-a-list\n", encoding="utf-8")
    with pytest.raises(HumanBenchmarkCompileError, match="items must be a list"):
        compiler._gold_need_specs(manifest, gold_data, malformed_root)

    (case_dir / "gold_need_spec.yaml").write_text("items: [not-a-dict]\n", encoding="utf-8")
    with pytest.raises(HumanBenchmarkCompileError, match="must be an object"):
        compiler._gold_need_specs(manifest, gold_data, malformed_root)

    (case_dir / "gold_need_spec.yaml").write_text(
        "items:\n- id: UNKNOWN-GOLD\n  required_need_scopes: [current]\n",
        encoding="utf-8",
    )
    with pytest.raises(HumanBenchmarkCompileError, match="unknown Gold"):
        compiler._gold_need_specs(manifest, gold_data, malformed_root)

    (case_dir / "gold_need_spec.yaml").write_text(
        "items:\n- id: P003-G01\n  blindness: not-a-kind\n  required_need_scopes: [current]\n",
        encoding="utf-8",
    )
    with pytest.raises(HumanBenchmarkCompileError, match="blindness is invalid"):
        compiler._gold_need_specs(manifest, gold_data, malformed_root)


def test_runtime_world_labels_bind_entities_while_oracle_namespace_differs() -> None:
    """The evaluator must use the frozen runtime World entity IDs, not the
    bundle oracle World namespace (plan §2.4 / §4.2)."""
    from novel_agent.services.memory_benchmark_evaluation import MemoryBenchmarkEvaluator

    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    plan = next(root for root in bundle.plan_roots if root.root_hash == case.input_plan_root)
    oracle = bundle.world_roots[0]
    oracle_entity = oracle.entities[0]
    runtime_id = StableId("entity.runtime.different-namespace")
    task = build_safe_task_contract(
        case_id=case.case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
    )
    need = _entity_bound_need(task, oracle_entity.entity_id, oracle_entity.internal_label)
    need = need.model_copy(update={"entity_ids": (runtime_id,)})

    evaluator = MemoryBenchmarkEvaluator()
    entity_id_by_label = {oracle_entity.internal_label: runtime_id}
    report = evaluator.evaluate_five_segments(
        needs=(need,),
        gold_need_specs=(
            _spec(
                "gold.runtime",
                scopes=(need.need_facets[0].expected_claim_scope.value,),
                entities=(oracle_entity.internal_label,),
                facets=(need.need_facets[0].facet_kind.name,),
            ),
        ),
        plan_goals=(plan.chapter_goals[0],),
        gold_items=(
            case.observed_use_gold[0].model_copy(update={"gold_id": StableId("gold.runtime")}),
        ),
        evidence_ledger=_empty_ledger(),
        completion_accuracy=0.0,
        future_leakage_count=0,
        entity_id_by_label=entity_id_by_label,
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
    )
    binding = next(item for item in report.bindings if item.gold_id.root == "gold.runtime")
    assert binding.entity_hits == (oracle_entity.internal_label,)
    assert binding.entity_misses == ()


def test_oracle_world_labels_do_not_bind_runtime_entities() -> None:
    """A bundle oracle World with the same label but a different ID namespace
    must not produce entity hits for runtime-bound Needs (plan §2.4)."""
    from novel_agent.services.memory_benchmark_evaluation import MemoryBenchmarkEvaluator

    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    plan = next(root for root in bundle.plan_roots if root.root_hash == case.input_plan_root)
    oracle = bundle.world_roots[0]
    oracle_entity = oracle.entities[0]
    runtime_id = StableId("entity.runtime.other-id")
    task = build_safe_task_contract(
        case_id=case.case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
    )
    need = _entity_bound_need(task, oracle_entity.entity_id, oracle_entity.internal_label)
    need = need.model_copy(update={"entity_ids": (runtime_id,)})

    evaluator = MemoryBenchmarkEvaluator()
    oracle_label_map = {oracle_entity.internal_label: oracle_entity.entity_id}
    report = evaluator.evaluate_five_segments(
        needs=(need,),
        gold_need_specs=(
            _spec(
                "gold.oracle-wrong",
                scopes=(need.need_facets[0].expected_claim_scope.value,),
                entities=(oracle_entity.internal_label,),
                facets=(need.need_facets[0].facet_kind.name,),
            ),
        ),
        plan_goals=(plan.chapter_goals[0],),
        gold_items=(
            case.observed_use_gold[0].model_copy(update={"gold_id": StableId("gold.oracle-wrong")}),
        ),
        evidence_ledger=_empty_ledger(),
        completion_accuracy=0.0,
        future_leakage_count=0,
        entity_id_by_label=oracle_label_map,
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
    )
    binding = next(item for item in report.bindings if item.gold_id.root == "gold.oracle-wrong")
    assert binding.entity_hits == ()


def test_ambiguous_runtime_label_never_binds() -> None:
    """A label shared by two runtime entities must not yield an entity hit."""
    from novel_agent.services.memory_benchmark_evaluation import MemoryBenchmarkEvaluator

    bundle = make_synthetic_bundle()
    case = bundle.case_manifests[0]
    plan = next(root for root in bundle.plan_roots if root.root_hash == case.input_plan_root)
    oracle = bundle.world_roots[0]
    entity = oracle.entities[0]
    task = build_safe_task_contract(
        case_id=case.case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
    )
    need = _entity_bound_need(task, entity.entity_id, entity.internal_label)

    evaluator = MemoryBenchmarkEvaluator()
    report = evaluator.evaluate_five_segments(
        needs=(need,),
        gold_need_specs=(
            _spec(
                "gold.ambiguous",
                scopes=(need.need_facets[0].expected_claim_scope.value,),
                entities=(entity.internal_label,),
                facets=(need.need_facets[0].facet_kind.name,),
            ),
        ),
        plan_goals=(plan.chapter_goals[0],),
        gold_items=(
            case.observed_use_gold[0].model_copy(update={"gold_id": StableId("gold.ambiguous")}),
        ),
        evidence_ledger=_empty_ledger(),
        completion_accuracy=0.0,
        future_leakage_count=0,
        entity_id_by_label=None,
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
    )
    assert all(not binding.entity_hits for binding in report.bindings)


def _entity_bound_need(task: Any, entity_id: StableId, label: str) -> Stage1MemoryNeed:
    """One license-free entity-bound Need with a single CURRENT_STATE facet."""
    from novel_agent.domain.memory import (
        ExpectedClaimScope,
        FacetEvidenceRequirement,
        NeedCompletionSpec,
        NeedFacet,
        NeedFacetKind,
        NeedGapPolicy,
        NeedUncertaintyPolicy,
    )
    from novel_agent.domain.writer_context import WriterContextSection

    need_id = StableId("need.stage2m.entity.runtime-test.state")
    facet = NeedFacet(
        need_facet_id=StableId("need-facet.runtime-test.CURRENT_STATE"),
        need_id=need_id,
        facet_kind=NeedFacetKind.CURRENT_STATE,
        expected_claim_scope=ExpectedClaimScope.CURRENT,
        derivation_refs=(task.task_id,),
        producer="test",
        producer_version="test.v1",
        information_scope="cutoff_safe",
    )
    completion = NeedCompletionSpec(
        need_id=need_id,
        required_need_facet_ids=(facet.need_facet_id,),
        irreducible_need_facet_ids=(facet.need_facet_id,),
        evidence_requirement_by_facet={
            facet.need_facet_id.root: FacetEvidenceRequirement.CUTOFF_CURRENT_SOURCE
        },
        min_distinct_evidence_sources=1,
        min_distinct_chapters=1,
        require_current_claim=True,
        require_causal_history=False,
        uncertainty_policy=NeedUncertaintyPolicy.ALLOW_GAP_ONLY,
        gap_policy=NeedGapPolicy.FAIL_MANDATORY,
        producer="test",
        producer_version="test.v1",
    )
    return Stage1MemoryNeed(
        need_id=need_id,
        run_id=RunId("run.stage2m.test"),
        task_id=TaskId(task.task_id.root),
        base_commit=CommitId("sha256:" + "a" * 64),
        horizon_target=(task.target_chapter_start, task.target_chapter_end),
        need_type="current_state",
        query_intent=Stage1QueryIntent.SEMANTIC_HISTORY,
        query_text=f"{label} 当前状态是什么?",
        semantic_question="",
        trigger_plan_chapters=(),
        trigger_plan_goal="",
        entity_ids=(entity_id,),
        access_scope="writer_safe",
        allow_plan=False,
        planner_may_read_plan=True,
        retrieval_may_return_plan=False,
        claim_may_cite_plan=False,
        legacy_allow_plan=False,
        why_needed="test",
        risk_level=NeedRisk.HIGH,
        requirement=RequirementLevel.MANDATORY,
        preferred_resolution_path=ResolutionPath.ANCHOR_FIRST,
        allowed_candidate_pools=(CandidatePool.ANCHOR,),
        expected_evidence_types=("text_span",),
        stop_condition="one current claim",
        purpose="test",
        expected_section=WriterContextSection.CURRENT_WORLD_STATE,
        focus_ids=(StableId("focus.runtime-test"),),
        priority=90,
        query_hints=(f"{label} 状态",),
        need_facets=(facet,),
        completion_spec=completion,
    )
