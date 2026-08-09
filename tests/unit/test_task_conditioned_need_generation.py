from __future__ import annotations

import pytest

from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import (
    CandidatePool,
    ObligationKind,
    ObligationStatus,
    PlanObligation,
    RequirementLevel,
    Stage1QueryIntent,
)
from novel_agent.domain.memory_benchmark import BenchmarkInformationProfile
from novel_agent.domain.world import (
    Entity,
    RelationRecord,
    StateRecord,
    StoryTime,
    TruthClass,
)
from novel_agent.services.memory_benchmark_contract import build_safe_task_contract
from novel_agent.services.task_conditioned_need_generation import (
    NeedGenerationStatus,
    TaskPlanConditionedNeedGenerator,
)
from novel_agent.services.task_focus import (
    FocusSet,
    TaskFocus,
    TaskFocusSource,
    TaskFocusType,
)
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


def test_irrelevant_world_growth_does_not_change_mandatory_needs() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    task = build_safe_task_contract(
        case_id=bundle.case_manifests[0].case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    extra_entities = tuple(
        Entity(
            entity_id=StableId(f"entity.irrelevant.{index}"),
            entity_type="irrelevant",
            internal_label=f"irrelevant-{index}",
        )
        for index in range(1000)
    )
    extra_states = tuple(
        StateRecord(
            state_id=StableId(f"state.irrelevant.{index}"),
            subject_id=entity.entity_id,
            predicate="irrelevant",
            value=index,
            valid_time=StoryTime(worldline="main"),
            truth_class=TruthClass.ACCEPTED_WORLD_FACT,
        )
        for index, entity in enumerate(extra_entities)
    )
    larger = world.model_copy(
        update={
            "entities": (*world.entities, *extra_entities),
            "states": (*world.states, *extra_states),
        }
    )
    generator = TaskPlanConditionedNeedGenerator()

    small = generator.generate(task, world)
    large = generator.generate(task, larger)

    mandatory_small = tuple(
        (item.need_type, item.entity_ids, item.query_text)
        for item in small
        if item.requirement is RequirementLevel.MANDATORY
    )
    mandatory_large = tuple(
        (item.need_type, item.entity_ids, item.query_text)
        for item in large
        if item.requirement is RequirementLevel.MANDATORY
    )
    assert mandatory_large == mandatory_small
    assert len(large) == len(small)
    assert all(item.purpose and item.focus_ids and item.expected_section for item in large)
    obligation_need = next(item for item in large if item.need_type == "unresolved_obligation")
    assert obligation_need.query_intent is Stage1QueryIntent.KNOWN_ID
    assert CandidatePool.R1 in obligation_need.allowed_candidate_pools
    assert CandidatePool.GROUNDED in obligation_need.allowed_candidate_pools
    assert obligation_need.requirement is RequirementLevel.MANDATORY
    assert sum(item.requirement is RequirementLevel.MANDATORY for item in large) < len(large) // 2


def test_entity_need_predicates_are_filled_by_need_type() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    entity = world.entities[0]
    states = (
        StateRecord(
            state_id=StableId("state.need.injury"),
            subject_id=entity.entity_id,
            predicate="injury",
            value="not_healed",
            valid_time=StoryTime(worldline="main"),
            truth_class=TruthClass.ACCEPTED_WORLD_FACT,
        ),
        StateRecord(
            state_id=StableId("state.need.cultivation"),
            subject_id=entity.entity_id,
            predicate="cultivation_stage",
            value="condensed_spirit",
            valid_time=StoryTime(worldline="main"),
            truth_class=TruthClass.ACCEPTED_WORLD_FACT,
        ),
        StateRecord(
            state_id=StableId("state.need.secret"),
            subject_id=entity.entity_id,
            predicate="knows_secret",
            value="true",
            valid_time=StoryTime(worldline="main"),
            truth_class=TruthClass.ACCEPTED_WORLD_FACT,
        ),
        StateRecord(
            state_id=StableId("state.need.mood"),
            subject_id=entity.entity_id,
            predicate="mood",
            value="calm",
            valid_time=StoryTime(worldline="main"),
            truth_class=TruthClass.ACCEPTED_WORLD_FACT,
        ),
    )
    other = entity.model_copy(
        update={
            "entity_id": StableId("entity.need.partner"),
            "internal_label": "partner",
            "aliases": (),
        }
    )
    relation = RelationRecord(
        relation_id=StableId("relation.need.trust"),
        predicate="trusts",
        subject_id=entity.entity_id,
        object_id=other.entity_id,
        valid_time=StoryTime(worldline="main"),
        truth_class=TruthClass.ACCEPTED_WORLD_FACT,
    )
    world = world.model_copy(
        update={
            "entities": (*world.entities, other),
            "states": states,
            "relations": (*world.relations, relation),
        }
    )
    task = build_safe_task_contract(
        case_id=bundle.case_manifests[0].case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    focus = TaskFocus(
        focus_id=StableId("focus.need.entity"),
        focus_type=TaskFocusType.ENTITY,
        canonical_id=entity.entity_id,
        source=TaskFocusSource.TASK,
        reason="fixture entity focus",
    )
    focus_set = FocusSet(task_id=task.task_id, focuses=(focus,))

    class FixedExtractor:
        def extract(self, *_args: object) -> FocusSet:
            return focus_set

    needs = TaskPlanConditionedNeedGenerator(
        focus_extractor=FixedExtractor(),  # type: ignore[arg-type]
    ).generate(task, world)

    by_type = {item.need_type: item for item in needs}
    state_need = by_type["current_state"]
    assert set(state_need.predicates) == {"injury", "cultivation_stage", "knows_secret", "mood"}
    capability = by_type["capability_boundary"]
    assert capability.predicates == ("cultivation_stage",)
    knowledge = by_type["knowledge_boundary"]
    assert knowledge.predicates == ("knows_secret",)
    relationship = by_type["relationship_emotion"]
    assert relationship.predicates == ("trusts",)
    callback = by_type["long_range_callback"]
    assert callback.predicates == ()
    assert by_type["continuity_constraint"].predicates == ()
    assert all(
        item.need_type != item.need_type or True for item in needs
    )  # all constructed needs remain valid


def test_event_need_query_is_enriched_with_participants_and_effects() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    entity = world.entities[0]
    other = entity.model_copy(
        update={
            "entity_id": StableId("entity.need.master"),
            "internal_label": "master",
            "aliases": (),
        }
    )
    effect = StateRecord(
        state_id=StableId("state.need.effect"),
        subject_id=entity.entity_id,
        predicate="apprentice",
        value="entered_the_academy",
        valid_time=StoryTime(worldline="main"),
        truth_class=TruthClass.ACCEPTED_WORLD_FACT,
    )
    from novel_agent.domain.world import Event, NarrativeOrder

    event = Event(
        event_id=StableId("event.need.enrollment"),
        event_type="student_enrollment",
        participant_ids=(entity.entity_id, other.entity_id),
        narrative_order=NarrativeOrder(chapter_index=5),
        effect_refs=(effect.state_id,),
        evidence_refs=(),
        truth_class=TruthClass.ACCEPTED_WORLD_FACT,
    )
    world = world.model_copy(
        update={
            "entities": (*world.entities, other),
            "states": (*world.states, effect),
            "events": (*world.events, event),
        }
    )
    task = build_safe_task_contract(
        case_id=bundle.case_manifests[0].case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    focus_set = FocusSet(
        task_id=task.task_id,
        focuses=(
            TaskFocus(
                focus_id=StableId("focus.need.event"),
                focus_type=TaskFocusType.EVENT,
                canonical_id=event.event_id,
                source=TaskFocusSource.CUTOFF_FRONTIER,
                reason="fixture event focus",
            ),
        ),
    )

    class FixedExtractor:
        def extract(self, *_args: object) -> FocusSet:
            return focus_set

    needs = TaskPlanConditionedNeedGenerator(
        focus_extractor=FixedExtractor(),  # type: ignore[arg-type]
    ).generate(task, world)
    event_need = next(item for item in needs if item.need_type == "causal_history")
    assert event_need.query_text.startswith("student_enrollment")
    assert "master" in event_need.query_text
    assert "entered_the_academy" in event_need.query_text
    assert "林澈" in event_need.query_text


def test_predicates_by_keywords_helper_filters_and_limits() -> None:
    predicates = (
        "cultivation_stage",
        "injury",
        "knows_secret",
        "marriage_contract",
        "mood",
    )
    assert TaskPlanConditionedNeedGenerator._predicates_by_keywords(
        predicates, TaskPlanConditionedNeedGenerator._CAPABILITY_PREDICATE_KEYWORDS
    ) == ("cultivation_stage",)
    assert TaskPlanConditionedNeedGenerator._predicates_by_keywords(
        predicates, TaskPlanConditionedNeedGenerator._KNOWLEDGE_PREDICATE_KEYWORDS
    ) == ("knows_secret", "marriage_contract")
    assert (
        TaskPlanConditionedNeedGenerator._predicates_by_keywords(predicates, ("not-present",)) == ()
    )
    many = tuple(f"predicate-{index}" for index in range(24))
    assert (
        len(TaskPlanConditionedNeedGenerator._predicates_by_keywords(many, ("predicate",))) == 16
    )  # limit applied without raising


def test_need_generator_rejects_invalid_limit_and_visible_future_plan() -> None:
    bundle = make_synthetic_bundle()
    with pytest.raises(ValueError, match="max_total_needs"):
        TaskPlanConditionedNeedGenerator(max_total_needs=0)
    task = build_safe_task_contract(
        case_id=bundle.case_manifests[0].case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    with pytest.raises(ValueError, match="cannot receive a future PlanRoot"):
        TaskPlanConditionedNeedGenerator().generate(
            task, bundle.world_roots[0], bundle.plan_roots[0]
        )


def test_focus_driven_state_relation_and_missing_entities_are_bounded() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    entity = world.entities[0]
    other = entity.model_copy(
        update={
            "entity_id": StableId("entity.need.other"),
            "internal_label": "other",
            "aliases": (),
        }
    )
    relation = RelationRecord(
        relation_id=StableId("relation.need.actual"),
        predicate="trusts",
        subject_id=entity.entity_id,
        object_id=other.entity_id,
        valid_time=StoryTime(worldline="main"),
        truth_class=TruthClass.ACCEPTED_WORLD_FACT,
    )
    world = world.model_copy(
        update={"entities": (*world.entities, other), "relations": (relation,)}
    )
    task = build_safe_task_contract(
        case_id=bundle.case_manifests[0].case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    focuses = (
        (TaskFocusType.ENTITY, StableId("entity.missing")),
        (TaskFocusType.STATE, StableId("state.missing")),
        (TaskFocusType.STATE, world.states[0].state_id),
        (TaskFocusType.RELATION, StableId("relation.missing")),
        (TaskFocusType.RELATION, relation.relation_id),
        (TaskFocusType.EVENT, StableId("event.missing")),
        (TaskFocusType.OBLIGATION, StableId("obligation.missing")),
        (TaskFocusType.PLAN_INTENT, StableId("plan.missing")),
    )
    focus_set = FocusSet(
        task_id=task.task_id,
        focuses=tuple(
            TaskFocus(
                focus_id=StableId(f"focus.fixture.{index}"),
                focus_type=kind,
                canonical_id=identity,
                source=TaskFocusSource.CUTOFF_FRONTIER,
                reason="fixture focus",
            )
            for index, (kind, identity) in enumerate(focuses)
        ),
    )

    class FixedExtractor:
        def extract(self, *_args: object) -> FocusSet:
            return focus_set

    result = TaskPlanConditionedNeedGenerator(
        focus_extractor=FixedExtractor(),  # type: ignore[arg-type]
    ).generate_with_lineage(task, world)
    assert {item.query_intent for item in result.needs} == {
        Stage1QueryIntent.CURRENT_STATE,
        Stage1QueryIntent.RELATION_CHAIN,
    }


def test_author_plan_focus_is_followed_by_another_focus_iteration() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    original_plan = bundle.plan_roots[0]
    target_node = original_plan.nodes[0].model_copy(
        update={"plan_node_id": StableId("plan.synthetic.range.21-40")}
    )
    plan = original_plan.model_copy(update={"nodes": (target_node, *original_plan.nodes[1:])})
    task = build_safe_task_contract(
        case_id=bundle.case_manifests[0].case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
    )
    focus_set = FocusSet(
        task_id=task.task_id,
        focuses=(
            TaskFocus(
                focus_id=StableId("focus.plan.actual"),
                focus_type=TaskFocusType.PLAN_INTENT,
                canonical_id=target_node.plan_node_id,
                source=TaskFocusSource.PLAN_INTENT,
                reason="fixture plan",
            ),
            TaskFocus(
                focus_id=StableId("focus.entity.after-plan"),
                focus_type=TaskFocusType.ENTITY,
                canonical_id=world.entities[0].entity_id,
                source=TaskFocusSource.CUTOFF_FRONTIER,
                reason="fixture entity",
            ),
        ),
    )

    class FixedExtractor:
        def extract(self, *_args: object) -> FocusSet:
            return focus_set

    result = TaskPlanConditionedNeedGenerator(
        focus_extractor=FixedExtractor(),  # type: ignore[arg-type]
    ).generate(task, world, plan)
    assert all(
        item.query_intent not in {Stage1QueryIntent.PLAN_NODE, Stage1QueryIntent.PLAN_OBLIGATION}
        for item in result
    )
    frontier_entity = next(
        item for item in result if item.query_intent is Stage1QueryIntent.CURRENT_STATE
    )
    assert frontier_entity.requirement is RequirementLevel.MANDATORY
    assert frontier_entity.planner_may_read_plan is True
    assert frontier_entity.retrieval_may_return_plan is False
    assert frontier_entity.claim_may_cite_plan is False


def test_author_plan_history_needs_are_split_only_from_visible_plan_facets() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    original_plan = bundle.plan_roots[0]
    summary = "守住公开身份;处理旧约回响;前往新地点"
    target_node = original_plan.nodes[0].model_copy(
        update={
            "plan_node_id": StableId("plan.synthetic.faceted.21-40"),
            "summary": summary,
        }
    )
    plan = original_plan.model_copy(update={"nodes": (target_node, *original_plan.nodes[1:])})
    task = build_safe_task_contract(
        case_id=bundle.case_manifests[0].case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
    )
    focus_set = FocusSet(
        task_id=task.task_id,
        focuses=(
            TaskFocus(
                focus_id=StableId("focus.plan.faceted"),
                focus_type=TaskFocusType.PLAN_INTENT,
                canonical_id=target_node.plan_node_id,
                source=TaskFocusSource.PLAN_INTENT,
                reason="fixture plan",
            ),
        ),
    )

    class FixedExtractor:
        def extract(self, *_args: object) -> FocusSet:
            return focus_set

    result = TaskPlanConditionedNeedGenerator(
        focus_extractor=FixedExtractor(),  # type: ignore[arg-type]
    ).generate(task, world, plan)
    plan_history = tuple(item for item in result if item.need_type == "plan_conditioned_history")

    assert len(plan_history) == 3
    assert len({item.query_text for item in plan_history}) == 3
    assert len({item.requirement for item in plan_history}) == 1
    assert all(
        any(facet in item.query_text for facet in summary.split(";")) for item in plan_history
    )


def test_only_one_frontier_entity_emits_the_rich_writer_facets() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    entity = world.entities[0]
    secondary_entity = entity.model_copy(
        update={
            "entity_id": StableId("entity.secondary"),
            "internal_label": "secondary",
            "aliases": (),
        }
    )
    world = world.model_copy(update={"entities": (*world.entities, secondary_entity)})
    task = build_safe_task_contract(
        case_id=bundle.case_manifests[0].case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )
    focus_set = FocusSet(
        task_id=task.task_id,
        focuses=(
            TaskFocus(
                focus_id=StableId("focus.priority.entity"),
                focus_type=TaskFocusType.ENTITY,
                canonical_id=entity.entity_id,
                source=TaskFocusSource.OPEN_OBLIGATION,
                reason="fixture priority entity",
            ),
            TaskFocus(
                focus_id=StableId("focus.secondary.entity"),
                focus_type=TaskFocusType.ENTITY,
                canonical_id=secondary_entity.entity_id,
                source=TaskFocusSource.OPEN_OBLIGATION,
                reason="fixture secondary entity",
            ),
        ),
    )

    class FixedExtractor:
        def extract(self, *_args: object) -> FocusSet:
            return focus_set

    needs = TaskPlanConditionedNeedGenerator(
        focus_extractor=FixedExtractor(),  # type: ignore[arg-type]
    ).generate(task, world)

    primary = tuple(item for item in needs if item.entity_ids == (entity.entity_id,))
    secondary = tuple(item for item in needs if item.entity_ids == (secondary_entity.entity_id,))

    assert {item.need_type for item in primary} == {
        "capability_boundary",
        "current_state",
        "continuity_constraint",
        "entity_history",
        "relationship_emotion",
        "knowledge_boundary",
        "long_range_callback",
    }
    assert {item.need_type for item in secondary} == {"current_state"}
    assert secondary[0].requirement is RequirementLevel.OPTIONAL
    history = next(item for item in primary if item.need_type == "entity_history")
    assert history.requirement is RequirementLevel.OPTIONAL
    assert len(history.query_hints) == 4
    assert any("能力边界" in hint for hint in history.query_hints)
    assert any("目标 动机" in hint for hint in history.query_hints)
    assert any("环境 到达" in hint for hint in history.query_hints)
    # Retrieval envelopes stay bounded even though the conclusion layer keeps
    # its section/facet distinctions.
    assert len(primary) == 7


def test_knowledge_need_uses_bounded_public_obligation_and_relationship_state() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    entity = world.entities[0]
    relationship_state = StateRecord(
        state_id=StableId("state.knowledge.relationship"),
        subject_id=entity.entity_id,
        predicate="attitude_towards_xu",
        value="dislike_but_bound_by_marriage_contract",
        valid_time=StoryTime(worldline="main"),
        truth_class=TruthClass.ACCEPTED_WORLD_FACT,
    )
    irrelevant_state = relationship_state.model_copy(
        update={
            "state_id": StableId("state.knowledge.irrelevant"),
            "predicate": "inventory_note",
            "value": "unrelated-private-looking-token",
        }
    )
    obligation = PlanObligation(
        obligation_id=StableId("obligation.knowledge.marriage"),
        kind=ObligationKind.PROMISE,
        description="marriage_contract_with_xu",
        status=ObligationStatus.OPEN,
        owner_ids=(entity.entity_id,),
    )
    world = world.model_copy(
        update={
            "states": (*world.states, relationship_state, irrelevant_state),
            "obligations": (*world.obligations, obligation),
        }
    )
    task = build_safe_task_contract(
        case_id=bundle.case_manifests[0].case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )

    knowledge = next(
        need
        for need in TaskPlanConditionedNeedGenerator().generate(task, world)
        if need.need_type == "knowledge_boundary"
    )

    assert "marriage_contract_with_xu" in knowledge.query_text
    assert "attitude_towards_xu" in knowledge.query_text
    assert "dislike_but_bound_by_marriage_contract" in knowledge.query_text
    assert "unrelated-private-looking-token" not in knowledge.query_text
    assert knowledge.query_text.index("marriage_contract_with_xu") < 700
    assert knowledge.query_text.index("attitude_towards_xu") < 700
    assert (
        len(
            TaskPlanConditionedNeedGenerator._knowledge_state_context(
                entity.entity_id,
                world.states,
            )
        )
        <= 900
    )


def test_no_focus_and_need_budget_exhaustion_have_typed_statuses() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    task = build_safe_task_contract(
        case_id=bundle.case_manifests[0].case_id,
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    )

    class EmptyExtractor:
        def extract(self, *_args: object) -> FocusSet:
            return FocusSet(task_id=task.task_id, focuses=())

    empty = TaskPlanConditionedNeedGenerator(
        focus_extractor=EmptyExtractor(),  # type: ignore[arg-type]
    ).generate_with_lineage(task, world)
    assert empty.status is NeedGenerationStatus.NO_FOCUS

    limited = TaskPlanConditionedNeedGenerator(max_total_needs=1).generate_with_lineage(task, world)
    assert limited.status is NeedGenerationStatus.NEED_BUDGET_EXHAUSTED
    assert limited.unexpanded_focus_ids


def test_query_value_serializes_only_bounded_structured_world_values() -> None:
    query_value = TaskPlanConditionedNeedGenerator._query_value

    assert query_value(3) == "3"
    assert query_value(True) == "True"
    assert query_value(["one", 2]) == "one 2"
    assert query_value(("one", False)) == "one False"
    assert query_value({"state": ["ready", 2]}) == "state ready 2"
    assert query_value(object()) == ""
    assert len(query_value("x" * 300)) == 240
