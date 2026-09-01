from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import (
    CandidatePool,
    NeedFacetKind,
    RelationFacetBinding,
    Stage1MemoryNeed,
    Stage1QueryIntent,
    WorldRootDocument,
)
from novel_agent.domain.planning import (
    GoalProposal,
    PlanningInquiry,
    PlanningProblemIdentitySeed,
    PlanningProvenance,
    PlanningQuestion,
    PlanningQuestionKind,
    PlanningReference,
    PlanReview,
    ReviewDecision,
    ReviewTargetKind,
)
from novel_agent.domain.planning_memory import PlannerNeedGenerationResult
from novel_agent.domain.stage2 import AgentMode, AgentType
from novel_agent.domain.world import Entity, RelationRecord, StateRecord, StoryTime, TruthClass
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.benchmark_importer import world_root_content_id
from novel_agent.services.planning_context_loop import (
    PlanningContextLoopService,
    _planner_memory_questions,
)
from novel_agent.services.planning_inquiry_need_generation import (
    PlanningInquiryConditionedNeedGenerator,
    PlanningInquiryNeedError,
)
from novel_agent.services.retrieval import ROUTES
from novel_agent.tools.retrieval import CHANNEL_BY_TOOL, POOL_BY_CHANNEL
from tests.unit.test_stage4_planning_contracts import _receipt

VERSION = SchemaVersion("1.0.0")
COMMIT = CommitId("sha256:" + "2" * 64)
CHEN = StableId("entity.chen-changsheng")
XU = StableId("entity.xu-yourong")
LUOLUO = StableId("entity.luoluo")
DRAGON = StableId("entity.black-dragon")
COURT = StableId("entity.zhou-court")
SWORD = StableId("entity.sword")
SHORT_SWORD = StableId("entity.short-sword")
ACADEMY = StableId("entity.xing-academy")
GUOJIAO = StableId("entity.guojiao-college")
TIANHAI_HOUSE = StableId("entity.graph.tianhai-house")
TIANHAI_YAER = StableId("entity.tianhai-yaer")
TIANHAI_SHENGXUE = StableId("entity.tianhai-shengxue")
GARDEN = StableId("entity.graph.ruined-garden")


def _world() -> WorldRootDocument:
    entities = (
        Entity(
            entity_id=CHEN,
            entity_type="character",
            internal_label="陈长生",
            aliases=("Chen Changsheng",),
        ),
        Entity(
            entity_id=XU,
            entity_type="character",
            internal_label="徐有容",
            aliases=("Xu Yourong",),
        ),
        Entity(
            entity_id=LUOLUO,
            entity_type="character",
            internal_label="落落",
            aliases=("Luoluo",),
        ),
        Entity(
            entity_id=DRAGON,
            entity_type="creature",
            internal_label="黑龙",
            aliases=("Black Dragon",),
        ),
        Entity(
            entity_id=COURT,
            entity_type="org",
            internal_label="朝廷",
            aliases=("Zhou court",),
        ),
        Entity(
            entity_id=SWORD,
            entity_type="artifact",
            internal_label="剑",
            aliases=("sword",),
        ),
        Entity(
            entity_id=SHORT_SWORD,
            entity_type="artifact",
            internal_label="短剑",
            aliases=("short sword",),
        ),
        Entity(
            entity_id=ACADEMY,
            entity_type="organization",
            internal_label="摘星学院",
            aliases=("Xing Academy",),
        ),
        Entity(
            entity_id=GUOJIAO,
            entity_type="organization",
            internal_label="国教学院",
        ),
        Entity(
            entity_id=TIANHAI_HOUSE,
            entity_type="organization",
            internal_label="天海家",
        ),
        Entity(
            entity_id=TIANHAI_YAER,
            entity_type="character",
            internal_label="天海牙儿",
        ),
        Entity(
            entity_id=TIANHAI_SHENGXUE,
            entity_type="character",
            internal_label="天海胜雪",
        ),
        Entity(
            entity_id=GARDEN,
            entity_type="location",
            internal_label="废园",
        ),
    )
    relations = (
        RelationRecord(
            relation_id=StableId("relation.betrothal"),
            predicate="婚约",
            subject_id=CHEN,
            object_id=XU,
            valid_time=StoryTime(worldline="main", start_ordinal=1),
            truth_class=TruthClass.ACCEPTED_WORLD_FACT,
        ),
        RelationRecord(
            relation_id=StableId("relation.luoluo"),
            predicate="师徒",
            subject_id=CHEN,
            object_id=LUOLUO,
            valid_time=StoryTime(worldline="main", start_ordinal=1),
            truth_class=TruthClass.ACCEPTED_WORLD_FACT,
        ),
    )
    provisional = WorldRootDocument(
        root_hash=ArtifactId("sha256:" + "0" * 64),
        schema_version=VERSION,
        source_commit=COMMIT,
        entities=entities,
        relations=relations,
    )
    return provisional.model_copy(update={"root_hash": world_root_content_id(provisional)})


def _put(repo: ArtifactRepository, payload: str) -> ArtifactRef:
    return repo.put(payload.encode("utf-8"), "application/json", VERSION)


def _compile(
    tmp_path: Path,
    question: str,
    *,
    world: WorldRootDocument | None = None,
    problem_identity_seed: PlanningProblemIdentitySeed | None = None,
    relation: tuple[str, str, str] | None = None,
) -> PlannerNeedGenerationResult:
    repo = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    source = _put(repo, "author")
    goal = GoalProposal(
        goal_id=StableId("goal.u4l2.r2"),
        summary="固定五问路由",
        rationale="typed compilation",
        provenance=PlanningReference(provenance=PlanningProvenance.PLANNER_PROPOSED),
        decision_criteria=("可回答",),
    )
    item = PlanningQuestion(
        question_id=StableId("question.u4l2.r2"),
        kind=PlanningQuestionKind.FACT,
        question=question,
        provenance=PlanningReference(provenance=PlanningProvenance.PLANNER_PROPOSED),
        goal_id=goal.goal_id,
        blocking=True,
        relation_subject=None if relation is None else relation[0],
        relation_predicate=None if relation is None else relation[1],
        relation_object=None if relation is None else relation[2],
    )
    inquiry = PlanningInquiry(
        inquiry_id=StableId("inquiry.u4l2.r2"),
        project_id=ProjectId("project.u4l2"),
        mode=AgentMode.CHAPTER_SET,
        planning_scope=("rolling",),
        horizon_start=21,
        horizon_end=23,
        author_intent_refs=(source,),
        goal_proposals=(goal,),
        questions=(item,),
        expected_output_shape="bounded PlanProposal",
    )
    inquiry_ref = repo.put(inquiry.model_dump_json().encode(), "application/json", VERSION)
    review = PlanReview(
        review_id=StableId("review.u4l2.r2"),
        target_kind=ReviewTargetKind.INQUIRY,
        target_artifact_ref=inquiry_ref,
        decision=ReviewDecision.ACCEPT,
        receipt=_receipt(AgentMode.CHAPTER_SET, AgentType.PLAN_REVIEWER),
    )
    review_ref = repo.put(review.model_dump_json().encode(), "application/json", VERSION)
    return PlanningInquiryConditionedNeedGenerator().generate(
        inquiry=inquiry,
        inquiry_ref=inquiry_ref,
        review=review,
        review_ref=review_ref,
        world=world or _world(),
        run_id=RunId("run.u4l2.r2"),
        task_id=TaskId("task.u4l2.r2"),
        problem_identity_seed=problem_identity_seed,
    )


def _route_receipt(need: Stage1MemoryNeed) -> dict[str, object]:
    tools = tuple(
        name
        for name, channel in CHANNEL_BY_TOOL.items()
        if POOL_BY_CHANNEL.get(channel) in need.allowed_candidate_pools
    )
    return {
        "entities": need.entity_ids,
        "facets": tuple(facet.facet_kind for facet in need.need_facets),
        "intent": need.query_intent,
        "pools": need.allowed_candidate_pools,
        "tools": tools,
    }


def test_stage4_has_no_keyword_kind_switch() -> None:
    source = inspect.getsource(PlanningContextLoopService)
    assert "marker in question.casefold()" not in source
    assert '("relation", "causal", "关系", "因果")' not in source


def test_five_questions_share_route_across_chinese_and_english(tmp_path: Path) -> None:
    pairs = (
        (
            "徐有容与陈长生的婚约关系是什么?",
            "What is the betrothal relationship between Xu Yourong and Chen Changsheng?",
        ),
        (
            "朝廷政治反弹及后果是什么?",
            "What political backlash and its consequences followed in the Zhou court?",
        ),
        (
            "陈长生与黑龙现在如何?",
            "What is the state of Chen Changsheng and the Black Dragon?",
        ),
        (
            "陈长生经脉当前状态如何?",
            "What is Chen Changsheng's current meridian state?",
        ),
        (
            "落落与陈长生的 relationship 是什么?",
            "What is Luoluo's relationship with Chen Changsheng?",
        ),
    )
    for chinese, english in pairs:
        left = _compile(tmp_path / chinese[:12], chinese)
        right = _compile(tmp_path / english[:12], english)
        assert left.needs, chinese
        assert right.needs, english
        left_receipts = tuple(_route_receipt(need) for need in left.needs)
        right_receipts = tuple(_route_receipt(need) for need in right.needs)
        assert left_receipts == right_receipts, (chinese, english)


def _need_with_facet(result: PlannerNeedGenerationResult, kind: NeedFacetKind) -> Stage1MemoryNeed:
    return next(
        need for need in result.needs if any(facet.facet_kind is kind for facet in need.need_facets)
    )


def test_relation_and_causal_keep_controller_graph_pool(tmp_path: Path) -> None:
    betrothal = _need_with_facet(
        _compile(tmp_path / "betrothal", "徐有容与陈长生的婚约关系是什么?"),
        NeedFacetKind.RELATION_STATE,
    )
    backlash = _compile(tmp_path / "backlash", "朝廷政治反弹及后果是什么?")
    meridians = _compile(tmp_path / "meridians", "陈长生经脉当前状态如何?").needs[0]
    assert betrothal.query_intent is Stage1QueryIntent.RELATION_CHAIN
    assert CandidatePool.GRAPH in betrothal.allowed_candidate_pools
    assert "memory.search_graph" in _route_receipt(betrothal)["tools"]
    causal = _need_with_facet(backlash, NeedFacetKind.CAUSAL_HISTORY)
    assert causal.query_intent is Stage1QueryIntent.CAUSAL_MULTI_HOP
    assert CandidatePool.GRAPH in causal.allowed_candidate_pools
    assert meridians.query_intent is Stage1QueryIntent.CURRENT_STATE
    assert CandidatePool.GRAPH not in meridians.allowed_candidate_pools
    relation_route = ROUTES[Stage1QueryIntent.RELATION_CHAIN]
    assert any(
        POOL_BY_CHANNEL[channel] is CandidatePool.GRAPH
        for channel in (*relation_route.channels, *relation_route.fallback_channels)
    )


def test_temporal_public_constraint_question_keeps_causal_history(tmp_path: Path) -> None:
    question = "陈长生 public_constraint_status 在第95章后是否因京都舆论产生新的公开约束?"
    need = _compile(tmp_path / "temporal-public-constraint", question).needs[0]

    assert tuple(facet.facet_kind for facet in need.need_facets) == (NeedFacetKind.CAUSAL_HISTORY,)


def test_explicit_scalar_predicates_override_multi_entity_relation_heuristic(
    tmp_path: Path,
) -> None:
    world = _world().model_copy(
        update={
            "states": (
                StateRecord(
                    state_id=StableId("state.chen.current-location"),
                    subject_id=CHEN,
                    predicate="current_location",
                    value="教枢处",
                    valid_time=StoryTime(worldline="main", start_ordinal=19),
                    truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                ),
                StateRecord(
                    state_id=StableId("state.chen.physical-state"),
                    subject_id=CHEN,
                    predicate="physical_state",
                    value="经脉稳定",
                    valid_time=StoryTime(worldline="main", start_ordinal=59),
                    truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                ),
            ),
        }
    )
    need = _compile(
        tmp_path / "explicit-state-predicates",
        "陈长生 与 天海家 在第95章结束时的 current_location 和 physical_state 是什么?",
        world=world,
    ).needs[0]

    assert need.query_intent is Stage1QueryIntent.CURRENT_STATE
    assert need.need_facets[0].facet_kind is NeedFacetKind.CURRENT_STATE
    assert CandidatePool.GRAPH not in need.allowed_candidate_pools
    assert "当前关系状态" not in need.semantic_question


def test_relation_backed_location_predicate_uses_graph_owner(tmp_path: Path) -> None:
    world = _world().model_copy(
        update={
            "states": (
                StateRecord(
                    state_id=StableId("state.chen.location"),
                    subject_id=CHEN,
                    predicate="location",
                    value="国教学院",
                    valid_time=StoryTime(worldline="main", start_ordinal=58),
                    truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                ),
            ),
        }
    )
    need = _compile(
        tmp_path / "relation-backed-location",
        "陈长生 当前 location 是什么?",
        world=world,
    ).needs[0]

    assert need.need_facets[0].facet_kind is NeedFacetKind.RELATION_STATE
    assert need.query_intent is Stage1QueryIntent.RELATION_CHAIN
    assert CandidatePool.GRAPH in need.allowed_candidate_pools


def test_inquiry_need_binds_explicit_relation_predicate_for_facet_closure(
    tmp_path: Path,
) -> None:
    """An exact relation anchor must be able to close the generated facet."""

    world = _world().model_copy(
        update={
            "relations": (
                *_world().relations,
                RelationRecord(
                    relation_id=StableId("relation.chen.located-at-guojiao"),
                    predicate="located_at",
                    subject_id=CHEN,
                    object_id=GUOJIAO,
                    valid_time=StoryTime(worldline="main", start_ordinal=20),
                    truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                ),
            )
        }
    )
    need = _compile(
        tmp_path / "explicit-relation-predicate",
        "陈长生 located_at 国教学院 在第95章末尾的具体状态是什么?",
        world=world,
    ).needs[0]

    assert need.need_facets[0].facet_kind is NeedFacetKind.RELATION_STATE
    assert need.predicates == ("located_at",)
    assert need.completion_spec is not None
    facet_id = need.need_facets[0].need_facet_id.root
    assert need.completion_spec.predicates_by_facet[facet_id] == ("located_at",)


def test_explicit_relation_fields_override_scalar_state_route(tmp_path: Path) -> None:
    world = _world().model_copy(
        update={
            "states": (
                StateRecord(
                    state_id=StableId("state.chen.location-for-relation-test"),
                    subject_id=CHEN,
                    predicate="location",
                    value="国教学院",
                    valid_time=StoryTime(worldline="main", start_ordinal=20),
                    truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                ),
            ),
        }
    )
    result = _compile(
        tmp_path / "explicit-relation-overrides-state",
        "陈长生 location mounts 黑龙 的关系是什么?",
        world=world,
        relation=("陈长生", "mounts", "黑龙"),
    )

    need = result.needs[0]
    assert need.need_facets[0].facet_kind is NeedFacetKind.RELATION_STATE
    assert need.query_intent is Stage1QueryIntent.RELATION_CHAIN
    assert need.completion_spec is not None
    facet_id = need.need_facets[0].need_facet_id.root
    assert need.completion_spec.relation_bindings_by_facet[facet_id]


def test_unresolved_explicit_relation_is_rejected_without_entity_widening(tmp_path: Path) -> None:
    result = _compile(
        tmp_path / "unresolved-explicit-relation",
        "陈长生 mounts 未知坐骑 的关系是什么?",
        relation=("陈长生", "mounts", "未知坐骑"),
    )

    assert result.needs == ()
    assert result.rejection_reasons["question.u4l2.r2"] == "explicit_relation_endpoint_unresolved"


def test_explicit_relation_need_preserves_ordered_endpoints_and_rejects_unrelated_anchor(
    tmp_path: Path,
) -> None:
    subject = CHEN
    object_ = DRAGON
    unrelated = XU
    world = _world().model_copy(
        update={
            "relations": (
                *_world().relations,
                RelationRecord(
                    relation_id=StableId("relation.chen.mounts-dragon"),
                    predicate="mounts",
                    subject_id=subject,
                    object_id=object_,
                    valid_time=StoryTime(worldline="main", start_ordinal=20),
                    truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                ),
                RelationRecord(
                    relation_id=StableId("relation.chen.mounts-xu"),
                    predicate="mounts",
                    subject_id=subject,
                    object_id=unrelated,
                    valid_time=StoryTime(worldline="main", start_ordinal=20),
                    truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                ),
            )
        }
    )
    result = _compile(
        tmp_path / "explicit-ordered-relation",
        "陈长生 mounts 黑龙 的关系是什么?",
        world=world,
        relation=("陈长生", "mounts", "黑龙"),
    )

    assert len(result.needs) == 1
    need = result.needs[0]
    assert need.entity_ids == (subject, object_)
    assert need.predicates == ("mounts",)
    assert need.completion_spec is not None
    facet_id = need.need_facets[0].need_facet_id.root
    assert need.completion_spec.relation_bindings_by_facet[facet_id] == (
        RelationFacetBinding(subject_id=subject, predicate="mounts", object_id=object_),
    )


def test_pre_registered_problem_identity_seed_controls_need_identity(tmp_path: Path) -> None:
    question = "徐有容与陈长生的婚约关系是什么?"
    seed = PlanningProblemIdentitySeed(
        need_id=StableId("need.u8c.preregistered.betrothed"),
        question_id=StableId("question.u4l2.r2"),
        need_query=question,
        semantic_question="预注册: 徐有容与陈长生的婚约关系是什么?",
        facet=NeedFacetKind.RELATION_STATE,
        source_commit=COMMIT,
        source_text_root=ArtifactId("sha256:" + "3" * 64),
        cutoff_chapter=20,
    )

    result = _compile(tmp_path / "seeded", question, problem_identity_seed=seed)

    assert len(result.needs) == 1
    need = result.needs[0]
    assert need.need_id == seed.need_id
    assert need.query_text == seed.need_query
    assert need.semantic_question == seed.semantic_question
    assert tuple(facet.facet_kind for facet in need.need_facets) == (seed.facet,)
    assert need.need_facets[0].need_facet_id.root == (
        f"facet.{seed.need_id.root}.{seed.facet.value}"
    )


def test_pre_registered_problem_identity_seed_rejects_route_drift(tmp_path: Path) -> None:
    question = "徐有容与陈长生的婚约关系是什么?"
    seed = PlanningProblemIdentitySeed(
        need_id=StableId("need.u8c.preregistered.current"),
        question_id=StableId("question.u4l2.r2"),
        need_query=question,
        semantic_question="预注册: 徐有容与陈长生的婚约关系是什么?",
        facet=NeedFacetKind.CURRENT_STATE,
        source_commit=COMMIT,
        source_text_root=ArtifactId("sha256:" + "4" * 64),
        cutoff_chapter=20,
    )

    with pytest.raises(
        PlanningInquiryNeedError,
        match="problem identity seed facet differs from deterministic Need routing",
    ):
        _compile(tmp_path / "route-drift", question, problem_identity_seed=seed)


def test_pre_registered_problem_identity_seed_binds_planner_follow_up_question() -> None:
    question = "徐有容与陈长生的婚约关系是什么?"
    goal = GoalProposal(
        goal_id=StableId("goal.u4l2.seed-follow-up"),
        summary="固定问题身份",
        rationale="stable identity",
        provenance=PlanningReference(provenance=PlanningProvenance.PLANNER_PROPOSED),
    )
    registered = PlanningQuestion(
        question_id=StableId("question.u4l2.seed-follow-up"),
        kind=PlanningQuestionKind.FACT,
        question=question,
        provenance=PlanningReference(provenance=PlanningProvenance.PLANNER_PROPOSED),
        goal_id=goal.goal_id,
    )
    inquiry = PlanningInquiry(
        inquiry_id=StableId("inquiry.u4l2.seed-follow-up"),
        project_id=ProjectId("project.u4l2"),
        mode=AgentMode.CHAPTER_SET,
        planning_scope=("rolling",),
        horizon_start=21,
        horizon_end=21,
        author_intent_refs=(
            ArtifactRef(
                artifact_id=ArtifactId("sha256:" + "a" * 64),
                media_type="application/json",
                byte_length=1,
                schema_version=VERSION,
            ),
        ),
        goal_proposals=(goal,),
        questions=(registered,),
        expected_output_shape="bounded PlanProposal",
    )
    seed = PlanningProblemIdentitySeed(
        need_id=StableId("need.u8c.preregistered.follow-up"),
        question_id=registered.question_id,
        need_query=question,
        semantic_question="预注册: " + question,
        facet=NeedFacetKind.RELATION_STATE,
        source_commit=COMMIT,
        source_text_root=ArtifactId("sha256:" + "3" * 64),
        cutoff_chapter=20,
    )

    planner_questions = _planner_memory_questions(
        inquiry,
        ("模型提出的不同问题",),
        seed,
    )

    assert planner_questions[0].question_id == registered.question_id
    assert planner_questions[0].question == question
    assert planner_questions[0].blocking is True


def test_tianhai_family_english_grounds_tianhai_house_not_only_garden(
    tmp_path: Path,
) -> None:
    """C17: 'Tianhai family' missed 天海家 (empty aliases); 废园+陈长生 paired instead."""

    result = _compile(
        tmp_path / "tianhai-family",
        "What is the current status of the public victory and political backlash "
        "(Tianhai family pressure) relative to the '废园' and '陈长生' "
        "as of the end of chapter 95?",
    )
    need = result.needs[0]
    assert TIANHAI_HOUSE in need.entity_ids
    assert TIANHAI_YAER in need.entity_ids
    assert TIANHAI_SHENGXUE in need.entity_ids
    assert CHEN in need.entity_ids
    assert "天海家" in need.semantic_question
    assert "天海牙儿" not in need.semantic_question
    assert "天海胜雪" not in need.semantic_question
    bundle = result.query_bundles[need.need_id.root]
    assert bundle.exact_entity_ids == need.entity_ids
    assert bundle.graph_seeds == need.entity_ids


def test_tianhai_house_political_pressure_keeps_family_seeds_on_causal_route(
    tmp_path: Path,
) -> None:
    result = _compile(
        tmp_path / "tianhai-political-pressure",
        "What is the current status of the political backlash or pressure from the "
        "天海家 on the 国教学院 after the public victory?",
    )
    need = result.needs[0]
    assert need.query_intent is Stage1QueryIntent.CAUSAL_MULTI_HOP
    assert need.entity_ids[:2] == (TIANHAI_HOUSE, GUOJIAO)
    assert TIANHAI_YAER in need.entity_ids
    assert TIANHAI_SHENGXUE in need.entity_ids
    bundle = result.query_bundles[need.need_id.root]
    assert bundle.exact_entity_ids == need.entity_ids
    assert bundle.graph_seeds == need.entity_ids
    assert "天海家、国教学院" in need.semantic_question


def test_single_entity_political_backlash_is_causal_not_identity_current_state(
    tmp_path: Path,
) -> None:
    """C16: 'political backlash from 天海家' without 'consequences' was CURRENT_STATE."""

    result = _compile(
        tmp_path / "backlash-only",
        "What is the current status of the public victory and political backlash "
        "from 朝廷, and has this event already occurred prior to Chapter 96?",
    )
    need = result.needs[0]
    kinds = {facet.facet_kind for facet in need.need_facets}
    assert NeedFacetKind.CAUSAL_HISTORY in kinds
    assert NeedFacetKind.CURRENT_STATE not in kinds
    assert need.query_intent is Stage1QueryIntent.CAUSAL_MULTI_HOP
    assert CandidatePool.GRAPH in need.allowed_candidate_pools


def test_old_case_relitigation_is_causal_not_current_identity_state(tmp_path: Path) -> None:
    """C42: historical re-litigation wording must use the causal route."""

    result = _compile(
        tmp_path / "old-case",
        "What is the current status of the old case that led to 摘星学院's abandonment, "
        "and is it being re-litigated now?",
    )
    need = result.needs[0]
    assert need.query_intent is Stage1QueryIntent.CAUSAL_MULTI_HOP
    assert NeedFacetKind.CAUSAL_HISTORY in {facet.facet_kind for facet in need.need_facets}
    assert CandidatePool.GRAPH in need.allowed_candidate_pools


def test_compound_planner_question_compiles_facet_scoped_semantic_question(
    tmp_path: Path,
) -> None:
    original = (
        "What is the current state of the 婚约 conflict between 陈长生 and 徐有容 "
        "after the public opposition during ask_the_world, and has 徐有容 returned "
        "from nanhai or is she still in 苦修?"
    )
    result = _compile(tmp_path / "compound", original)
    assert result.needs
    relation = _need_with_facet(result, NeedFacetKind.RELATION_STATE)
    assert relation.query_text == original
    assert "陈长生" in relation.semantic_question
    assert "徐有容" in relation.semantic_question
    assert original in relation.semantic_question
    assert NeedFacetKind.CAUSAL_HISTORY not in {
        facet.facet_kind for need in result.needs for facet in need.need_facets
    }


def test_short_sword_question_does_not_ground_generic_sword(tmp_path: Path) -> None:
    question = (
        "What is the current status of the 短剑 artifact, specifically regarding "
        "its last known usage or manifestation of power?"
    )
    need = _compile(tmp_path / "short-sword", question).needs[0]
    assert SHORT_SWORD in need.entity_ids
    assert SWORD not in need.entity_ids
    assert need.need_facets[0].facet_kind is NeedFacetKind.CURRENT_STATE
    assert "短剑" in need.semantic_question
    assert "当前关系状态" not in need.semantic_question
    assert "last known usage" in need.semantic_question


def test_academy_question_keeps_admission_and_exam_predicates(tmp_path: Path) -> None:
    question = (
        "What is the current status of the 摘星学院, specifically regarding the "
        "'blocked_by_palace_order' admission status and the recent "
        "'fourth_failure' exam result?"
    )
    need = _compile(tmp_path / "academy", question).needs[0]
    assert ACADEMY in need.entity_ids
    assert need.need_facets[0].facet_kind is NeedFacetKind.CURRENT_STATE
    assert "blocked_by_palace_order" in need.semantic_question
    assert "fourth_failure" in need.semantic_question
    assert need.query_text == question


def test_compiler_does_not_use_forbidden_kind_keywords() -> None:
    source = inspect.getsource(PlanningInquiryConditionedNeedGenerator)
    assert "marker in question.casefold()" not in source
    compile_src = inspect.getsource(PlanningInquiryConditionedNeedGenerator._compile_facets)
    assert "RELATION_CAUSAL" not in compile_src
    assert '"关系"' not in compile_src
    assert '"因果"' not in compile_src
    routing_src = inspect.getsource(PlanningInquiryConditionedNeedGenerator._routing)
    assert "allowed_candidate_pools" not in routing_src or "ROUTES" in routing_src
    assert (
        CandidatePool.GRAPH
        in PlanningInquiryConditionedNeedGenerator._routing((NeedFacetKind.RELATION_STATE,))[1]
    )
