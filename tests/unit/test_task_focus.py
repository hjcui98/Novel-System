from __future__ import annotations

import pytest

from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import ObligationStatus
from novel_agent.domain.memory_benchmark import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
)
from novel_agent.domain.world import Event, NarrativeOrder, RelationRecord, StoryTime, TruthClass
from novel_agent.services.memory_benchmark_contract import build_safe_task_contract
from novel_agent.services.task_focus import TaskFocusExtractor, TaskFocusSource, TaskFocusType
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


def _task(
    profile: BenchmarkInformationProfile = BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
) -> BenchmarkTaskContract:
    return build_safe_task_contract(
        case_id=StableId("ZTJ-P001"),
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=profile,
    )


def test_focus_limits_reject_invalid_values() -> None:
    with pytest.raises(ValueError, match="focus limits"):
        TaskFocusExtractor(max_focuses=0)
    with pytest.raises(ValueError, match="focus limits"):
        TaskFocusExtractor(max_relation_expansions=-1)
    with pytest.raises(ValueError, match="focus limits"):
        TaskFocusExtractor(recent_event_limit=0)


def test_task_frontier_filters_closed_but_keeps_progressed_overdue_obligations() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    entity = world.entities[0]
    task = _task().model_copy(update={"task_text": f"请恢复{entity.internal_label}的当前状态"})
    closed = world.obligations[0].model_copy(update={"status": ObligationStatus.RESOLVED})
    overdue = world.obligations[0].model_copy(
        update={"obligation_id": StableId("obligation.overdue"), "due_chapter": 19}
    )
    focused = TaskFocusExtractor().extract(
        task,
        world.model_copy(update={"obligations": (closed, overdue)}),
    )

    assert any(
        item.canonical_id == entity.entity_id and item.source is TaskFocusSource.TASK
        for item in focused.focuses
    )
    assert any(
        item.focus_type is TaskFocusType.OBLIGATION and item.canonical_id == overdue.obligation_id
        for item in focused.focuses
    )
    assert not any(item.canonical_id == closed.obligation_id for item in focused.focuses)


def test_task_entity_aliases_are_deduplicated_by_label_and_prefer_bootstrap() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    entity = world.entities[0]
    duplicate = entity.model_copy(
        update={
            "entity_id": StableId("entity.synthetic.duplicate"),
            "aliases": (*entity.aliases, entity.internal_label),
        }
    )
    bootstrap = entity.model_copy(
        update={
            "entity_id": StableId("entity.bootstrap.preferred"),
            "aliases": (*entity.aliases, entity.internal_label),
        }
    )
    task = _task().model_copy(update={"task_text": f"恢复{entity.internal_label}的历史上下文"})

    result = TaskFocusExtractor(max_relation_expansions=0).extract(
        task,
        world.model_copy(update={"entities": (duplicate, bootstrap, *world.entities)}),
    )
    task_entities = tuple(
        item
        for item in result.focuses
        if item.focus_type is TaskFocusType.ENTITY and item.source is TaskFocusSource.TASK
    )

    assert tuple(item.canonical_id for item in task_entities) == (bootstrap.entity_id,)


def test_author_plan_range_and_one_hop_relation_are_bounded() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    entity = world.entities[0]
    other = entity.model_copy(
        update={
            "entity_id": StableId("entity.synthetic.other"),
            "internal_label": "顾行",
            "aliases": ("顾行",),
        }
    )
    relation = RelationRecord(
        relation_id=StableId("relation.synthetic.knows"),
        predicate="knows",
        subject_id=entity.entity_id,
        object_id=other.entity_id,
        valid_time=StoryTime(worldline="main"),
        truth_class=TruthClass.ACCEPTED_WORLD_FACT,
    )
    unrelated = relation.model_copy(
        update={
            "relation_id": StableId("relation.synthetic.unrelated"),
            "subject_id": StableId("entity.synthetic.ghost-a"),
            "object_id": StableId("entity.synthetic.ghost-b"),
        }
    )
    after_limit = relation.model_copy(
        update={"relation_id": StableId("relation.synthetic.after-limit")}
    )
    world = world.model_copy(
        update={
            "entities": (*world.entities, other),
            "relations": (unrelated, relation, after_limit),
        }
    )
    plan = bundle.plan_roots[0].model_copy(
        update={
            "nodes": (
                bundle.plan_roots[0]
                .nodes[0]
                .model_copy(
                    update={
                        "plan_node_id": StableId("plan.bootstrap.range.21-30"),
                        "title": "林澈与顾行",
                        "summary": "两人共同推进北塔线",
                    }
                ),
                bundle.plan_roots[0]
                .nodes[0]
                .model_copy(
                    update={
                        "plan_node_id": StableId("plan.bootstrap.range.80-90"),
                        "title": "远期",
                        "summary": "远期意图",
                    }
                ),
            )
        }
    )
    result = TaskFocusExtractor(max_relation_expansions=1).extract(
        _task(BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED),
        world,
        plan,
    )

    ids = {item.canonical_id for item in result.focuses}
    assert StableId("plan.bootstrap.range.21-30") in ids
    assert relation.relation_id in ids
    assert other.entity_id in ids
    assert any(
        item.canonical_id == other.entity_id and item.source is TaskFocusSource.PLAN_INTENT
        for item in result.focuses
    )
    assert TaskFocusExtractor._plan_node_intersects_target(
        StableId("plan.bootstrap.range.21-30"), _task()
    )
    assert not TaskFocusExtractor._plan_node_intersects_target(
        StableId("plan.without-range"), _task()
    )


def test_author_plan_entity_expansion_ignores_non_target_nodes() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    entity = world.entities[0]
    remote = entity.model_copy(
        update={
            "entity_id": StableId("entity.plan.remote"),
            "internal_label": "远期人物",
            "aliases": ("远期人物",),
        }
    )
    plan = bundle.plan_roots[0].model_copy(
        update={
            "nodes": (
                bundle.plan_roots[0]
                .nodes[0]
                .model_copy(
                    update={
                        "plan_node_id": StableId("plan.range.21-30"),
                        "title": "近期",
                        "summary": "推进主角任务",
                    }
                ),
                bundle.plan_roots[0]
                .nodes[0]
                .model_copy(
                    update={
                        "plan_node_id": StableId("plan.range.80-90"),
                        "title": "远期人物登场",
                        "summary": "远期人物推进支线",
                    }
                ),
            ),
            "chapter_goals": (),
        }
    )

    result = TaskFocusExtractor(max_relation_expansions=0).extract(
        _task(BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED),
        world.model_copy(update={"entities": (*world.entities, remote)}),
        plan,
    )

    assert not any(
        item.canonical_id == remote.entity_id and item.source is TaskFocusSource.PLAN_INTENT
        for item in result.focuses
    )


def test_focus_truncation_and_zero_relation_expansion_are_explicit() -> None:
    bundle = make_synthetic_bundle()
    result = TaskFocusExtractor(max_focuses=1, max_relation_expansions=0).extract(
        _task(), bundle.world_roots[0]
    )
    assert len(result.focuses) == 1
    assert result.truncated_focus_ids


def test_recent_event_frontier_is_bounded_and_plan_wins_truncation() -> None:
    bundle = make_synthetic_bundle()
    entity = bundle.world_roots[0].entities[0]
    events = tuple(
        reversed(
            tuple(
                Event(
                    event_id=StableId(f"event.frontier.{index}"),
                    event_type=f"frontier-{index}",
                    participant_ids=(entity.entity_id,),
                    narrative_order=NarrativeOrder(chapter_index=index),
                    truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                )
                for index in range(13)
            )
        )
    )
    world = bundle.world_roots[0].model_copy(update={"events": events, "obligations": ()})
    visible = TaskFocusExtractor(
        recent_event_limit=12,
        max_relation_expansions=0,
    ).extract(_task(), world)
    visible_ids = {item.canonical_id for item in visible.focuses}
    assert StableId("event.frontier.0") not in visible_ids
    assert StableId("event.frontier.1") in visible_ids
    assert StableId("event.frontier.12") in visible_ids

    author = TaskFocusExtractor(
        max_focuses=1,
        recent_event_limit=12,
        max_relation_expansions=0,
    ).extract(
        _task(BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED),
        world,
        bundle.plan_roots[0],
    )
    assert author.focuses[0].source is TaskFocusSource.PLAN_INTENT


def test_plan_entity_focus_replaces_lower_priority_cutoff_focus() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    entity = world.entities[0]
    event = world.events[0].model_copy(update={"participant_ids": (entity.entity_id,)})
    plan_node = (
        bundle.plan_roots[0]
        .nodes[0]
        .model_copy(
            update={
                "plan_node_id": StableId("plan.range.21-30"),
                "summary": f"{entity.internal_label} 推进近期任务",
            }
        )
    )
    plan = bundle.plan_roots[0].model_copy(update={"nodes": (plan_node,)})

    result = TaskFocusExtractor(max_relation_expansions=0).extract(
        _task(BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED),
        world.model_copy(update={"events": (event,), "obligations": ()}),
        plan,
    )

    focus = next(
        item
        for item in result.focuses
        if item.focus_type is TaskFocusType.ENTITY and item.canonical_id == entity.entity_id
    )
    assert focus.source is TaskFocusSource.PLAN_INTENT


def test_event_chapter_falls_back_over_missing_and_invalid_evidence_chapters() -> None:
    event = make_synthetic_bundle().world_roots[0].events[0]
    reference = event.evidence_refs[0]
    fallback = event.model_copy(
        update={
            "narrative_order": None,
            "evidence_refs": (
                reference.model_copy(update={"chapter_id": None}),
                reference.model_copy(update={"chapter_id": StableId("chapter.without-number")}),
                reference.model_copy(update={"chapter_id": StableId("chapter.case.17")}),
            ),
        }
    )

    assert TaskFocusExtractor._event_chapter(fallback) == 17
