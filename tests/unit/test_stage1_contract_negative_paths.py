from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from novel_agent.domain.benchmark import (
    BenchmarkBundle,
    BenchmarkCaseManifest,
    ChapterDocument,
    ChapterSummaryRootDocument,
    GoldKind,
    PlanRootDocument,
    ReplayCaseManifest,
    ReplayStateCheckpoint,
    SceneDocument,
    TextRootDocument,
)
from novel_agent.domain.changes import (
    StateTransitionEdge,
    StateTransitionPolicy,
    StateTransitionRule,
)
from novel_agent.domain.ids import SchemaVersion, StableId
from novel_agent.domain.memory import (
    CandidatePool,
    HorizonNeedSet,
    Stage1MemoryNeed,
    Stage1QueryIntent,
    WorldRootDocument,
)
from novel_agent.domain.world import Entity
from tests.fixtures.stage1_synthetic import make_synthetic_bundle
from tests.unit.test_stage1_retrieval import need


def invalid(model_type: type[Any], model: Any, **updates: Any) -> None:
    with pytest.raises(ValidationError):
        model_type.model_validate({**model.model_dump(), **updates}, strict=True)


def test_scene_chapter_and_text_root_structural_guards() -> None:
    bundle = make_synthetic_bundle()
    text = bundle.text_roots[0]
    chapter = text.chapters[0]
    scene = chapter.scenes[0]
    block = scene.blocks[0]

    invalid(SceneDocument, scene, blocks=(block, block))
    invalid(
        SceneDocument,
        scene,
        blocks=(block.model_copy(update={"scene_id": StableId("scene.wrong")}),),
    )
    invalid(
        SceneDocument,
        scene,
        blocks=(
            block.model_copy(update={"block_id": StableId("block.later"), "narrative_index": 2}),
            block.model_copy(update={"block_id": StableId("block.earlier"), "narrative_index": 1}),
        ),
    )
    invalid(ChapterDocument, chapter, scenes=(scene, scene))
    wrong_chapter_block = block.model_copy(update={"chapter_id": StableId("chapter.wrong")})
    invalid(
        ChapterDocument,
        chapter,
        scenes=(scene.model_copy(update={"blocks": (wrong_chapter_block,)}),),
    )
    invalid(
        ChapterDocument,
        chapter,
        scenes=(
            SceneDocument(scene_id=StableId("scene.later"), scene_index=2),
            SceneDocument(scene_id=StableId("scene.earlier"), scene_index=1),
        ),
    )
    invalid(TextRootDocument, text, chapters=(text.chapters[1], text.chapters[0]))
    second_chapter = text.chapters[1]
    duplicate_id_block = block.model_copy(
        update={
            "chapter_id": second_chapter.chapter_id,
            "scene_id": second_chapter.scenes[0].scene_id,
        }
    )
    duplicate_block_chapter = second_chapter.model_copy(
        update={
            "scenes": (
                second_chapter.scenes[0].model_copy(update={"blocks": (duplicate_id_block,)}),
            )
        }
    )
    invalid(
        TextRootDocument,
        text,
        chapters=(text.chapters[0], duplicate_block_chapter),
    )


def test_plan_case_and_bundle_reference_shape_guards() -> None:
    bundle = make_synthetic_bundle()
    plan = bundle.plan_roots[0]
    node = plan.nodes[0]
    goal = plan.chapter_goals[0]
    case = bundle.case_manifests[0]
    summaries = bundle.summary_roots[0]

    invalid(PlanRootDocument, plan, nodes=(node, node))
    invalid(
        PlanRootDocument,
        plan,
        nodes=(node.model_copy(update={"parent_id": StableId("plan.missing")}),),
    )
    invalid(PlanRootDocument, plan, chapter_goals=(goal, goal))
    invalid(BenchmarkCaseManifest, case, history_range=(0, 20))
    invalid(BenchmarkCaseManifest, case, target_range=(20, 23))
    wrong_kind = case.observed_use_gold[0].model_copy(update={"kind": GoldKind.PLAN_OBLIGATION})
    invalid(BenchmarkCaseManifest, case, observed_use_gold=(wrong_kind,))
    wrong_chapter = case.observed_use_gold[0].model_copy(update={"target_chapters": (24,)})
    invalid(BenchmarkCaseManifest, case, observed_use_gold=(wrong_chapter,))

    invalid(
        ChapterSummaryRootDocument,
        summaries,
        summaries=(summaries.summaries[1], summaries.summaries[0]),
    )
    duplicate_chapter_id = summaries.summaries[1].model_copy(
        update={"chapter_id": summaries.summaries[0].chapter_id}
    )
    invalid(
        ChapterSummaryRootDocument,
        summaries,
        summaries=(summaries.summaries[0], duplicate_chapter_id),
    )

    invalid(BenchmarkBundle, bundle, text_roots=(bundle.text_roots[0],) * 2)
    invalid(BenchmarkBundle, bundle, summary_roots=(summaries, summaries))
    invalid(BenchmarkBundle, bundle, plan_roots=(bundle.plan_roots[0],) * 2)
    invalid(BenchmarkBundle, bundle, world_roots=(bundle.world_roots[0],) * 2)
    invalid(BenchmarkBundle, bundle, case_manifests=(case, case))
    replay = bundle.replay_manifests[0]
    invalid(ReplayCaseManifest, replay, chapter_range=(0, 3))
    invalid(ReplayCaseManifest, replay, chapter_range=(22, 23))
    invalid(ReplayCaseManifest, replay, gate_eligible=True)
    invalid(
        ReplayCaseManifest,
        replay,
        chapter_range=(1, 50),
        gold_changes=(),
        gate_eligible=True,
    )
    invalid(BenchmarkBundle, bundle, replay_manifests=(replay, replay))
    checkpoint = replay.state_checkpoints[0]
    duplicate_record = checkpoint.expected_records[0]
    invalid(
        ReplayStateCheckpoint,
        checkpoint,
        expected_records=(duplicate_record, duplicate_record),
    )
    invalid(
        ReplayCaseManifest,
        replay,
        state_checkpoints=(checkpoint, checkpoint),
    )
    invalid(
        ReplayCaseManifest,
        replay,
        state_checkpoints=(checkpoint.model_copy(update={"chapter_index": 99}),),
    )
    invalid(
        ReplayCaseManifest,
        replay,
        chapter_range=(1, 50),
        gate_eligible=True,
    )


def test_world_need_and_horizon_guards() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    entity = world.entities[0]

    invalid(WorldRootDocument, world, entities=(entity, entity))
    duplicate_record = world.events[0].model_copy(update={"event_id": world.states[0].state_id})
    invalid(WorldRootDocument, world, events=(duplicate_record,))
    unknown = Entity(
        entity_id=StableId("entity.unknown"),
        entity_type="character",
        internal_label="unknown",
    )
    invalid(
        WorldRootDocument,
        world,
        states=(world.states[0].model_copy(update={"subject_id": unknown.entity_id}),),
    )

    valid_need = need(
        Stage1QueryIntent.EXACT_QUOTE,
        "quote",
        (CandidatePool.GROUNDED,),
    )
    invalid(Stage1MemoryNeed, valid_need, chapter_target=None, horizon_target=None)
    invalid(Stage1MemoryNeed, valid_need, chapter_target=None, horizon_target=(3, 2))
    with pytest.raises(ValidationError, match="horizon end"):
        HorizonNeedSet(horizon_start=3, horizon_end=2)
    assert HorizonNeedSet(horizon_start=2, horizon_end=3).horizon_end == 3
    duplicate_rule = StateTransitionRule(
        predicate="injury",
        allowed=(StateTransitionEdge(from_value="hurt", to_value="healed"),),
    )
    policy = StateTransitionPolicy(
        policy_id=StableId("transition.test"),
        schema_version=SchemaVersion("0.1.0"),
        rules=(duplicate_rule,),
    )
    invalid(StateTransitionPolicy, policy, rules=(duplicate_rule, duplicate_rule))
