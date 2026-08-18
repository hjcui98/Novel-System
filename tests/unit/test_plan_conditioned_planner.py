from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, cast

import pytest
from pydantic import ValidationError

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.benchmark import (
    AuthorPlanningContext,
    PlanRootDocument,
    VisibleOutlineNode,
)
from novel_agent.domain.ids import ArtifactId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.memory import (
    CandidatePool,
    NeedRisk,
    ObligationStatus,
    RequirementLevel,
    ResolutionPath,
    RetrievalChannel,
    Stage1MemoryNeed,
    Stage1QueryIntent,
    WorldRootDocument,
)
from novel_agent.domain.memory_benchmark import BenchmarkInformationProfile
from novel_agent.domain.model_calls import ModelRequest, ModelRole, ModelUsage, ProviderModelResult
from novel_agent.domain.planning_memory import (
    EntityMention,
    GroundingStatus,
    PlannedNeedDraft,
    PlannerArtifactMetadata,
    PlannerFallbackStatus,
    PlannerFinalNeedManifest,
    PlannerInvocationArtifact,
    PlannerInvocationAttempt,
    PlannerInvocationAttemptStatus,
    RelationMention,
)
from novel_agent.domain.world import (
    Event,
    NarrativeOrder,
    RelationRecord,
    StoryTime,
    TruthClass,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.benchmark_importer import content_id
from novel_agent.services.memory_benchmark_contract import build_safe_task_contract
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.need_draft_grounder import NeedDraftGrounder
from novel_agent.services.need_query_compiler import NeedQueryCompiler
from novel_agent.services.need_validator import NeedValidator
from novel_agent.services.plan_conditioned_need_planner import (
    PlanConditionedNeedPlanner,
    PlannerWorldSummaryBuilder,
)
from novel_agent.services.task_conditioned_need_generation import (
    NeedGenerationStatus,
    TaskPlanConditionedNeedGenerator,
)
from novel_agent.services.task_focus import FocusSet, TaskFocusExtractor
from tests.fixtures.stage1_synthetic import make_synthetic_bundle

COMMIT = "sha256:" + "a" * 64


def _planner_context(task: Any) -> Any:
    plan = make_synthetic_bundle().plan_roots[0]
    normalized = {
        "profile": task.information_profile.value,
        "task_intent": task.task_intent,
        "target_range": [task.target_chapter_start, task.target_chapter_end],
        "outline": [node.model_dump(mode="json") for node in plan.nodes],
        "goals": [goal.model_dump(mode="json") for goal in plan.chapter_goals],
    }
    context = AuthorPlanningContext(
        profile=task.information_profile,
        task_intent=task.task_intent,
        target_range=(task.target_chapter_start, task.target_chapter_end),
        visible_outline_nodes=tuple(
            VisibleOutlineNode(
                node_id=node.plan_node_id,
                title=node.title,
                summary=node.summary,
            )
            for node in plan.nodes
        ),
        chapter_goals=plan.chapter_goals,
        source_hash=content_id(normalized),
        planner_may_read_plan=True,
    )
    return context


def _task(
    profile: BenchmarkInformationProfile = BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
    *,
    task_intent: str = "为写 21-23 章准备历史记忆",
) -> Any:
    return build_safe_task_contract(
        case_id=StableId("case.planner"),
        checkpoint_chapter=20,
        target_range=(21, 23),
        information_profile=profile,
        task_intent=(
            task_intent if profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED else ""
        ),
    )


def _entity_world() -> WorldRootDocument:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    teacher = world.entities[0].model_copy(
        update={
            "entity_id": StableId("entity.planner.teacher"),
            "internal_label": "teacher",
            "aliases": ("师",),
        }
    )
    student = teacher.model_copy(
        update={
            "entity_id": StableId("entity.planner.student"),
            "internal_label": "student",
            "aliases": ("小徒",),
        }
    )
    return world.model_copy(
        update={
            "entities": (teacher, *world.entities[1:], student),
            "relations": (
                RelationRecord(
                    relation_id=StableId("relation.planner.teach"),
                    predicate="teaches",
                    subject_id=teacher.entity_id,
                    object_id=student.entity_id,
                    valid_time=StoryTime(worldline="main"),
                    truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                ),
            ),
            "events": (
                Event(
                    event_id=StableId("event.planner.enroll"),
                    event_type="student_enrollment",
                    participant_ids=(teacher.entity_id, student.entity_id),
                    narrative_order=NarrativeOrder(chapter_index=5),
                    evidence_refs=(),
                    truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                ),
            ),
        }
    )


def _draft(
    draft_id: str = "need-1",
    question: str = "在截止点 teacher 的伤势是否仍未痊愈?",
    *,
    facets: tuple[str, ...] = ("CURRENT_STATE",),
    chapters: tuple[int, ...] = (21,),
    goal: str = "重申旧誓言",
    historical_time_scope: str = "main",
) -> PlannedNeedDraft:
    return PlannedNeedDraft(
        draft_id=draft_id,
        semantic_question=question,
        entity_mentions=(EntityMention(label="teacher", role_in_need="subject"),),
        relation_mentions=(),
        trigger_plan_chapters=chapters,
        trigger_plan_goal=goal,
        why_needed="plan the memory reload",
        required_claim_scopes=("current",),
        suggested_facets=facets,
        historical_time_scope=historical_time_scope,
        query_hints=(),
    )


class _PlannerEndpoint:
    is_external = False
    model = "planner-test-model"
    max_retries = 0

    def __init__(self, payloads: tuple[object | Exception, ...]) -> None:
        self.payloads: list[object | Exception] = list(payloads)
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ProviderModelResult:
        self.requests.append(request)
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return ProviderModelResult(
            text=json.dumps(payload, ensure_ascii=False),
            model_version=self.model,
            usage=ModelUsage(input_tokens=10, output_tokens=10, cost_usd=Decimal("0")),
        )


def _gateway(endpoint: _PlannerEndpoint) -> ModelGateway:
    return ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="planner-test",
                model_name=endpoint.model,
                adapter=endpoint,
            ),
        )
    )


def _planner_payload() -> dict[str, object]:
    return {
        "drafts": [
            {
                "draft_id": "marriage-knowledge",
                "semantic_question": "在截止点前 teacher 是否知道 student 的秘密?",
                "entity_mentions": [
                    {"label": "teacher", "role_in_need": "subject"},
                    {"label": "student", "role_in_need": "object"},
                ],
                "relation_mentions": [],
                "trigger_plan_chapters": [22],
                "trigger_plan_goal": "保持受伤状态约束",
                "why_needed": "决定第22章能否揭示秘密",
                "required_claim_scopes": ["knowledge"],
                "suggested_facets": ["KNOWLEDGE_BOUNDARY"],
                "historical_time_scope": "main",
                "query_hints": ["teacher 对 student 秘密的知情情况"],
            },
            {
                "draft_id": "teacher-injury",
                "semantic_question": "在截止点前 teacher 的伤势是否仍未痊愈?",
                "entity_mentions": [
                    {"label": "teacher", "role_in_need": "subject"},
                ],
                "relation_mentions": [],
                "trigger_plan_chapters": [21],
                "trigger_plan_goal": "重申旧誓言",
                "why_needed": "第21章重申誓言依赖伤势状态",
                "required_claim_scopes": ["current"],
                "suggested_facets": ["CURRENT_STATE"],
                "historical_time_scope": "main",
                "query_hints": ["teacher 伤势当前状态"],
            },
            {
                "draft_id": "north-tower-vow",
                "semantic_question": "在截止点前 student 是否已承诺前往北塔?",
                "entity_mentions": [
                    {"label": "student", "role_in_need": "subject"},
                ],
                "relation_mentions": [],
                "trigger_plan_chapters": [23],
                "trigger_plan_goal": "进入北塔",
                "why_needed": "第23章进入北塔前需要恢复承诺来源",
                "required_claim_scopes": ["historical"],
                "suggested_facets": ["CAUSAL_HISTORY"],
                "historical_time_scope": "main",
                "query_hints": ["student 前往北塔的承诺"],
            },
        ],
        "meta": {"rationale": "backward chaining from chapters 21-23"},
    }


def test_planner_prompt_is_general_and_bounded() -> None:
    world = _entity_world()
    task = _task()
    planner = PlanConditionedNeedPlanner()
    context = _planner_context(task)
    summary = PlannerWorldSummaryBuilder.build(task, world, context)
    assert summary.checkpoint_chapter == 20
    assert summary.target_range == (21, 23)
    assert summary.entity_count == len(world.entities)
    prompt = planner._build_prompt(context, summary)
    assert "target chapters" not in prompt
    assert "gold" not in prompt.casefold() or "gold" not in prompt
    assert context.task_intent in prompt
    assert "draft_id" in prompt
    assert "suggested_facets" in prompt
    assert summary.entities
    assert summary.states
    assert "当前状态面" in prompt


def test_world_summary_filters_before_cap_and_prioritizes_relevant_rows() -> None:
    world = _entity_world()
    task = _task(task_intent="关键人物必须履行关键承诺")
    context = _planner_context(task)
    source_entity = world.entities[0]
    irrelevant_entities = tuple(
        source_entity.model_copy(
            update={
                "entity_id": StableId(f"entity.irrelevant.{index}"),
                "internal_label": f"无关人物{index}",
                "aliases": (),
            }
        )
        for index in range(48)
    )
    relevant_entity = source_entity.model_copy(
        update={
            "entity_id": StableId("entity.relevant.last"),
            "internal_label": "关键人物",
            "aliases": (),
        }
    )
    source_obligation = world.obligations[0]
    closed = tuple(
        source_obligation.model_copy(
            update={
                "obligation_id": StableId(f"obligation.closed.{index}"),
                "description": f"已关闭义务{index}",
                "status": ObligationStatus.RESOLVED,
            }
        )
        for index in range(40)
    )
    relevant_open = source_obligation.model_copy(
        update={
            "obligation_id": StableId("obligation.relevant.last"),
            "description": "关键承诺",
            "status": ObligationStatus.OPEN,
            "owner_ids": (relevant_entity.entity_id,),
        }
    )
    expanded = world.model_copy(
        update={
            "entities": (*irrelevant_entities, relevant_entity),
            "obligations": (*closed, relevant_open),
        }
    )

    first = PlannerWorldSummaryBuilder.build(task, expanded, context)
    second = PlannerWorldSummaryBuilder.build(task, expanded, context)

    assert first == second
    assert first.entities[0].label == "关键人物"
    assert tuple(item.description for item in first.open_obligations) == ("关键承诺",)
    assert first.truncated_entity_count == 1
    assert first.truncated_obligation_count == 0


def test_planner_falls_back_without_gateway() -> None:
    task = _task()
    world = _entity_world()
    planner = PlanConditionedNeedPlanner()
    context = _planner_context(task)
    result = planner.plan(task=task, world=world, planning_context=context)
    assert result.fallback_status is PlannerFallbackStatus.PLANNER_FALLBACK
    assert result.drafts == ()
    assert result.error_category == "no_gateway"


def test_planner_parses_drafts_and_records_lineage() -> None:
    endpoint = _PlannerEndpoint((_planner_payload(),))
    gateway = _gateway(endpoint)
    task = _task()
    world = _entity_world()
    planner = PlanConditionedNeedPlanner(gateway=gateway)
    context = _planner_context(task)
    result = planner.plan(task=task, world=world, planning_context=context)
    assert result.fallback_status is PlannerFallbackStatus.PLANNER
    assert len(result.drafts) == 3
    draft = result.drafts[0]
    assert draft.draft_id == "marriage-knowledge"
    assert "秘密" in draft.semantic_question
    assert draft.trigger_plan_chapters == (22,)
    assert draft.suggested_facets == ("KNOWLEDGE_BOUNDARY",)
    assert {item.draft_id for item in result.drafts} == {
        "marriage-knowledge",
        "teacher-injury",
        "north-tower-vow",
    }
    assert result.metadata is not None
    assert result.metadata.planner_model == "planner-test-model"
    assert result.metadata.planner_prompt_version == PlanConditionedNeedPlanner.prompt_version
    assert result.metadata.planning_context_hash == context.source_hash
    assert result.metadata.raw_response_hash != ArtifactId("sha256:" + "0" * 64)
    assert len(endpoint.requests) == 1


def test_planner_retries_and_falls_back_on_garbage_output() -> None:
    endpoint = _PlannerEndpoint(
        (
            {"drafts": "not-a-list"},
            {"no-drafts-key": True},
        )
    )
    gateway = _gateway(endpoint)
    task = _task()
    world = _entity_world()
    planner = PlanConditionedNeedPlanner(gateway=gateway, max_retries=1)
    context = _planner_context(task)
    result = planner.plan(task=task, world=world, planning_context=context)
    assert result.fallback_status is PlannerFallbackStatus.PLANNER_FALLBACK
    assert result.drafts == ()
    assert result.error_category == "ValueError"
    assert len(endpoint.requests) == 2
    assert len({request.request_id for request in endpoint.requests}) == 2
    assert tuple(attempt.request_id for attempt in result.attempts) == tuple(
        request.request_id for request in endpoint.requests
    )
    assert len({record.request_id for record in gateway.call_records}) == 2


def test_planner_draft_parser_filters_malformed_drafts() -> None:
    payload = {
        "drafts": [
            {"draft_id": "bad", "semantic_question": ""},
            {"draft_id": "", "semantic_question": "空 ID 会被跳过"},
            {"draft_id": "ok", "semantic_question": "是否已经完成了修行?"},
            {"draft_id": "odd;;name", "semantic_question": "是否有未决承诺?"},
        ]
    }
    drafts = PlanConditionedNeedPlanner._parse_drafts(json.dumps(payload))
    assert [draft.draft_id for draft in drafts] == ["ok", "odd__name"]
    assert drafts[1].trigger_plan_chapters == ()


def test_grounder_resolves_exact_alias_fuzzy_and_ambiguous_mentions() -> None:
    world = _entity_world()
    grounder = NeedDraftGrounder()
    grounded = grounder.ground(
        PlannedNeedDraft(
            draft_id="g1",
            semantic_question="question?",
            entity_mentions=(
                EntityMention(label="小徒", role_in_need="student"),
                EntityMention(label="unknown-person", role_in_need="other"),
            ),
        ),
        world,
    )
    student = next(entity for entity in world.entities if entity.internal_label == "student")
    mentions = {item.mention: item for item in grounded.entity_mentions}
    assert mentions["小徒"].grounding_status is GroundingStatus.GROUNDED
    assert mentions["小徒"].entity_id == student.entity_id
    assert mentions["unknown-person"].grounding_status is GroundingStatus.UNRESOLVED

    shared_a = world.entities[0].model_copy(
        update={
            "entity_id": StableId("entity.planner.shared-a"),
            "internal_label": "shared",
            "aliases": (),
        }
    )
    shared_b = shared_a.model_copy(
        update={
            "entity_id": StableId("entity.planner.shared-b"),
            "internal_label": "shared",
            "aliases": (),
        }
    )
    ambiguous_world = world.model_copy(update={"entities": (*world.entities, shared_a, shared_b)})
    grounded = grounder.ground(
        PlannedNeedDraft(
            draft_id="g2",
            semantic_question="question?",
            entity_mentions=(EntityMention(label="shared", role_in_need="subject"),),
        ),
        ambiguous_world,
    )
    assert grounded.entity_mentions[0].grounding_status is GroundingStatus.AMBIGUOUS
    assert grounded.entity_mentions[0].entity_id is None


def test_grounder_resolves_relation_context_and_relation_mentions() -> None:
    world = _entity_world()
    teacher = world.entities[0]
    student = next(entity for entity in world.entities if entity.internal_label == "student")
    grounder = NeedDraftGrounder()
    grounded = grounder.ground(
        PlannedNeedDraft(
            draft_id="g3",
            semantic_question="question?",
            entity_mentions=(
                EntityMention(label="teacher", role_in_need="subject"),
                EntityMention(label="student", role_in_need="object"),
            ),
            relation_mentions=(
                RelationMention(
                    subject_label="teacher",
                    relation_label="teaches",
                    object_label="student",
                ),
            ),
        ),
        world,
    )
    relation = grounded.relation_mentions[0]
    assert relation.grounding_status is GroundingStatus.GROUNDED
    assert relation.relation_id == StableId("relation.planner.teach")
    assert grounder.grounded_entity_ids(grounded) == (teacher.entity_id, student.entity_id)


def test_validator_rejects_dedupes_and_truncates() -> None:
    task = _task()
    world = _entity_world()
    validator = NeedValidator()
    draft_a = _draft("a", "teacher 的伤势是否痊愈?", chapters=(21,))
    draft_b = _draft("b", "teacher 的伤势是否痊愈?", chapters=(21,))
    draft_out = _draft("out", "question?", chapters=(30,))
    draft_fact = _draft("fact", "重申旧誓言", chapters=(21,), goal="重申旧誓言")
    draft_scope = _draft("scope", "question?", historical_time_scope="future-line")
    draft_unknown_facet = _draft("unknown-facet", "question?", facets=("UNKNOWN",))
    draft_missing_goal_binding = _draft("missing-goal", "question?", chapters=())
    # The model's trigger_plan_goal is only an auditable explanation after the
    # 2026-08-13 repair: a semantically different restatement must not reject
    # an otherwise legal draft (P001/P003/P005 root cause).
    draft_goal_mismatch = _draft("goal-mismatch", "teacher 是否痊愈?", goal="not canonical")
    draft_no_mention = PlannedNeedDraft(
        draft_id="no-mention",
        semantic_question="历史状态与当前状态是否一致?",
        entity_mentions=(),
        relation_mentions=(),
        trigger_plan_chapters=(21,),
        trigger_plan_goal="重申旧誓言",
        required_claim_scopes=("current",),
        suggested_facets=("CURRENT_STATE",),
    )
    grounder = NeedDraftGrounder()
    grounded = tuple(
        grounder.ground(draft, world)
        for draft in (
            draft_a,
            draft_b,
            draft_out,
            draft_fact,
            draft_scope,
            draft_unknown_facet,
            draft_missing_goal_binding,
            draft_goal_mismatch,
            draft_no_mention,
        )
    )
    focus_set = TaskFocusExtractor().extract(task, world)
    result = validator.validate(
        drafts=grounded,
        task=task,
        world=world,
        focus_set=focus_set,
        plan=make_synthetic_bundle().plan_roots[0],
    )
    assert [draft.draft_id for draft in result.accepted_drafts] == ["a", "goal-mismatch"]
    assert set(result.rejected_draft_ids) == {
        "out",
        "fact",
        "scope",
        "unknown-facet",
        "missing-goal",
        "no-mention",
    }
    assert result.rejected_reasons["out"] == "out_of_range_chapters"
    assert result.rejected_reasons["fact"] == "plan_goal_as_fact"
    assert result.rejected_reasons["scope"] == "unknown_time_scope"
    assert result.rejected_reasons["unknown-facet"] == "unknown_or_empty_scope_facet"
    assert result.rejected_reasons["missing-goal"] == "missing_trigger_goal_binding"
    assert result.rejected_reasons["no-mention"] == "no_anchoring_mention"
    assert result.deduplicated_draft_ids == ("b",)
    assert result.grounded_entity_count >= 1

    tiny = NeedValidator(max_total_needs=1)
    draft_c = _draft("c", "teacher 是否有未决的承诺?", chapters=(21,))
    grounded_c = grounder.ground(draft_c, world)
    result = tiny.validate(
        drafts=(grounded[0], grounded_c),
        task=task,
        world=world,
        focus_set=focus_set,
        plan=make_synthetic_bundle().plan_roots[0],
    )
    assert [draft.draft_id for draft in result.accepted_drafts] == ["a"]
    assert result.truncated_draft_ids == ("c",)


def test_validator_accepts_semantically_equivalent_and_empty_goal_explanation() -> None:
    """2026-08-13 repair: trigger_plan_goal is explanation, not binding identity."""
    task = _task()
    world = _entity_world()
    grounder = NeedDraftGrounder()
    focus_set = TaskFocusExtractor().extract(task, world)
    paraphrase = grounder.ground(
        _draft("paraphrase", "teacher 伤势是否痊愈?", goal="与正文章节目标语义等价的不同表述"),
        world,
    )
    no_explanation = grounder.ground(
        _draft("no-goal-text", "teacher 当前伤势状态?", goal=""),
        world,
    )
    result = NeedValidator().validate(
        drafts=(paraphrase, no_explanation),
        task=task,
        world=world,
        focus_set=focus_set,
        plan=make_synthetic_bundle().plan_roots[0],
    )
    assert [draft.draft_id for draft in result.accepted_drafts] == [
        "paraphrase",
        "no-goal-text",
    ]
    assert result.rejected_reasons == {}


def test_planner_need_binds_canonical_goal_by_chapter() -> None:
    """The host binds the plan's canonical goal text onto planner-derived Needs."""
    task = _task()
    world = _entity_world()
    plan = make_synthetic_bundle().plan_roots[0]
    context = _planner_context(task)
    gateway = _gateway(_PlannerEndpoint((_planner_payload(),)))
    result = TaskPlanConditionedNeedGenerator(planner_gateway=gateway).generate_with_lineage(
        task,
        world,
        plan,
        context,
    )
    planner_needs = tuple(need for need in result.needs if need.trigger_plan_chapters)
    assert planner_needs
    for need in planner_needs:
        assert need.canonical_goal_by_chapter
        assert set(need.canonical_goal_by_chapter) == set(need.trigger_plan_chapters)
        assert all(goal.strip() for goal in need.canonical_goal_by_chapter.values())


def test_validator_need_type_mapping_and_sanitization() -> None:
    assert NeedValidator.need_type_for_facets(("PLAN_NODE",)) == "plan_obligation"
    assert NeedValidator.need_type_for_facets(("KNOWLEDGE_BOUNDARY",)) == "knowledge_boundary"
    assert NeedValidator.need_type_for_facets(("CAPABILITY_STATUS", "LIMITATION")) == (
        "capability_boundary"
    )
    assert NeedValidator.need_type_for_facets(("RELATION_STATE",)) == "relationship_emotion"
    assert NeedValidator.need_type_for_facets(("COMMITMENT",)) == "unresolved_obligation"
    assert NeedValidator.need_type_for_facets(("SETUP",)) == "long_range_callback"
    assert NeedValidator.need_type_for_facets(("CAUSAL_HISTORY",)) == "entity_history"
    assert NeedValidator.need_type_for_facets(()) == "current_state"
    assert NeedValidator.sanitize_draft_id("a/b:c d") == "a_b_c_d"
    assert NeedValidator.sanitize_draft_id("") == "draft"


def test_focus_set_extend_backfills_grounded_entities_bounded() -> None:
    task = _task()
    world = _entity_world()
    extractor = TaskFocusExtractor()
    focus_set = extractor.extract(task, world)
    teacher = world.entities[0]
    student = next(entity for entity in world.entities if entity.internal_label == "student")
    bystander = teacher.model_copy(
        update={
            "entity_id": StableId("entity.planner.bystander"),
            "internal_label": "bystander",
            "aliases": (),
        }
    )
    before = len(focus_set.focuses)
    extended = extractor.extend(
        focus_set, (teacher.entity_id, student.entity_id, bystander.entity_id)
    )
    assert len(extended.focuses) == before + 1
    twice = extractor.extend(extended, (bystander.entity_id,))
    assert len(twice.focuses) == len(extended.focuses)
    tiny = TaskFocusExtractor(max_focuses=before + 1)
    second_bystander = teacher.model_copy(
        update={
            "entity_id": StableId("entity.planner.bystander-2"),
            "internal_label": "bystander-2",
            "aliases": (),
        }
    )
    capped = tiny.extend(focus_set, (bystander.entity_id, second_bystander.entity_id))
    assert len(capped.focuses) == before + 1
    assert len(capped.truncated_focus_ids) == 1


def test_generator_planner_chain_emits_lineaged_needs() -> None:
    endpoint = _PlannerEndpoint((_planner_payload(),))
    gateway = _gateway(endpoint)
    task = _task()
    world = _entity_world()
    plan = make_synthetic_bundle().plan_roots[0]
    generator = TaskPlanConditionedNeedGenerator(planner_gateway=gateway)
    context = _planner_context(task)
    result = generator.generate_with_lineage(task, world, plan, context)
    assert result.status is NeedGenerationStatus.READY
    assert result.fallback_used is False
    assert result.planner_metadata is not None
    needs = result.needs
    assert needs
    need = next(item for item in needs if item.planned_draft_id == "marriage-knowledge")
    assert need.need_type == "knowledge_boundary"
    assert need.semantic_question == need.query_text
    assert need.semantic_question
    assert need.planner_artifact_ref is not None
    assert need.validated_need_set_hash is not None
    assert result.planner_artifact is not None
    assert need.planner_artifact_ref == content_id(result.planner_artifact.model_dump(mode="json"))
    assert need.trigger_plan_chapters == (22,)
    assert need.entity_ids
    assert need.need_facets
    assert need.completion_spec is not None
    assert need.query_hints
    assert need.requirement.value == "mandatory"
    assert need.completion_spec is not None
    assert (
        need.completion_spec.irreducible_need_facet_ids
        == need.completion_spec.required_need_facet_ids
    )
    assert all(item is not None for item in (need.semantic_question,))
    # The plan-obligation channel remains explicit when the planner asks for it.
    assert all(item.claim_may_cite_plan is False for item in needs)
    # Evidence-first default: no active "one current claim" stop condition and
    # no current-claim gate in the default NeedCompletionSpec.
    for item in needs:
        assert item.completion_spec is not None
        assert item.completion_spec.require_current_claim is False
        assert "one current claim" not in item.stop_condition
        assert "claim is supported" not in (item.completion_criteria or "")
        assert item.stop_condition == (
            "served by cutoff-safe exact evidence slices or an explicit typed gap"
        )


def test_planner_artifact_is_content_addressed_replayable_and_basis_checked(
    tmp_path: Any,
) -> None:
    endpoint = _PlannerEndpoint((_planner_payload(),))
    gateway = _gateway(endpoint)
    task = _task()
    world = _entity_world()
    plan = make_synthetic_bundle().plan_roots[0]
    context = _planner_context(task)
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    generator = TaskPlanConditionedNeedGenerator(
        planner_gateway=gateway,
        planner_artifact_writer=lambda payload, media_type: repository.put(
            payload, media_type, SchemaVersion("1.0.0")
        ),
    )

    result = generator.generate_with_lineage(task, world, plan, context)

    assert result.planner_artifact_document_ref is not None
    artifact = PlannerInvocationArtifact.model_validate_json(
        repository.read_verified(result.planner_artifact_document_ref)
    )
    assert result.needs[0].planner_artifact_ref == result.planner_artifact_document_ref.artifact_id
    calls_before_replay = len(gateway.call_records)
    assert artifact.metadata is not None
    replay = PlanConditionedNeedPlanner(gateway=gateway).replay(
        artifact,
        task=task,
        world=world,
        planning_context=context,
        planner_model=artifact.metadata.planner_model,
        planner_model_revision=artifact.metadata.planner_model_revision,
    )
    assert replay.drafts == artifact.parsed_drafts
    assert len(gateway.call_records) == calls_before_replay
    regenerated = generator.generate_with_lineage(
        task,
        world,
        plan,
        context,
        frozen_planner_artifact=artifact,
    )
    assert len(gateway.call_records) == calls_before_replay
    assert regenerated.planner_artifact == artifact
    assert regenerated.planner_artifact_document_ref == result.planner_artifact_document_ref
    assert regenerated.planner_artifact_document_ref is not None
    assert all(
        need.planner_artifact_ref == regenerated.planner_artifact_document_ref.artifact_id
        for need in regenerated.needs
    )
    assert tuple(
        (
            need.need_id,
            need.completion_spec,
            NeedQueryCompiler().compile(need),
            need.validated_need_set_hash,
        )
        for need in regenerated.needs
    ) == tuple(
        (
            need.need_id,
            need.completion_spec,
            NeedQueryCompiler().compile(need),
            need.validated_need_set_hash,
        )
        for need in result.needs
    )
    with pytest.raises(ValueError, match="replay basis mismatch"):
        PlanConditionedNeedPlanner(gateway=gateway).replay(
            artifact,
            task=task,
            world=world,
            planning_context=context.model_copy(
                update={"source_hash": content_id({"different": "context"})}
            ),
            planner_model=artifact.metadata.planner_model,
            planner_model_revision=artifact.metadata.planner_model_revision,
        )
    with pytest.raises(ValueError, match="grounded drafts mismatch"):
        generator.generate_with_lineage(
            task,
            world,
            plan,
            context,
            frozen_planner_artifact=artifact.model_copy(update={"grounded_drafts": ()}),
        )
    with pytest.raises(ValueError, match="validation outcome mismatch"):
        generator.generate_with_lineage(
            task,
            world,
            plan,
            context,
            frozen_planner_artifact=artifact.model_copy(update={"accepted_draft_ids": ()}),
        )
    with pytest.raises(ValueError, match="final Need set mismatch"):
        generator.generate_with_lineage(
            task,
            world,
            plan,
            context,
            frozen_planner_artifact=artifact.model_copy(update={"final_need_manifests": ()}),
        )
    with pytest.raises(ValueError, match="requires AuthorPlanningContext"):
        generator._replay_planner_artifact(task, world, plan, None, artifact)
    with pytest.raises(ValueError, match="requires model-policy metadata"):
        generator._replay_planner_artifact(
            task,
            world,
            plan,
            context,
            artifact.model_copy(update={"metadata": None}),
        )
    with pytest.raises(ValueError, match="lineage count"):
        generator._final_need_manifests(result.needs, ())
    with pytest.raises(ValueError, match="missing its invocation artifact"):
        TaskPlanConditionedNeedGenerator()._finalize_fallback_lineage(())


def test_generator_falls_back_to_templates_when_planner_fails(tmp_path: Any) -> None:
    endpoint = _PlannerEndpoint(({"drafts": "broken"}, {"broken": True}))
    gateway = _gateway(endpoint)
    task = _task()
    world = _entity_world()
    plan = make_synthetic_bundle().plan_roots[0]
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "fallback-objects"))
    generator = TaskPlanConditionedNeedGenerator(
        planner_gateway=gateway,
        planner_artifact_writer=lambda payload, media_type: repository.put(
            payload, media_type, SchemaVersion("1.0.0")
        ),
    )
    context = _planner_context(task)
    result = generator.generate_with_lineage(task, world, plan, context)
    assert result.status is NeedGenerationStatus.PLANNER_FALLBACK
    assert result.fallback_used is True
    assert result.planner_artifact_document_ref is not None
    fallback_artifact = PlannerInvocationArtifact.model_validate_json(
        repository.read_verified(result.planner_artifact_document_ref)
    )
    assert fallback_artifact.fallback_status is PlannerFallbackStatus.PLANNER_FALLBACK
    assert fallback_artifact.fallback_reason is not None
    assert fallback_artifact.metadata is not None
    calls_before_replay = len(gateway.call_records)
    replay = PlanConditionedNeedPlanner(gateway=gateway).replay(
        fallback_artifact,
        task=task,
        world=world,
        planning_context=context,
        planner_model=fallback_artifact.metadata.planner_model,
        planner_model_revision=fallback_artifact.metadata.planner_model_revision,
    )
    assert replay.drafts == ()
    assert replay.fallback_status is PlannerFallbackStatus.PLANNER_FALLBACK
    assert len(gateway.call_records) == calls_before_replay
    assert result.needs  # deterministic template needs survive the fallback
    assert fallback_artifact.final_need_manifests
    assert len(fallback_artifact.final_need_manifests) == len(result.needs)
    assert len({attempt.request_id for attempt in fallback_artifact.attempts}) == 2
    assert all(item.planner_artifact_ref is not None for item in result.needs)
    assert all(item.planned_draft_id is not None for item in result.needs)
    assert all(item.semantic_question for item in result.needs)
    regenerated = generator.generate_with_lineage(
        task,
        world,
        plan,
        context,
        frozen_planner_artifact=fallback_artifact,
    )
    assert len(gateway.call_records) == calls_before_replay
    assert regenerated.planner_artifact == fallback_artifact
    assert tuple(
        (
            need.need_id,
            need.completion_spec,
            NeedQueryCompiler().compile(need),
            need.validated_need_set_hash,
        )
        for need in regenerated.needs
    ) == tuple(
        (
            need.need_id,
            need.completion_spec,
            NeedQueryCompiler().compile(need),
            need.validated_need_set_hash,
        )
        for need in result.needs
    )
    with pytest.raises(ValueError, match="final Need set mismatch"):
        generator.generate_with_lineage(
            task,
            world,
            plan,
            context,
            frozen_planner_artifact=fallback_artifact.model_copy(
                update={"final_need_manifests": ()}
            ),
        )


def test_generator_grounding_status_counts_covers_all_statuses() -> None:
    from novel_agent.domain.planning_memory import (
        GroundedEntityMention,
        GroundingStatus,
    )
    from novel_agent.services.need_draft_grounder import NeedDraftGrounder

    world = _entity_world()
    teacher = world.entities[0]
    grounder = NeedDraftGrounder()
    grounded = grounder.ground(
        PlannedNeedDraft(
            draft_id="statuses",
            semantic_question="问题?",
            entity_mentions=(
                EntityMention(label="teacher", role_in_need="subject"),
                EntityMention(label="ghost-a", role_in_need="other"),
                EntityMention(label="ghost-b", role_in_need="other"),
            ),
        ),
        world,
    )
    ambiguous = grounded.model_copy(
        update={
            "entity_mentions": (
                *grounded.entity_mentions[:1],
                GroundedEntityMention(
                    mention="shared",
                    canonical_label="shared",
                    entity_id=None,
                    confidence=0.4,
                    grounding_method="ambiguous_label_match",
                    grounding_status=GroundingStatus.AMBIGUOUS,
                ),
                *grounded.entity_mentions[1:],
            )
        }
    )
    counts = TaskPlanConditionedNeedGenerator._grounding_status_counts((ambiguous,))
    assert counts == (1, 1, 2)
    assert teacher.entity_id is not None


def test_generator_vac_path_never_runs_the_planner() -> None:
    task = _task(profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF)
    world = _entity_world()
    generator = TaskPlanConditionedNeedGenerator(
        planner_gateway=_gateway(_PlannerEndpoint((_planner_payload(),)))
    )
    result = generator.generate_with_lineage(task, world, None)
    assert result.status is not NeedGenerationStatus.PLANNER_FALLBACK
    assert result.fallback_used is False
    assert result.needs
    fallback_artifact = PlannerInvocationArtifact(
        planning_context=_planner_context(_task()),
        world_summary=PlannerWorldSummaryBuilder.build(_task(), world, _planner_context(_task())),
        exact_prompt="unused",
        validated_need_set_hash=content_id({"empty": True}),
        fallback_status=PlannerFallbackStatus.PLANNER_FALLBACK,
        fallback_reason="no_gateway",
    )
    with pytest.raises(ValueError, match="requires an APC task"):
        generator.generate_with_lineage(
            task,
            world,
            None,
            frozen_planner_artifact=fallback_artifact,
        )


def test_planner_fallback_with_empty_focus_still_binds_empty_final_set() -> None:
    task = _task()
    world = _entity_world()
    plan = make_synthetic_bundle().plan_roots[0]

    class EmptyFocusExtractor(TaskFocusExtractor):
        def extract(self, task: Any, world: Any, plan: Any = None) -> FocusSet:
            return FocusSet(task_id=task.task_id, focuses=())

    broken_response: dict[str, object] = {"broken": True}
    broken_responses: tuple[dict[str, object] | Exception, ...] = (
        broken_response,
        broken_response,
    )
    generator = TaskPlanConditionedNeedGenerator(
        planner_gateway=_gateway(_PlannerEndpoint(broken_responses)),
        focus_extractor=EmptyFocusExtractor(),
    )
    result = generator.generate_with_lineage(task, world, plan, _planner_context(task))
    assert result.needs == ()
    assert result.planner_artifact is not None
    assert result.planner_artifact.final_need_manifests == ()
    assert result.planner_metadata is not None


def test_generator_planner_chain_falls_back_when_all_drafts_rejected() -> None:
    payload: dict[str, object] = {
        "drafts": [
            {
                "draft_id": "out-of-range",
                "semantic_question": "这个问题指向范围外章节?",
                "entity_mentions": [{"label": "teacher", "role_in_need": "subject"}],
                "trigger_plan_chapters": [30],
                "suggested_facets": ["CURRENT_STATE"],
                "historical_time_scope": "main",
            }
        ]
    }
    endpoint = _PlannerEndpoint((payload,))
    gateway = _gateway(endpoint)
    task = _task()
    world = _entity_world()
    plan = make_synthetic_bundle().plan_roots[0]
    generator = TaskPlanConditionedNeedGenerator(planner_gateway=gateway)
    context = _planner_context(task)
    result = generator.generate_with_lineage(task, world, plan, context)
    assert result.status is NeedGenerationStatus.PLANNER_FALLBACK
    assert result.fallback_used is True
    assert result.needs


def test_apc_planner_fails_closed_without_compiled_context() -> None:
    task = _task()
    generator = TaskPlanConditionedNeedGenerator(
        planner_gateway=_gateway(_PlannerEndpoint((_planner_payload(),)))
    )
    with pytest.raises(ValueError, match="compiled AuthorPlanningContext"):
        generator.generate_with_lineage(
            task,
            _entity_world(),
            make_synthetic_bundle().plan_roots[0],
        )


def test_generator_rejects_plan_obligation_as_historical_memory_need() -> None:
    payload: dict[str, object] = {
        "drafts": [
            {
                "draft_id": "plan-goal-reload",
                "semantic_question": "写第22章前需要恢复哪些与目标计划相关的历史约束?",
                "entity_mentions": [],
                "relation_mentions": [],
                "trigger_plan_chapters": [22],
                "trigger_plan_goal": "保持受伤状态约束",
                "why_needed": "该章计划要求恢复相关历史约束",
                "required_claim_scopes": ["planned"],
                "suggested_facets": ["PLAN_NODE"],
                "historical_time_scope": "main",
            }
        ]
    }
    endpoint = _PlannerEndpoint((payload,))
    gateway = _gateway(endpoint)
    task = _task()
    world = _entity_world()
    plan = make_synthetic_bundle().plan_roots[0]
    generator = TaskPlanConditionedNeedGenerator(planner_gateway=gateway)
    context = _planner_context(task)
    result = generator.generate_with_lineage(task, world, plan, context)
    assert result.status is NeedGenerationStatus.PLANNER_FALLBACK
    assert all(item.planned_draft_id != "plan-goal-reload" for item in result.needs)
    assert all(item.retrieval_may_return_plan is False for item in result.needs)
    assert all(item.claim_may_cite_plan is False for item in result.needs)


def test_planning_memory_domain_edges() -> None:
    with pytest.raises(ValidationError, match="must be unique"):
        PlannedNeedDraft(
            draft_id="dup",
            semantic_question="question?",
            entity_mentions=(
                EntityMention(label="same", role_in_need="a"),
                EntityMention(label="same", role_in_need="b"),
            ),
        )
    with pytest.raises(ValidationError, match="positive"):
        PlannedNeedDraft(
            draft_id="neg",
            semantic_question="question?",
            trigger_plan_chapters=(0,),
        )
    with pytest.raises(ValidationError, match="chapters must be unique"):
        PlannedNeedDraft(
            draft_id="dup-chapters",
            semantic_question="question?",
            trigger_plan_chapters=(21, 21),
        )
    with pytest.raises(ValidationError, match="real raw response hash"):
        PlannerArtifactMetadata(
            run_id=RunId("run.planner"),
            planner_model="m",
            planner_model_revision="r",
            planner_prompt_version="p1",
            planner_prompt_hash=content_id({"prompt": 1}),
            planner_output_schema_version="v1",
            temperature=0.0,
            requested_seed=None,
            effective_seed_supported=False,
            planning_context_hash=content_id({"ctx": 1}),
            world_summary_hash=content_id({"world": 1}),
            raw_response_hash=ArtifactId("sha256:" + "0" * 64),
            validated_need_set_hash=content_id({"validated": 1}),
            fallback_used=False,
            input_tokens=1,
            output_tokens=1,
        )
    from novel_agent.domain.planning_memory import PlannerWorldSummary

    with pytest.raises(ValidationError, match="target range is invalid"):
        PlannerWorldSummary(
            checkpoint_chapter=20,
            target_range=(23, 21),
            entity_count=0,
            state_count=0,
            event_count=0,
            relation_count=0,
            obligation_count=0,
        )
    summary_base = {
        "checkpoint_chapter": 20,
        "target_range": (21, 23),
        "entity_count": 1,
        "state_count": 0,
        "event_count": 0,
        "relation_count": 1,
        "obligation_count": 0,
    }
    with pytest.raises(ValidationError, match="entity truncation"):
        PlannerWorldSummary.model_validate(summary_base)
    with pytest.raises(ValidationError, match="relation truncation"):
        PlannerWorldSummary.model_validate(summary_base | {"truncated_entity_count": 1})
    with pytest.raises(ValidationError, match="state truncation"):
        PlannerWorldSummary.model_validate(
            summary_base | {"truncated_entity_count": 1, "state_count": 1}
        )


def test_planner_invocation_artifact_consistency_edges() -> None:
    task = _task()
    context = _planner_context(task)
    world_summary = PlannerWorldSummaryBuilder.build(task, _entity_world(), context)
    valid_hash = content_id({"validated": True})
    metadata = PlannerArtifactMetadata(
        run_id=RunId("run.planner.edges"),
        planner_model="model",
        planner_model_revision="revision",
        planner_prompt_version="prompt-v1",
        planner_prompt_hash=content_id({"prompt": True}),
        planner_output_schema_version="v1",
        temperature=0.0,
        requested_seed=None,
        effective_seed_supported=False,
        planning_context_hash=context.source_hash,
        world_summary_hash=content_id(world_summary.model_dump(mode="json")),
        raw_response_hash=content_id({"raw": True}),
        validated_need_set_hash=valid_hash,
        fallback_used=False,
        input_tokens=1,
        output_tokens=1,
    )
    base = {
        "planning_context": context,
        "world_summary": world_summary,
        "exact_prompt": "prompt",
        "metadata": metadata,
        "validated_need_set_hash": valid_hash,
        "fallback_status": PlannerFallbackStatus.PLANNER,
    }
    with pytest.raises(ValidationError, match="planning context hash mismatch"):
        PlannerInvocationArtifact(
            **(
                base
                | {
                    "metadata": metadata.model_copy(
                        update={"planning_context_hash": content_id({"other": True})}
                    )
                }
            )
        )
    with pytest.raises(ValidationError, match="validated set hash mismatch"):
        PlannerInvocationArtifact(
            **(base | {"validated_need_set_hash": content_id({"other": True})})
        )
    with pytest.raises(ValidationError, match="requires a reason"):
        PlannerInvocationArtifact(
            **(base | {"fallback_status": PlannerFallbackStatus.PLANNER_FALLBACK})
        )
    with pytest.raises(ValidationError, match="cannot carry fallback reason"):
        PlannerInvocationArtifact(**(base | {"fallback_reason": "unexpected"}))
    metadata_free_fallback = PlannerInvocationArtifact(
        **(
            base
            | {
                "metadata": None,
                "fallback_status": PlannerFallbackStatus.PLANNER_FALLBACK,
                "fallback_reason": "no_gateway",
            }
        )
    )
    assert metadata_free_fallback.metadata is None

    attempt = PlannerInvocationAttempt(
        request_id=StableId("planner.attempt.1"),
        status=PlannerInvocationAttemptStatus.SUCCEEDED,
        raw_response="{}",
        raw_response_hash=content_id({"raw": "{}"}),
        input_tokens=1,
        output_tokens=1,
    )
    with pytest.raises(ValidationError, match="requires an error category"):
        PlannerInvocationAttempt(
            **(
                attempt.model_dump()
                | {
                    "status": PlannerInvocationAttemptStatus.ERROR,
                    "error_category": None,
                }
            )
        )
    with pytest.raises(ValidationError, match="cannot carry an error category"):
        PlannerInvocationAttempt(**(attempt.model_dump() | {"error_category": "unexpected"}))
    manifest = PlannerFinalNeedManifest(
        need_id=StableId("need.final.1"),
        source_draft_id="draft.1",
        need_payload_hash=content_id({"need": 1}),
        completion_contract_hash=content_id({"completion": 1}),
        query_bundle_hash=content_id({"query": 1}),
    )
    with pytest.raises(ValidationError, match="attempt request ids must be unique"):
        PlannerInvocationArtifact(**(base | {"attempts": (attempt, attempt)}))
    with pytest.raises(ValidationError, match="final Need ids must be unique"):
        PlannerInvocationArtifact(**(base | {"final_need_manifests": (manifest, manifest)}))


def test_grounder_fuzzy_mentions_fail_closed_without_dense_inference() -> None:
    world = _entity_world()
    grounder = NeedDraftGrounder()
    # 徒 is only a substring of the alias 小徒: without fuzzy/dense inference
    # the mention stays unresolved (fail closed).
    fuzzy = grounder.ground(
        PlannedNeedDraft(
            draft_id="fuzzy",
            semantic_question="question?",
            entity_mentions=(EntityMention(label="徒", role_in_need="student"),),
        ),
        world,
    )
    assert fuzzy.entity_mentions[0].grounding_status is GroundingStatus.UNRESOLVED
    assert fuzzy.entity_mentions[0].grounding_method == "no_label_match"

    student_b = world.entities[0].model_copy(
        update={
            "entity_id": StableId("entity.planner.student-b"),
            "internal_label": "student-b",
            "aliases": ("小心",),
        }
    )
    ambiguous_world = world.model_copy(update={"entities": (*world.entities, student_b)})
    fuzzy_ambiguous = grounder.ground(
        PlannedNeedDraft(
            draft_id="fuzzy-ambiguous",
            semantic_question="question?",
            entity_mentions=(EntityMention(label="小", role_in_need="student"),),
        ),
        ambiguous_world,
    )
    assert fuzzy_ambiguous.entity_mentions[0].grounding_status is GroundingStatus.UNRESOLVED
    assert fuzzy_ambiguous.entity_mentions[0].grounding_method == "no_label_match"


def test_grounder_shared_internal_label_fails_closed_without_relation_context() -> None:
    world = _entity_world()
    teacher = world.entities[0]
    student = next(entity for entity in world.entities if entity.internal_label == "student")
    shared_a = teacher.model_copy(
        update={
            "entity_id": StableId("entity.planner.shared-a"),
            "internal_label": "shared",
            "aliases": (),
        }
    )
    shared_b = shared_a.model_copy(
        update={
            "entity_id": StableId("entity.planner.shared-b"),
            "internal_label": "shared",
            "aliases": (),
        }
    )
    world = world.model_copy(
        update={
            "entities": (*world.entities, shared_a, shared_b),
            "relations": (
                RelationRecord(
                    relation_id=StableId("relation.planner.shared-teaches"),
                    predicate="teaches",
                    subject_id=shared_a.entity_id,
                    object_id=student.entity_id,
                    valid_time=StoryTime(worldline="main"),
                    truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                ),
            ),
        }
    )
    grounder = NeedDraftGrounder()
    grounded = grounder.ground(
        PlannedNeedDraft(
            draft_id="shared-context",
            semantic_question="question?",
            entity_mentions=(
                EntityMention(label="shared", role_in_need="subject"),
                EntityMention(label="student", role_in_need="object"),
            ),
            relation_mentions=(
                RelationMention(
                    subject_label="shared",
                    relation_label="teaches",
                    object_label="student",
                ),
            ),
        ),
        world,
    )
    shared_mention = next(item for item in grounded.entity_mentions if item.mention == "shared")
    assert shared_mention.grounding_status is GroundingStatus.AMBIGUOUS
    assert shared_mention.entity_id is None
    assert shared_mention.grounding_method == "ambiguous_label_match"


def test_grounder_relation_edge_statuses() -> None:
    world = _entity_world()
    teacher = world.entities[0]
    student = next(entity for entity in world.entities if entity.internal_label == "student")
    grounder = NeedDraftGrounder()

    unresolved_endpoint = grounder.ground(
        PlannedNeedDraft(
            draft_id="rel-unresolved",
            semantic_question="question?",
            entity_mentions=(EntityMention(label="teacher", role_in_need="subject"),),
            relation_mentions=(
                RelationMention(
                    subject_label="teacher",
                    relation_label="teaches",
                    object_label="ghost",
                ),
            ),
        ),
        world,
    )
    assert unresolved_endpoint.relation_mentions[0].grounding_status is GroundingStatus.UNRESOLVED

    no_relation = grounder.ground(
        PlannedNeedDraft(
            draft_id="rel-missing",
            semantic_question="question?",
            entity_mentions=(
                EntityMention(label="teacher", role_in_need="subject"),
                EntityMention(label="student", role_in_need="object"),
            ),
            relation_mentions=(
                RelationMention(
                    subject_label="teacher",
                    relation_label="does_not_exist",
                    object_label="student",
                ),
            ),
        ),
        world,
    )
    assert no_relation.relation_mentions[0].grounding_status is GroundingStatus.UNRESOLVED
    assert no_relation.relation_mentions[0].grounding_method == "no_relation_match"

    duplicate_relation_world = world.model_copy(
        update={
            "relations": (
                *world.relations,
                RelationRecord(
                    relation_id=StableId("relation.planner.teach-duplicate"),
                    predicate="teaches",
                    subject_id=teacher.entity_id,
                    object_id=student.entity_id,
                    valid_time=StoryTime(worldline="main"),
                    truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                ),
            )
        }
    )
    duplicate = grounder.ground(
        PlannedNeedDraft(
            draft_id="rel-dup",
            semantic_question="question?",
            entity_mentions=(
                EntityMention(label="teacher", role_in_need="subject"),
                EntityMention(label="student", role_in_need="object"),
            ),
            relation_mentions=(
                RelationMention(
                    subject_label="teacher",
                    relation_label="teaches",
                    object_label="student",
                ),
            ),
        ),
        duplicate_relation_world,
    )
    assert duplicate.relation_mentions[0].grounding_status is GroundingStatus.AMBIGUOUS

    ambiguous_endpoint = grounder.ground(
        PlannedNeedDraft(
            draft_id="rel-ambiguous-endpoint",
            semantic_question="question?",
            entity_mentions=(
                EntityMention(label="teacher", role_in_need="subject"),
                EntityMention(label="student", role_in_need="object"),
            ),
            relation_mentions=(
                RelationMention(
                    subject_label="teacher",
                    relation_label="teaches",
                    object_label="student",
                ),
            ),
        ),
        world.model_copy(
            update={
                "entities": (
                    teacher.model_copy(
                        update={
                            "entity_id": StableId("entity.planner.dup-a"),
                            "aliases": ("师",),
                        }
                    ),
                    teacher.model_copy(
                        update={
                            "entity_id": StableId("entity.planner.dup-b"),
                            "aliases": ("师",),
                        }
                    ),
                    student,
                )
            }
        ),
    )
    assert ambiguous_endpoint.relation_mentions[0].grounding_status is GroundingStatus.AMBIGUOUS


def test_grounder_skips_blank_and_duplicate_entity_labels() -> None:
    world = _entity_world()
    teacher = world.entities[0]
    messy = teacher.model_copy(
        update={
            "entity_id": StableId("entity.planner.messy"),
            "internal_label": "messy",
            "aliases": ("Messy", "别名"),
        }
    )
    grounder = NeedDraftGrounder()
    grounded = grounder.ground(
        PlannedNeedDraft(
            draft_id="messy",
            semantic_question="question?",
            entity_mentions=(
                EntityMention(label="messy", role_in_need="subject"),
                EntityMention(label="别名", role_in_need="subject"),
            ),
        ),
        world.model_copy(update={"entities": (*world.entities, messy)}),
    )
    mentions = {item.mention: item for item in grounded.entity_mentions}
    assert mentions["messy"].grounding_status is GroundingStatus.GROUNDED
    assert mentions["别名"].grounding_status is GroundingStatus.GROUNDED


def test_grounder_same_mention_resolves_to_cached_entity() -> None:
    world = _entity_world()
    grounder = NeedDraftGrounder()
    grounded = grounder.ground(
        PlannedNeedDraft(
            draft_id="cached",
            semantic_question="question?",
            entity_mentions=(
                EntityMention(label="teacher", role_in_need="subject"),
                EntityMention(label="Teacher", role_in_need="subject"),
            ),
        ),
        world,
    )
    mentions = tuple(item.entity_id for item in grounded.entity_mentions)
    assert mentions[0] == mentions[1]
    assert mentions[0] is not None


def test_grounder_relation_context_requires_resolved_other_endpoint() -> None:
    world = _entity_world()
    teacher = world.entities[0]
    shared_a = teacher.model_copy(
        update={
            "entity_id": StableId("entity.planner.shared-c"),
            "internal_label": "shared",
            "aliases": (),
        }
    )
    shared_b = shared_a.model_copy(update={"entity_id": StableId("entity.planner.shared-d")})
    world = world.model_copy(update={"entities": (*world.entities, shared_a, shared_b)})
    grounder = NeedDraftGrounder()

    missing_other = grounder.ground(
        PlannedNeedDraft(
            draft_id="rel-missing-other",
            semantic_question="question?",
            entity_mentions=(EntityMention(label="shared", role_in_need="subject"),),
            relation_mentions=(
                RelationMention(
                    subject_label="shared",
                    relation_label="teaches",
                    object_label="ghost",
                ),
            ),
        ),
        world,
    )
    shared_mention = next(
        item for item in missing_other.entity_mentions if item.mention == "shared"
    )
    assert shared_mention.grounding_status is GroundingStatus.AMBIGUOUS

    unresolved_other = grounder.ground(
        PlannedNeedDraft(
            draft_id="rel-unresolved-other",
            semantic_question="question?",
            entity_mentions=(
                EntityMention(label="shared", role_in_need="subject"),
                EntityMention(label="ghost", role_in_need="object"),
            ),
            relation_mentions=(
                RelationMention(
                    subject_label="shared",
                    relation_label="teaches",
                    object_label="ghost",
                ),
            ),
        ),
        world,
    )
    shared_mention = next(
        item for item in unresolved_other.entity_mentions if item.mention == "shared"
    )
    assert shared_mention.grounding_status is GroundingStatus.AMBIGUOUS

    unrelated_relation = grounder.ground(
        PlannedNeedDraft(
            draft_id="rel-unrelated",
            semantic_question="question?",
            entity_mentions=(EntityMention(label="shared", role_in_need="subject"),),
            relation_mentions=(
                RelationMention(
                    subject_label="teacher",
                    relation_label="teaches",
                    object_label="student",
                ),
            ),
        ),
        world,
    )
    shared_mention = next(
        item for item in unrelated_relation.entity_mentions if item.mention == "shared"
    )
    assert shared_mention.grounding_status is GroundingStatus.AMBIGUOUS

    unmatched_relation = grounder.ground(
        PlannedNeedDraft(
            draft_id="rel-unmatched",
            semantic_question="question?",
            entity_mentions=(
                EntityMention(label="shared", role_in_need="subject"),
                EntityMention(label="student", role_in_need="object"),
            ),
            relation_mentions=(
                RelationMention(
                    subject_label="shared",
                    relation_label="teaches",
                    object_label="student",
                ),
            ),
        ),
        world,
    )
    shared_mention = next(
        item for item in unmatched_relation.entity_mentions if item.mention == "shared"
    )
    assert shared_mention.grounding_status is GroundingStatus.AMBIGUOUS
    assert shared_mention.grounding_method == "ambiguous_label_match"


def test_grounder_shared_label_resolution_fails_closed_even_with_resolved_other() -> None:
    world = _entity_world()
    teacher = world.entities[0]
    student = next(entity for entity in world.entities if entity.internal_label == "student")
    shared_a = teacher.model_copy(
        update={
            "entity_id": StableId("entity.planner.shared-e"),
            "internal_label": "shared",
            "aliases": (),
        }
    )
    shared_b = shared_a.model_copy(update={"entity_id": StableId("entity.planner.shared-f")})
    world = world.model_copy(
        update={
            "entities": (*world.entities, shared_a, shared_b),
            "relations": (
                RelationRecord(
                    relation_id=StableId("relation.planner.shared-teaches-b"),
                    predicate="teaches",
                    subject_id=shared_a.entity_id,
                    object_id=student.entity_id,
                    valid_time=StoryTime(worldline="main"),
                    truth_class=TruthClass.ACCEPTED_WORLD_FACT,
                ),
            ),
        }
    )
    grounder = NeedDraftGrounder()
    grounded = grounder.ground(
        PlannedNeedDraft(
            draft_id="shared-resolved-second",
            semantic_question="question?",
            entity_mentions=(
                EntityMention(label="student", role_in_need="object"),
                EntityMention(label="shared", role_in_need="subject"),
            ),
            relation_mentions=(
                RelationMention(
                    subject_label="shared",
                    relation_label="teaches",
                    object_label="student",
                ),
            ),
        ),
        world,
    )
    shared_mention = next(item for item in grounded.entity_mentions if item.mention == "shared")
    assert shared_mention.grounding_status is GroundingStatus.AMBIGUOUS
    assert shared_mention.entity_id is None


def test_planner_json_extraction_edge_paths() -> None:
    with pytest.raises(ValueError, match="no JSON object"):
        PlanConditionedNeedPlanner._extract_json_payload("no braces here")
    with pytest.raises(ValueError, match="unterminated"):
        PlanConditionedNeedPlanner._extract_json_payload('{"drafts": [')
    assert PlanConditionedNeedPlanner.sanitize_draft_id("") == "draft"
    with pytest.raises(ValueError, match="must be an object"):
        PlanConditionedNeedPlanner._parse_drafts('{"drafts": ["not-a-dict"]}')


def test_planner_falls_back_on_endpoint_error_and_empty_drafts() -> None:
    task = _task()
    world = _entity_world()
    endpoint = _PlannerEndpoint((RuntimeError("endpoint down"), RuntimeError("endpoint down")))
    gateway = _gateway(endpoint)
    planner = PlanConditionedNeedPlanner(gateway=gateway)
    result = planner.plan(task=task, world=world, planning_context=_planner_context(task))
    assert result.fallback_status is PlannerFallbackStatus.PLANNER_FALLBACK
    assert result.error_category == "RuntimeError"

    endpoint = _PlannerEndpoint(({"drafts": []}, {"drafts": []}))
    gateway = _gateway(endpoint)
    planner = PlanConditionedNeedPlanner(gateway=gateway)
    result = planner.plan(task=task, world=world, planning_context=_planner_context(task))
    assert result.fallback_status is PlannerFallbackStatus.PLANNER_FALLBACK
    assert result.error_category == "empty_drafts"


def test_world_summary_event_chapter_falls_back_to_evidence_refs() -> None:
    from novel_agent.domain.ids import CommitId
    from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, TextSpanRef
    from novel_agent.domain.world import Event
    from novel_agent.services.benchmark_importer import quote_hash

    world = _entity_world()
    teacher = world.entities[0]
    student = next(entity for entity in world.entities if entity.internal_label == "student")

    def evidence(chapter_id: str) -> EvidenceRef:
        return EvidenceRef(
            evidence_id=StableId(f"evidence.chapter.{chapter_id}"),
            root_hash=ArtifactId("sha256:" + "e" * 64),
            object_hash=ArtifactId("sha256:" + "e" * 64),
            chapter_id=StableId(chapter_id),
            scene_id=StableId("scene.evidence.1"),
            span=TextSpanRef(
                block_id=StableId("block.evidence.1"),
                start=0,
                end=1,
            ),
            quote_hash=quote_hash("phrase"),
            support_status=EvidenceSupportStatus.CURRENT,
            resolved_at_commit=CommitId("sha256:" + "e" * 64),
        )

    plain_event = Event(
        event_id=StableId("event.planner.plain"),
        event_type="plain_event",
        participant_ids=(teacher.entity_id, student.entity_id),
        narrative_order=None,
        evidence_refs=(
            evidence("chapter.evidence.12"),
            evidence("chapter.unnumbered"),
        ),
        truth_class=TruthClass.ACCEPTED_WORLD_FACT,
    )
    world = world.model_copy(update={"events": (*world.events, plain_event)})
    task = _task()
    summary = PlannerWorldSummaryBuilder.build(task, world, _planner_context(task))
    plain = next(event for event in summary.recent_events if event.event_type == "plain_event")
    assert plain.chapter == 12


def test_planner_rejects_invalid_constructor_limits() -> None:
    with pytest.raises(ValueError, match="max drafts"):
        PlanConditionedNeedPlanner(max_drafts=0)
    with pytest.raises(ValueError, match="retries"):
        PlanConditionedNeedPlanner(max_retries=3)
    with pytest.raises(ValueError, match="temperature"):
        PlanConditionedNeedPlanner(temperature=3.0)
    with pytest.raises(ValueError, match="max_total_needs"):
        NeedValidator(max_total_needs=0)


def test_validator_canonicalizes_mismatched_scope_without_dropping_draft() -> None:
    task = _task()
    world = _entity_world()
    validator = NeedValidator()
    draft = _draft(
        "multi-facet",
        "在截止点前 teacher 的伤势与未决承诺的状态是什么?",
        facets=("CAUSAL_HISTORY", "UNRESOLVED_STATUS"),
        chapters=(21,),
    )
    draft = draft.model_copy(update={"required_claim_scopes": ("historical",)})
    grounded = NeedDraftGrounder().ground(draft, world)
    focus_set = TaskFocusExtractor().extract(task, world)
    result = validator.validate(
        drafts=(grounded,),
        task=task,
        world=world,
        focus_set=focus_set,
        plan=make_synthetic_bundle().plan_roots[0],
    )
    assert [item.draft_id for item in result.accepted_drafts] == ["multi-facet"]
    assert result.scope_normalization_reasons["multi-facet"] == (
        "mismatched_scope_canonicalized_from_facets"
    )
    canonical = result.canonical_scope_by_draft["multi-facet"]
    assert set(canonical) == {"historical", "current"}


def test_validator_canonicalizes_missing_scope_without_dropping_draft() -> None:
    task = _task()
    world = _entity_world()
    validator = NeedValidator()
    draft = _draft(
        "no-scope",
        "在截止点前 teacher 的知情边界是什么?",
        facets=("KNOWLEDGE_BOUNDARY", "CURRENT_STATE"),
        chapters=(21,),
    )
    draft = draft.model_copy(update={"required_claim_scopes": ()})
    grounded = NeedDraftGrounder().ground(draft, world)
    focus_set = TaskFocusExtractor().extract(task, world)
    result = validator.validate(
        drafts=(grounded,),
        task=task,
        world=world,
        focus_set=focus_set,
        plan=make_synthetic_bundle().plan_roots[0],
    )
    assert [item.draft_id for item in result.accepted_drafts] == ["no-scope"]
    assert result.scope_normalization_reasons["no-scope"] == (
        "missing_scope_canonicalized_from_facets"
    )
    assert set(result.canonical_scope_by_draft["no-scope"]) == {"knowledge", "current"}


def test_validator_still_rejects_plan_scope_and_unknown_values() -> None:
    task = _task()
    world = _entity_world()
    validator = NeedValidator()
    draft_plan = _draft("plan-scope", "q?", facets=("PLAN_NODE",), chapters=(21,))
    draft_unknown = _draft(
        "unknown-scope", "q?", facets=("CURRENT_STATE",), chapters=(21,)
    ).model_copy(update={"required_claim_scopes": ("future-line",)})
    grounded = tuple(
        NeedDraftGrounder().ground(draft, world) for draft in (draft_plan, draft_unknown)
    )
    focus_set = TaskFocusExtractor().extract(task, world)
    result = validator.validate(
        drafts=grounded,
        task=task,
        world=world,
        focus_set=focus_set,
        plan=make_synthetic_bundle().plan_roots[0],
    )
    assert not result.accepted_drafts
    assert result.rejected_reasons["plan-scope"] == "plan_scope_not_historical_memory"
    assert result.rejected_reasons["unknown-scope"] == "unknown_or_empty_scope_facet"


def test_partial_goal_coverage_triggers_full_fallback_with_typed_missing_goals() -> None:
    partial_payload = {
        "drafts": [
            {
                "draft_id": "only-21",
                "semantic_question": "在截止点前 teacher 的伤势是否仍未痊愈?",
                "entity_mentions": [{"label": "teacher", "role_in_need": "subject"}],
                "relation_mentions": [],
                "trigger_plan_chapters": [21],
                "trigger_plan_goal": "重申旧誓言",
                "why_needed": "plan the memory reload",
                "required_claim_scopes": ["current"],
                "suggested_facets": ["CURRENT_STATE"],
                "historical_time_scope": "main",
                "query_hints": ["teacher 伤势"],
            }
        ],
        "meta": {"rationale": "partial"},
    }
    endpoint = _PlannerEndpoint((partial_payload, partial_payload))
    gateway = _gateway(endpoint)
    task = _task()
    world = _entity_world()
    plan = make_synthetic_bundle().plan_roots[0]
    context = _planner_context(task)
    generator = TaskPlanConditionedNeedGenerator(planner_gateway=gateway)
    result = generator.generate_with_lineage(task, world, plan, context)
    assert result.status is NeedGenerationStatus.PLANNER_FALLBACK
    assert result.fallback_used is True
    assert result.planner_fallback_reason == "insufficient_target_goal_coverage"
    assert result.planner_artifact is not None
    assert result.planner_artifact.fallback_status is PlannerFallbackStatus.PLANNER_FALLBACK
    assert result.planner_artifact.fallback_reason == "insufficient_target_goal_coverage"
    assert set(result.planner_artifact.missing_goal_chapters) == {22, 23}


def test_full_goal_coverage_does_not_fallback() -> None:
    endpoint = _PlannerEndpoint((_planner_payload(),))
    gateway = _gateway(endpoint)
    task = _task()
    world = _entity_world()
    plan = make_synthetic_bundle().plan_roots[0]
    context = _planner_context(task)
    generator = TaskPlanConditionedNeedGenerator(planner_gateway=gateway)
    result = generator.generate_with_lineage(task, world, plan, context)
    assert result.status is NeedGenerationStatus.READY
    assert result.fallback_used is False
    assert result.planner_artifact is not None
    assert not result.planner_artifact.missing_goal_chapters


def test_planner_coverage_audit_is_bounded_and_rejects_unknown_world_labels() -> None:
    audit_payload = {
        "findings": [
            {
                "target_chapter": 21,
                "category": "KEY_CHARACTER",
                "canonical_entity_labels": ["not-in-world"],
                "missing_historical_question": "unknown anchor",
                "reason": "must not enter repair instructions",
            }
        ]
    }
    endpoint = _PlannerEndpoint((_planner_payload(), audit_payload))
    generator = TaskPlanConditionedNeedGenerator(
        planner_gateway=_gateway(endpoint),
        planner_coverage_audit=True,
    )
    task = _task()
    result = generator.generate_with_lineage(
        task,
        _entity_world(),
        make_synthetic_bundle().plan_roots[0],
        _planner_context(task),
    )
    assert result.status is NeedGenerationStatus.READY
    assert result.planner_artifact is not None
    assert len(result.planner_artifact.coverage_audits) == 1
    assert result.planner_artifact.coverage_audits[0].findings == ()
    assert len(endpoint.requests) == 2
    audit_prompt = endpoint.requests[1].prompt
    assert "未来正文" in audit_prompt
    assert "not-in-world" not in audit_prompt


def test_normal_planner_needs_are_mandatory() -> None:
    endpoint = _PlannerEndpoint((_planner_payload(),))
    gateway = _gateway(endpoint)
    task = _task()
    world = _entity_world()
    plan = make_synthetic_bundle().plan_roots[0]
    context = _planner_context(task)
    generator = TaskPlanConditionedNeedGenerator(planner_gateway=gateway)
    result = generator.generate_with_lineage(task, world, plan, context)
    assert result.needs
    for need in result.needs:
        assert need.requirement.value == "mandatory"
        assert need.completion_spec is not None
        assert need.completion_spec.irreducible_need_facet_ids == (
            need.completion_spec.required_need_facet_ids
        )


def test_model_planner_keeps_more_than_thirty_two_valid_needs_without_implicit_cap() -> None:
    payload = _planner_payload()
    drafts = list(cast(list[dict[str, object]], payload["drafts"]))
    for index in range(33):
        drafts.append(
            {
                "draft_id": f"capacity-{index}",
                "semantic_question": f"在截止点前 teacher 的历史状态条目 {index} 是什么?",
                "entity_mentions": [{"label": "teacher", "role_in_need": "subject"}],
                "relation_mentions": [],
                "trigger_plan_chapters": [21],
                "trigger_plan_goal": "重申旧誓言",
                "why_needed": "capacity regression",
                "required_claim_scopes": ["current"],
                "suggested_facets": ["CURRENT_STATE"],
                "historical_time_scope": "main",
                "query_hints": [],
            }
        )
    endpoint = _PlannerEndpoint((payload | {"drafts": drafts},))
    task = _task()
    result = TaskPlanConditionedNeedGenerator(
        planner_gateway=_gateway(endpoint)
    ).generate_with_lineage(
        task,
        _entity_world(),
        make_synthetic_bundle().plan_roots[0],
        _planner_context(task),
    )
    assert result.status is NeedGenerationStatus.READY
    assert len(result.needs) > 32


def _chinese_world() -> WorldRootDocument:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    chen = world.entities[0].model_copy(
        update={
            "entity_id": StableId("entity.planner.chen"),
            "internal_label": "陈长生",
            "aliases": ("长生",),
        }
    )
    luo = chen.model_copy(
        update={
            "entity_id": StableId("entity.planner.luo"),
            "internal_label": "落落",
            "aliases": (),
        }
    )
    hei = chen.model_copy(
        update={
            "entity_id": StableId("entity.planner.hei"),
            "internal_label": "黑龙",
            "aliases": (),
        }
    )
    return world.model_copy(
        update={
            "entities": (chen, *world.entities[1:], luo, hei),
        }
    )


def test_grounder_closure_binds_entities_from_question_text() -> None:
    """P004-like: question names both entities but explicit mentions only one."""
    world = _chinese_world()
    draft = PlannedNeedDraft(
        draft_id="closure-p004",
        semantic_question="陈长生对落落的教学方式是什么?",
        entity_mentions=(EntityMention(label="陈长生", role_in_need="subject"),),
        trigger_plan_chapters=(81,),
        trigger_plan_goal="展开师徒教学",
        why_needed="决定第81章教学方式",
        required_claim_scopes=("current",),
        suggested_facets=("RELATION_STATE",),
        historical_time_scope="main",
        query_hints=("陈长生如何教导落落",),
    )
    grounded = NeedDraftGrounder().ground(draft, world)
    ids = set(NeedDraftGrounder().grounded_entity_ids(grounded))
    assert StableId("entity.planner.chen") in ids
    assert StableId("entity.planner.luo") in ids
    sources = {item.mention: item.mention_source for item in grounded.entity_mentions}
    assert sources["落落"] == "exact_text_mention_closure"


def test_grounder_closure_binds_fallback_goal_entities() -> None:
    """P003-like: fallback goal text names both entities; entity_ids was empty."""
    world = _chinese_world()
    draft = PlannedNeedDraft(
        draft_id="closure-p003",
        semantic_question="黑龙与陈长生的关系现状是什么?",
        entity_mentions=(),
        trigger_plan_chapters=(61,),
        trigger_plan_goal="黑龙与陈长生对峙",
        why_needed="第61章需要恢复双方关系",
        required_claim_scopes=("historical",),
        suggested_facets=("CAUSAL_HISTORY",),
        historical_time_scope="main",
        query_hints=(),
    )
    grounded = NeedDraftGrounder().ground(draft, world)
    ids = set(NeedDraftGrounder().grounded_entity_ids(grounded))
    assert StableId("entity.planner.chen") in ids
    assert StableId("entity.planner.hei") in ids


def test_grounder_closure_uses_longest_label_first() -> None:
    world = _chinese_world()
    draft = PlannedNeedDraft(
        draft_id="closure-longest",
        semantic_question="长生与落落的关系如何?",
        entity_mentions=(),
        trigger_plan_chapters=(81,),
        trigger_plan_goal="师徒重逢",
        why_needed="need",
        required_claim_scopes=("current",),
        suggested_facets=("RELATION_STATE",),
        historical_time_scope="main",
        query_hints=(),
    )
    grounded = NeedDraftGrounder().ground(draft, world)
    ids = set(NeedDraftGrounder().grounded_entity_ids(grounded))
    assert StableId("entity.planner.chen") in ids
    assert StableId("entity.planner.luo") in ids


def test_grounder_closure_skips_ambiguous_and_absent_labels() -> None:
    bundle = make_synthetic_bundle()
    world = bundle.world_roots[0]
    twin_a = world.entities[0].model_copy(
        update={"entity_id": StableId("entity.planner.twin-a"), "internal_label": "同名人"}
    )
    twin_b = world.entities[0].model_copy(
        update={"entity_id": StableId("entity.planner.twin-b"), "internal_label": "同名人"}
    )
    world = world.model_copy(update={"entities": (*world.entities, twin_a, twin_b)})
    draft = PlannedNeedDraft(
        draft_id="closure-ambiguous",
        semantic_question="同名人 与 不存在之人 的关系如何?",
        entity_mentions=(),
        trigger_plan_chapters=(81,),
        trigger_plan_goal="test",
        why_needed="need",
        required_claim_scopes=("current",),
        suggested_facets=("RELATION_STATE",),
        historical_time_scope="main",
        query_hints=(),
    )
    grounded = NeedDraftGrounder().ground(draft, world)
    ids = set(NeedDraftGrounder().grounded_entity_ids(grounded))
    assert ids == set()


def test_grounder_closure_records_source_fields() -> None:
    world = _chinese_world()
    draft = PlannedNeedDraft(
        draft_id="closure-fields",
        semantic_question="落落当前状态如何?",
        entity_mentions=(),
        trigger_plan_chapters=(81,),
        trigger_plan_goal="test",
        why_needed="need",
        required_claim_scopes=("current",),
        suggested_facets=("CURRENT_STATE",),
        historical_time_scope="main",
        query_hints=(),
    )
    grounded = NeedDraftGrounder().ground(draft, world)
    luo = next(
        item
        for item in grounded.entity_mentions
        if item.entity_id == StableId("entity.planner.luo")
    )
    assert luo.mention_source == "exact_text_mention_closure"
    assert "semantic_question" in luo.mention_source_fields


def test_goal_entity_coverage_repair_keeps_normal_path() -> None:
    """A draft naming both goal entities in text is accepted without fallback."""
    world = _chinese_world()
    plan = _chinese_goals_plan(world)
    context = AuthorPlanningContext(
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        task_intent="为写 81-85 章准备历史记忆",
        target_range=(81, 85),
        visible_outline_nodes=(),
        chapter_goals=plan.chapter_goals,
        source_hash=content_id({"goals": "chinese-test"}),
        planner_may_read_plan=True,
    )
    task = _task(task_intent="为写 81-85 章准备历史记忆")
    task = task.model_copy(
        update={
            "target_chapter_start": 81,
            "target_chapter_end": 85,
        }
    )
    endpoint = _PlannerEndpoint(
        (
            {
                "drafts": [
                    {
                        "draft_id": "d81-01",
                        "semantic_question": "陈长生对落落的教学方式是什么?",
                        "entity_mentions": [{"label": "陈长生", "role_in_need": "subject"}],
                        "relation_mentions": [],
                        "trigger_plan_chapters": [81],
                        "trigger_plan_goal": "师徒教学与感情进展",
                        "why_needed": "决定第81章教学方式",
                        "required_claim_scopes": ["current"],
                        "suggested_facets": ["RELATION_STATE"],
                        "historical_time_scope": "main",
                        "query_hints": ["陈长生如何教导落落"],
                    },
                    {
                        "draft_id": "d82-01",
                        "semantic_question": "落落当前对陈长生的态度是什么?",
                        "entity_mentions": [{"label": "落落", "role_in_need": "subject"}],
                        "relation_mentions": [],
                        "trigger_plan_chapters": [82],
                        "trigger_plan_goal": "感情升温",
                        "why_needed": "第82章感情线",
                        "required_claim_scopes": ["current"],
                        "suggested_facets": ["RELATION_STATE"],
                        "historical_time_scope": "main",
                        "query_hints": [],
                    },
                    {
                        "draft_id": "d83-01",
                        "semantic_question": "落落修行进展如何?",
                        "entity_mentions": [{"label": "落落", "role_in_need": "subject"}],
                        "relation_mentions": [],
                        "trigger_plan_chapters": [83],
                        "trigger_plan_goal": "修行突破",
                        "why_needed": "第83章修行",
                        "required_claim_scopes": ["current"],
                        "suggested_facets": ["CURRENT_STATE"],
                        "historical_time_scope": "main",
                        "query_hints": [],
                    },
                    {
                        "draft_id": "d84-01",
                        "semantic_question": "陈长生伤势当前状态如何?",
                        "entity_mentions": [{"label": "陈长生", "role_in_need": "subject"}],
                        "relation_mentions": [],
                        "trigger_plan_chapters": [84],
                        "trigger_plan_goal": "伤势稳定",
                        "why_needed": "第84章伤势",
                        "required_claim_scopes": ["current"],
                        "suggested_facets": ["CURRENT_STATE"],
                        "historical_time_scope": "main",
                        "query_hints": [],
                    },
                    {
                        "draft_id": "d85-01",
                        "semantic_question": "黑龙与陈长生的恩怨如何?",
                        "entity_mentions": [{"label": "陈长生", "role_in_need": "subject"}],
                        "relation_mentions": [],
                        "trigger_plan_chapters": [85],
                        "trigger_plan_goal": "黑龙与陈长生恩怨了结",
                        "why_needed": "第85章恩怨",
                        "required_claim_scopes": ["historical"],
                        "suggested_facets": ["CAUSAL_HISTORY"],
                        "historical_time_scope": "main",
                        "query_hints": [],
                    },
                ],
                "meta": {"rationale": "test"},
            },
        )
    )
    gateway = _gateway(endpoint)
    generator = TaskPlanConditionedNeedGenerator(planner_gateway=gateway)
    result = generator.generate_with_lineage(task, world, plan, context)
    assert result.status is NeedGenerationStatus.READY
    assert result.fallback_used is False
    assert result.planner_artifact is not None
    assert not result.planner_artifact.missing_goal_entities
    needs = result.needs
    d81 = next(item for item in needs if item.planned_draft_id == "d81-01")
    assert StableId("entity.planner.luo") in d81.entity_ids
    assert StableId("entity.planner.chen") in d81.entity_ids


def _chinese_goals_plan(world: WorldRootDocument) -> PlanRootDocument:
    from novel_agent.domain.benchmark import ChapterGoal
    from novel_agent.services.benchmark_importer import plan_root_content_id

    bundle = make_synthetic_bundle()
    plan = bundle.plan_roots[0]
    goals = tuple(
        ChapterGoal(
            goal_id=StableId(f"goal.chinese.{index}"),
            chapter_index=index,
            summary=summary,
        )
        for index, summary in (
            (81, "师徒教学与感情进展"),
            (82, "感情升温"),
            (83, "修行突破"),
            (84, "伤势稳定"),
            (85, "黑龙与陈长生恩怨了结"),
        )
    )
    plan = plan.model_copy(update={"chapter_goals": goals})
    return plan.model_copy(update={"root_hash": plan_root_content_id(plan)})


def test_goal_entity_missing_triggers_whole_fallback() -> None:
    """A target goal naming an entity absent from all accepted Needs falls back."""
    world = _chinese_world()
    plan = _chinese_goals_plan(world)
    context = AuthorPlanningContext(
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        task_intent="为写 81-85 章准备历史记忆",
        target_range=(81, 85),
        visible_outline_nodes=(),
        chapter_goals=plan.chapter_goals,
        source_hash=content_id({"goals": "chinese-fallback"}),
        planner_may_read_plan=True,
    )
    task = _task(task_intent="为写 81-85 章准备历史记忆")
    task = task.model_copy(update={"target_chapter_start": 81, "target_chapter_end": 85})
    # Only ch81 draft; ch82 goal names 落落 but the draft for 82 is missing.
    partial_payload = {
        "drafts": [
            {
                "draft_id": "d81-only",
                "semantic_question": "陈长生对落落的教学方式是什么?",
                "entity_mentions": [{"label": "陈长生", "role_in_need": "subject"}],
                "relation_mentions": [],
                "trigger_plan_chapters": [81],
                "trigger_plan_goal": "师徒教学与感情进展",
                "why_needed": "决定第81章教学方式",
                "required_claim_scopes": ["current"],
                "suggested_facets": ["RELATION_STATE"],
                "historical_time_scope": "main",
                "query_hints": [],
            }
        ],
        "meta": {"rationale": "partial"},
    }
    endpoint = _PlannerEndpoint((partial_payload, partial_payload))
    gateway = _gateway(endpoint)
    generator = TaskPlanConditionedNeedGenerator(planner_gateway=gateway)
    result = generator.generate_with_lineage(task, world, plan, context)
    assert result.status is NeedGenerationStatus.PLANNER_FALLBACK
    assert result.fallback_used is True
    assert result.planner_artifact is not None
    assert result.planner_artifact.fallback_reason == "insufficient_target_goal_coverage"


def test_query_compiler_keeps_natural_query_and_exact_filters() -> None:
    """Closed entity_ids enter exact filters while the natural question stays."""
    world = _chinese_world()
    draft = PlannedNeedDraft(
        draft_id="closure-query",
        semantic_question="陈长生对落落的教导方式是什么?",
        entity_mentions=(EntityMention(label="陈长生", role_in_need="subject"),),
        trigger_plan_chapters=(81,),
        trigger_plan_goal="师徒教学",
        why_needed="need",
        required_claim_scopes=("current",),
        suggested_facets=("RELATION_STATE",),
        historical_time_scope="main",
        query_hints=("陈长生如何教导落落",),
    )
    grounded = NeedDraftGrounder().ground(draft, world)
    entity_ids = NeedDraftGrounder().grounded_entity_ids(grounded)
    assert StableId("entity.planner.luo") in entity_ids
    compiler = NeedQueryCompiler()
    need = Stage1MemoryNeed(
        need_id=StableId("need.stage2m.closure-test.state"),
        run_id=RunId("run.test"),
        task_id=TaskId("task.test"),
        base_commit=world.source_commit,
        chapter_target=81,
        need_type="relationship_emotion",
        query_intent=Stage1QueryIntent.SEMANTIC_HISTORY,
        query_text="陈长生对落落的教导方式是什么?",
        entity_ids=entity_ids,
        why_needed="need",
        risk_level=NeedRisk.HIGH,
        requirement=RequirementLevel.MANDATORY,
        preferred_resolution_path=ResolutionPath.ANCHOR_FIRST,
        allowed_candidate_pools=(CandidatePool.ANCHOR, CandidatePool.R1),
        stop_condition="done",
        trigger_plan_chapters=(81,),
    )
    bundle = compiler.compile(need)
    assert StableId("entity.planner.luo") in bundle.exact_entity_ids
    assert StableId("entity.planner.chen") in bundle.exact_entity_ids
    assert any("陈长生" in query for query in bundle.lexical_queries)
    assert any("落落" in query for query in bundle.lexical_queries)


def test_missing_goal_entities_detects_uncovered_unique_entity() -> None:
    """Direct unit test of the goal/entity coverage postcondition."""
    world = _chinese_world()
    plan = _chinese_goals_plan(world)
    context = AuthorPlanningContext(
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        task_intent="为写 81-85 章准备历史记忆",
        target_range=(81, 85),
        visible_outline_nodes=(),
        chapter_goals=plan.chapter_goals,
        source_hash=content_id({"goals": "chinese-entity-unit"}),
        planner_may_read_plan=True,
    )
    from novel_agent.domain.planning_memory import GroundedEntityMention, GroundedNeedDraft

    chen = GroundedEntityMention(
        mention="陈长生",
        canonical_label="陈长生",
        entity_id=StableId("entity.planner.chen"),
        confidence=1.0,
        grounding_method="exact_label_match",
        grounding_status=GroundingStatus.GROUNDED,
    )
    covered_draft = GroundedNeedDraft(
        draft_id="d85-covered",
        semantic_question="黑龙与陈长生恩怨如何?",
        entity_mentions=(chen,),
        trigger_plan_chapters=(85,),
        trigger_plan_goal="黑龙与陈长生恩怨了结",
        why_needed="need",
        required_claim_scopes=("historical",),
        suggested_facets=("CAUSAL_HISTORY",),
        historical_time_scope="main",
    )
    generator = TaskPlanConditionedNeedGenerator()
    missing = generator._missing_goal_entities(
        context=context,
        world=world,
        accepted=(covered_draft,),
        target_start=81,
        target_end=85,
    )
    # ch85 goal names 黑龙 which is uniquely groundable and absent from the
    # covered draft -> reported as missing.
    assert any(label == "黑龙" for _chapter, _entity, label in missing)


def test_missing_goal_entities_ignores_goals_outside_target() -> None:
    """Goals outside the target range are not subject to entity coverage."""
    world = _chinese_world()
    plan = _chinese_goals_plan(world)
    context = AuthorPlanningContext(
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        task_intent="为写 81-85 章准备历史记忆",
        target_range=(81, 85),
        visible_outline_nodes=(),
        chapter_goals=plan.chapter_goals,
        source_hash=content_id({"goals": "chinese-outside"}),
        planner_may_read_plan=True,
    )
    from novel_agent.domain.planning_memory import GroundedEntityMention, GroundedNeedDraft

    chen = GroundedEntityMention(
        mention="陈长生",
        canonical_label="陈长生",
        entity_id=StableId("entity.planner.chen"),
        confidence=1.0,
        grounding_method="exact_label_match",
        grounding_status=GroundingStatus.GROUNDED,
    )
    empty_draft = GroundedNeedDraft(
        draft_id="d81-empty",
        semantic_question="陈长生状态如何?",
        entity_mentions=(chen,),
        trigger_plan_chapters=(81,),
        trigger_plan_goal="师徒教学与感情进展",
        why_needed="need",
        required_claim_scopes=("current",),
        suggested_facets=("CURRENT_STATE",),
        historical_time_scope="main",
    )
    generator = TaskPlanConditionedNeedGenerator()
    # context.target_range is 81-85 but the task passed below uses 21-23, so
    # no goal falls in range -> no missing entities.
    missing = generator._missing_goal_entities(
        context=context,
        world=world,
        accepted=(empty_draft,),
        target_start=21,
        target_end=23,
    )
    assert missing == ()


def test_unique_entities_in_text_handles_overlap_and_absent() -> None:
    """Longest-first closure skips overlapping shorter labels and absent text."""
    world = _chinese_world()
    from novel_agent.services.need_draft_grounder import NeedDraftGrounder

    unique = NeedDraftGrounder._world_label_map(world)
    generator = TaskPlanConditionedNeedGenerator()
    # "长生" overlaps "陈长生" when both appear; longest-first keeps 陈长生.
    found = generator._unique_entities_in_text("长生与陈长生", unique)
    labels = {label for label, _entity in found}
    assert "陈长生" in labels
    found_absent = generator._unique_entities_in_text("无关文本", unique)
    assert found_absent == ()


def test_entity_coverage_fallback_branch_via_monkeypatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive coverage: force the entity-coverage fallback branch in
    ``_run_planner_chain`` by making the goal-entity check report a miss."""
    world = _chinese_world()
    plan = _chinese_goals_plan(world)
    context = AuthorPlanningContext(
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        task_intent="为写 81-85 章准备历史记忆",
        target_range=(81, 85),
        visible_outline_nodes=(),
        chapter_goals=plan.chapter_goals,
        source_hash=content_id({"goals": "chinese-mock"}),
        planner_may_read_plan=True,
    )
    task = _task(task_intent="为写 81-85 章准备历史记忆")
    task = task.model_copy(update={"target_chapter_start": 81, "target_chapter_end": 85})
    endpoint = _PlannerEndpoint((_planner_payload_chinese(),))
    gateway = _gateway(endpoint)
    generator = TaskPlanConditionedNeedGenerator(planner_gateway=gateway)
    monkeypatch.setattr(
        generator,
        "_missing_goal_entities",
        lambda **kwargs: ((85, StableId("entity.planner.chen"), "陈长生"),),
    )
    result = generator.generate_with_lineage(task, world, plan, context)
    assert result.status is NeedGenerationStatus.PLANNER_FALLBACK
    assert result.fallback_used is True
    assert result.planner_artifact is not None
    assert result.planner_artifact.fallback_reason == ("insufficient_target_goal_entity_coverage")
    assert result.planner_artifact.missing_goal_entities


def _planner_payload_chinese() -> dict[str, object]:
    return {
        "drafts": [
            {
                "draft_id": f"d{ch}-01",
                "semantic_question": f"第{ch}章 陈长生 的状态?",
                "entity_mentions": [{"label": "陈长生", "role_in_need": "subject"}],
                "relation_mentions": [],
                "trigger_plan_chapters": [ch],
                "trigger_plan_goal": goal,
                "why_needed": f"need for ch{ch}",
                "required_claim_scopes": ["current"],
                "suggested_facets": ["CURRENT_STATE"],
                "historical_time_scope": "main",
                "query_hints": [],
            }
            for ch, goal in (
                (81, "师徒教学与感情进展"),
                (82, "感情升温"),
                (83, "修行突破"),
                (84, "伤势稳定"),
                (85, "黑龙与陈长生恩怨了结"),
            )
        ],
        "meta": {"rationale": "mock entity fallback"},
    }


def test_fallback_path_closure_binds_goal_entities() -> None:
    """The deterministic fallback adds entities that appear in the plan text."""
    world = _chinese_world()
    plan = _chinese_goals_plan(world)
    context = AuthorPlanningContext(
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        task_intent="为写 81-85 章准备历史记忆",
        target_range=(81, 85),
        visible_outline_nodes=(),
        chapter_goals=plan.chapter_goals,
        source_hash=content_id({"goals": "chinese-fallback-closure"}),
        planner_may_read_plan=True,
    )
    task = _task(task_intent="为写 81-85 章准备历史记忆")
    task = task.model_copy(update={"target_chapter_start": 81, "target_chapter_end": 85})
    # Empty drafts -> whole fallback; the fallback Needs must still carry the
    # entities named in their query/plan text.
    endpoint = _PlannerEndpoint(({"drafts": [], "meta": {"rationale": "empty"}},))
    gateway = _gateway(endpoint)
    generator = TaskPlanConditionedNeedGenerator(planner_gateway=gateway)
    result = generator.generate_with_lineage(task, world, plan, context)
    assert result.status is NeedGenerationStatus.PLANNER_FALLBACK
    assert result.fallback_used is True
    # Fallback Needs exist and carry closed entities from their query text.
    assert result.needs
    chen_in = any(StableId("entity.planner.chen") in need.entity_ids for need in result.needs)
    assert chen_in


def test_closed_entity_ids_for_text_skips_ambiguous() -> None:
    world = _chinese_world()
    ids = TaskPlanConditionedNeedGenerator._closed_entity_ids_for_text(
        world, ("陈长生与黑龙的恩怨",)
    )
    assert StableId("entity.planner.chen") in ids
    assert StableId("entity.planner.hei") in ids
    ambiguous_world = world.model_copy(
        update={
            "entities": (
                *world.entities,
                world.entities[0].model_copy(
                    update={
                        "entity_id": StableId("entity.planner.dup-a"),
                        "internal_label": "重名者",
                    }
                ),
                world.entities[0].model_copy(
                    update={
                        "entity_id": StableId("entity.planner.dup-b"),
                        "internal_label": "重名者",
                    }
                ),
            )
        }
    )
    ids_ambiguous = TaskPlanConditionedNeedGenerator._closed_entity_ids_for_text(
        ambiguous_world, ("重名者的状态",)
    )
    assert StableId("entity.planner.dup-a") not in ids_ambiguous
    assert StableId("entity.planner.dup-b") not in ids_ambiguous


def test_grounder_internal_label_beats_same_named_alias() -> None:
    """P004-like: 落落 is the canonical internal label and another entity's alias."""
    world = _chinese_world()
    twin = world.entities[0].model_copy(
        update={
            "entity_id": StableId("entity.planner.luo-heng"),
            "internal_label": "落衡",
            "aliases": ("落落",),
        }
    )
    world = world.model_copy(update={"entities": (*world.entities, twin)})
    grounder = NeedDraftGrounder()
    grounded = grounder.ground(
        PlannedNeedDraft(
            draft_id="p004-luo-luo",
            semantic_question="落落当前的状态与教学关系是什么?",
            entity_mentions=(EntityMention(label="落落", role_in_need="subject"),),
        ),
        world,
    )
    mention = grounded.entity_mentions[0]
    assert mention.grounding_status is GroundingStatus.GROUNDED
    assert mention.entity_id == StableId("entity.planner.luo")
    assert mention.grounding_method == "exact_internal_label_match"


def test_grounder_closure_resolves_internal_label_despite_alias_collision() -> None:
    """P004-like: closure binds the canonical internal label even when another
    entity carries the same label as an alias."""
    world = _chinese_world()
    twin = world.entities[0].model_copy(
        update={
            "entity_id": StableId("entity.planner.luo-heng"),
            "internal_label": "落衡",
            "aliases": ("落落",),
        }
    )
    world = world.model_copy(update={"entities": (*world.entities, twin)})
    grounded = NeedDraftGrounder().ground(
        PlannedNeedDraft(
            draft_id="closure-collision",
            semantic_question="落落与陈长生的师徒关系现状如何?",
            entity_mentions=(),
        ),
        world,
    )
    ids = {mention.entity_id for mention in grounded.entity_mentions}
    assert StableId("entity.planner.luo") in ids
    assert StableId("entity.planner.luo-heng") not in ids


def test_grounder_alias_exact_match_uses_dedicated_method() -> None:
    world = _chinese_world()
    grounded = NeedDraftGrounder().ground(
        PlannedNeedDraft(
            draft_id="alias-method",
            semantic_question="长生当前状态如何?",
            entity_mentions=(EntityMention(label="长生", role_in_need="subject"),),
        ),
        world,
    )
    mention = grounded.entity_mentions[0]
    assert mention.grounding_status is GroundingStatus.GROUNDED
    assert mention.entity_id == StableId("entity.planner.chen")
    assert mention.grounding_method == "exact_alias_match"


def test_validator_keeps_unresolved_lexical_anchor_drafts() -> None:
    """P005-like: an institution with no runtime id stays a lexical Need."""
    task = _task()
    world = _entity_world()
    draft = PlannedNeedDraft(
        draft_id="lexical-only",
        semantic_question="国教学院在京都政治格局中的定位是什么?",
        entity_mentions=(EntityMention(label="国教学院", role_in_need="institution"),),
        trigger_plan_chapters=(21,),
        trigger_plan_goal="重申旧誓言",
        why_needed="第21章需要学院定位",
        required_claim_scopes=("current",),
        suggested_facets=("CURRENT_STATE",),
        historical_time_scope="main",
        query_hints=("国教学院 政治 定位",),
    )
    grounded = NeedDraftGrounder().ground(draft, world)
    assert grounded.entity_mentions[0].grounding_status is GroundingStatus.UNRESOLVED
    focus_set = TaskFocusExtractor().extract(task, world)
    result = NeedValidator().validate(
        drafts=(grounded,),
        task=task,
        world=world,
        focus_set=focus_set,
        plan=make_synthetic_bundle().plan_roots[0],
    )
    assert [item.draft_id for item in result.accepted_drafts] == ["lexical-only"]


def test_query_compiler_keeps_lexical_query_and_fails_exact_graph_closed() -> None:
    """P005-like: unresolved anchor preserves BM25+dense, R1/graph fail closed."""
    task = _task()
    world = _entity_world()
    grounder = NeedDraftGrounder()
    grounded = grounder.ground(
        PlannedNeedDraft(
            draft_id="lexical-query",
            semantic_question="国教学院与教枢处的历史互动模式是怎样的?",
            entity_mentions=(EntityMention(label="国教学院", role_in_need="institution"),),
            trigger_plan_chapters=(21,),
            trigger_plan_goal="重申旧誓言",
            why_needed="第21章需要互动模式",
            required_claim_scopes=("current",),
            suggested_facets=("CURRENT_STATE",),
            historical_time_scope="main",
        ),
        world,
    )
    focus_set = TaskFocusExtractor().extract(task, world)
    NeedValidator().validate(
        drafts=(grounded,),
        task=task,
        world=world,
        focus_set=focus_set,
        plan=make_synthetic_bundle().plan_roots[0],
    )
    need = TaskPlanConditionedNeedGenerator().generate_evidence_first(
        task,
        world,
        make_synthetic_bundle().plan_roots[0],
        _planner_context(task),
        _frozen_lexical_artifact(grounded),
    )
    assert need is not None
    lexical = next(item for item in need.needs if item.planned_draft_id == "lexical-query")
    assert lexical.entity_ids == ()
    assert "国教学院" in lexical.query_text
    bundle = NeedQueryCompiler().compile(lexical)
    eligible, unavailable = NeedQueryCompiler.eligible_channels(
        lexical,
        bundle,
        (
            RetrievalChannel.ANCHOR_BM25,
            RetrievalChannel.ANCHOR_DENSE,
            RetrievalChannel.GROUNDED_BM25,
            RetrievalChannel.GROUNDED_DENSE,
            RetrievalChannel.R1_EXACT,
            RetrievalChannel.R1_TEMPORAL,
            RetrievalChannel.TYPED_GRAPH,
        ),
    )
    assert {channel.value for channel in eligible} == {
        "anchor_bm25",
        "anchor_dense",
        "grounded_bm25",
        "grounded_dense",
    }
    assert unavailable[RetrievalChannel.R1_EXACT] == "missing_exact_entity_or_predicate"
    assert unavailable[RetrievalChannel.TYPED_GRAPH] == "missing_graph_seed"


def _frozen_lexical_artifact(
    grounded: Any,
) -> PlannerInvocationArtifact:
    from novel_agent.domain.benchmark import AuthorPlanningContext
    from novel_agent.domain.planning_memory import (
        PLANNER_OUTPUT_SCHEMA_VERSION,
        PlannerArtifactMetadata,
        PlannerFallbackStatus,
    )

    context = AuthorPlanningContext(
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        task_intent="test",
        target_range=(21, 23),
        visible_outline_nodes=(),
        chapter_goals=(),
        source_hash=content_id({"t": "lexical"}),
        planner_may_read_plan=True,
    )
    world = _entity_world()
    drafts = (
        PlannedNeedDraft(
            draft_id="lexical-query",
            semantic_question="国教学院与教枢处的历史互动模式是怎样的?",
            entity_mentions=(EntityMention(label="国教学院", role_in_need="institution"),),
            trigger_plan_chapters=(21,),
            trigger_plan_goal="重申旧誓言",
            why_needed="第21章需要互动模式",
            required_claim_scopes=("current",),
            suggested_facets=("CURRENT_STATE",),
            historical_time_scope="main",
        ),
        PlannedNeedDraft(
            draft_id="injury-query",
            semantic_question="teacher 的伤势是否仍未痊愈?",
            entity_mentions=(EntityMention(label="teacher", role_in_need="subject"),),
            trigger_plan_chapters=(22,),
            trigger_plan_goal="保持受伤状态约束",
            why_needed="第22章需要伤势状态",
            required_claim_scopes=("current",),
            suggested_facets=("CURRENT_STATE",),
            historical_time_scope="main",
        ),
        PlannedNeedDraft(
            draft_id="tower-query",
            semantic_question="student 是否已承诺前往北塔?",
            entity_mentions=(EntityMention(label="student", role_in_need="subject"),),
            trigger_plan_chapters=(23,),
            trigger_plan_goal="进入北塔",
            why_needed="第23章需要承诺来源",
            required_claim_scopes=("historical",),
            suggested_facets=("CAUSAL_HISTORY",),
            historical_time_scope="main",
        ),
    )
    return PlannerInvocationArtifact(
        planning_context=context,
        world_summary=PlannerWorldSummaryBuilder.build(
            _task(),
            world,
            context,
        ),
        exact_prompt="prompt",
        metadata=PlannerArtifactMetadata(
            run_id=RunId("run.lexical.test"),
            planner_model="test-model",
            planner_model_revision="test",
            planner_prompt_version="v1",
            planner_prompt_hash=content_id({"p": "lexical"}),
            planner_output_schema_version=PLANNER_OUTPUT_SCHEMA_VERSION,
            temperature=0.0,
            requested_seed=None,
            effective_seed_supported=False,
            planning_context_hash=context.source_hash,
            world_summary_hash=content_id({"w": "lexical"}),
            raw_response_hash=content_id({"r": "lexical"}),
            validated_need_set_hash=content_id({"v": "lexical"}),
            fallback_used=False,
            input_tokens=1,
            output_tokens=1,
        ),
        raw_response='{"drafts": []}',
        attempts=(),
        parsed_drafts=drafts,
        validated_need_set_hash=content_id({"v": "lexical"}),
        fallback_status=PlannerFallbackStatus.PLANNER,
    )


def test_world_summary_target_aware_state_selection() -> None:
    """Target entities receive representative states within the fixed budget."""
    world = _entity_world()
    task = _task(task_intent="关键人物0 关键人物1 关键人物2 的伤势 与 承诺 状态")
    context = _planner_context(task)
    source_entity = world.entities[0]
    targets = tuple(
        source_entity.model_copy(
            update={
                "entity_id": StableId(f"entity.target.{index}"),
                "internal_label": f"关键人物{index}",
                "aliases": (),
            }
        )
        for index in range(3)
    )
    target_states: list[Any] = []
    for target in targets:
        for index in range(10):
            target_states.append(
                world.states[0].model_copy(
                    update={
                        "state_id": StableId(f"state.target.{target.entity_id.root}.{index}"),
                        "subject_id": target.entity_id,
                        "predicate": f"predicate{index}",
                        "value": f"value{index}",
                    }
                )
            )
    expanded = world.model_copy(
        update={"entities": (*world.entities, *targets), "states": (*world.states, *target_states)}
    )
    summary = PlannerWorldSummaryBuilder.build(task, expanded, context)
    assert len(summary.states) == min(PlannerWorldSummaryBuilder._MAX_STATES, len(expanded.states))
    assert summary.state_count == len(expanded.states)
    assert summary.truncated_state_count == len(expanded.states) - len(summary.states)
    coverage = {item.label: item for item in summary.target_state_coverage}
    for target in targets:
        item = coverage[target.internal_label]
        assert item.available == 10
        assert item.selected >= 1
        assert item.truncated == 10 - item.selected
    selected_subjects = {state.subject_label for state in summary.states}
    for target in targets:
        assert target.internal_label in selected_subjects
    # relations are never fabricated: the summary only exposes real records
    assert summary.relation_count == len(expanded.relations)
    assert len(summary.key_relations) == min(
        PlannerWorldSummaryBuilder._MAX_RELATIONS, len(expanded.relations)
    )


def test_world_summary_no_targets_keeps_bounded_first_selection() -> None:
    world = _entity_world()
    task = _task()
    summary = PlannerWorldSummaryBuilder.build(task, world, _planner_context(task))
    assert len(summary.states) <= PlannerWorldSummaryBuilder._MAX_STATES
    assert summary.target_state_coverage == ()
    assert summary.truncated_state_count == max(0, len(world.states) - len(summary.states))


def test_generate_evidence_first_requires_metadata_and_context() -> None:
    task = _task()
    world = _entity_world()
    plan = make_synthetic_bundle().plan_roots[0]
    generator = TaskPlanConditionedNeedGenerator()
    context = _planner_context(task)
    artifact = _frozen_lexical_artifact(None)
    with pytest.raises(ValueError, match="frozen Planner metadata"):
        generator.generate_evidence_first(
            task,
            world,
            plan,
            context,
            artifact.model_copy(update={"metadata": None}),
        )
    with pytest.raises(ValueError, match="AuthorPlanningContext"):
        generator.generate_evidence_first(
            task,
            world,
            plan,
            None,
            artifact,
        )


def test_generate_evidence_first_fallback_on_rejection_and_goal_gap() -> None:
    task = _task()
    world = _entity_world()
    plan = make_synthetic_bundle().plan_roots[0]
    generator = TaskPlanConditionedNeedGenerator()
    context = _planner_context(task)
    artifact = _frozen_lexical_artifact(None)
    # a draft-only artifact whose single draft is rejected -> template path
    rejected = artifact.model_copy(
        update={
            "parsed_drafts": (
                PlannedNeedDraft(
                    draft_id="bad-1",
                    semantic_question="无法验证的问题",
                    entity_mentions=(),
                    relation_mentions=(),
                    trigger_plan_chapters=(21,),
                    trigger_plan_goal="not canonical goal text",
                    required_claim_scopes=("current",),
                    suggested_facets=("CURRENT_STATE",),
                ),
            )
        }
    )
    assert generator.generate_evidence_first(task, world, plan, context, rejected) is None
    # drafts accepted but target goals uncovered -> template path
    partial = _frozen_lexical_artifact(None).model_copy(
        update={
            "parsed_drafts": (
                PlannedNeedDraft(
                    draft_id="only-21",
                    semantic_question="teacher 的伤势是否仍未痊愈?",
                    entity_mentions=(EntityMention(label="teacher", role_in_need="subject"),),
                    trigger_plan_chapters=(21,),
                    trigger_plan_goal="重申旧誓言",
                    why_needed="need",
                    required_claim_scopes=("current",),
                    suggested_facets=("CURRENT_STATE",),
                    historical_time_scope="main",
                ),
            )
        }
    )
    assert generator.generate_evidence_first(task, world, plan, context, partial) is None


def test_target_aware_selection_budget_breaks() -> None:
    world = _entity_world()
    task = _task(task_intent="关键人物0 关键人物1")
    context = _planner_context(task)
    source_entity = world.entities[0]
    targets = tuple(
        source_entity.model_copy(
            update={
                "entity_id": StableId(f"entity.target.{index}"),
                "internal_label": f"关键人物{index}",
                "aliases": (),
            }
        )
        for index in range(2)
    )
    target_states: list[Any] = []
    for target in targets:
        for index in range(80):
            target_states.append(
                world.states[0].model_copy(
                    update={
                        "state_id": StableId(f"state.target.{target.entity_id.root}.{index}"),
                        "subject_id": target.entity_id,
                        "predicate": f"predicate{index}",
                        "value": f"value{index}",
                    }
                )
            )
    expanded = world.model_copy(
        update={"entities": (*world.entities, *targets), "states": (*world.states, *target_states)}
    )
    summary = PlannerWorldSummaryBuilder.build(task, expanded, context)
    assert len(summary.states) == PlannerWorldSummaryBuilder._MAX_STATES
    assert summary.truncated_state_count == len(expanded.states) - len(summary.states)
    coverage = {item.label: item for item in summary.target_state_coverage}
    assert coverage["关键人物0"].available == 80
    assert coverage["关键人物0"].selected <= PlannerWorldSummaryBuilder._MAX_STATES
    assert coverage["关键人物0"].truncated == 80 - coverage["关键人物0"].selected


def test_grounder_ambiguous_alias_and_blank_labels() -> None:
    world = _entity_world()
    source = world.entities[0]
    shared = source.model_copy(
        update={
            "entity_id": StableId("entity.planner.alias-a"),
            "internal_label": "alias-a",
            "aliases": ("共同别名",),
        }
    )
    shared_b = source.model_copy(
        update={
            "entity_id": StableId("entity.planner.alias-b"),
            "internal_label": "alias-b",
            "aliases": ("共同别名",),
        }
    )
    blank = source.model_copy(
        update={
            "entity_id": StableId("entity.planner.blank"),
            "internal_label": "  ",
            "aliases": ("", " "),
        }
    )
    expanded = world.model_copy(update={"entities": (*world.entities, shared, shared_b, blank)})
    grounder = NeedDraftGrounder()
    grounded = grounder.ground(
        PlannedNeedDraft(
            draft_id="alias-ambiguous",
            semantic_question="共同别名 当前状态是什么?",
            entity_mentions=(EntityMention(label="共同别名", role_in_need="subject"),),
        ),
        expanded,
    )
    mention = grounded.entity_mentions[0]
    assert mention.grounding_status is GroundingStatus.AMBIGUOUS
    assert mention.grounding_method == "ambiguous_alias_match"
    assert mention.entity_id is None
    resolvable = grounder._resolvable_label_map(expanded)
    assert "共同别名" not in resolvable


def test_generate_evidence_first_fallback_artifact_returns_none() -> None:
    task = _task()
    world = _entity_world()
    plan = make_synthetic_bundle().plan_roots[0]
    generator = TaskPlanConditionedNeedGenerator()
    artifact = _frozen_lexical_artifact(None).model_copy(
        update={"fallback_status": PlannerFallbackStatus.PLANNER_FALLBACK}
    )
    assert (
        generator.generate_evidence_first(
            task,
            world,
            plan,
            _planner_context(task),
            artifact,
        )
        is None
    )


def test_validator_keeps_legitimate_history_questions_not_in_word_list() -> None:
    # 2026-08-17 diagnosis §4 (P003/C60 root cause): the closed history-word
    # list must not infer "future fact" from absence. The two real C64 drafts
    # rejected as plan_goal_as_fact in the §6 run are legitimate history
    # questions (pre-cutoff marriage-contract content) and must pass.
    task = _task()
    world = _entity_world()
    validator = NeedValidator()
    c64_questions = (
        "陈长生与徐有容之间的婚约具体内容和法律/社会效力?",
        "徐世绩对陈长生与徐有容婚约的态度及过往干预行为?",
    )
    # The narrowed check is the direct §4 semantic: absence from the closed
    # history-word list must not imply a future fact.
    assert not any(validator._looks_future_factualized(q) for q in c64_questions)
    drafts = tuple(
        _draft(f"c64-{index}", question, chapters=(21,), goal="重申旧誓言")
        for index, question in enumerate(c64_questions)
    )
    grounder = NeedDraftGrounder()
    grounded = tuple(grounder.ground(draft, world) for draft in drafts)
    focus_set = TaskFocusExtractor().extract(task, world)
    result = validator.validate(
        drafts=grounded,
        task=task,
        world=world,
        focus_set=focus_set,
        plan=make_synthetic_bundle().plan_roots[0],
    )
    assert "plan_goal_as_fact" not in result.rejected_reasons
    accepted = {draft.draft_id for draft in result.accepted_drafts}
    assert {"c64-0", "c64-1"} <= accepted


def test_validator_still_rejects_explicit_future_and_goal_restatement() -> None:
    # The explicit-future-marker rejection and the canonical goal restatement
    # rejection remain strict after the §4 narrowing.
    task = _task()
    world = _entity_world()
    validator = NeedValidator()
    assert validator._looks_future_factualized("陈长生将会进入公开场合吗")
    assert validator._looks_future_factualized("计划中如何处理婚约")
    assert not validator._looks_future_factualized(
        "陈长生与徐有容之间的婚约具体内容和法律/社会效力?"
    )
    draft_fact = _draft("fact", "重申旧誓言", chapters=(21,), goal="重申旧誓言")
    grounder = NeedDraftGrounder()
    grounded = (grounder.ground(draft_fact, world),)
    focus_set = TaskFocusExtractor().extract(task, world)
    result = validator.validate(
        drafts=grounded,
        task=task,
        world=world,
        focus_set=focus_set,
        plan=make_synthetic_bundle().plan_roots[0],
    )
    assert result.rejected_reasons.get("fact") == "plan_goal_as_fact"


def test_bounded_repair_closes_missing_goal_chapters() -> None:
    """P0-4b: one bounded semantic-repair closes a target-goal coverage gap."""
    first_payload: dict[str, object] = {
        "drafts": [
            {
                "draft_id": "only-21",
                "semantic_question": "在截止点前 teacher 的伤势是否仍未痊愈?",
                "entity_mentions": [{"label": "teacher", "role_in_need": "subject"}],
                "relation_mentions": [],
                "trigger_plan_chapters": [21],
                "trigger_plan_goal": "重申旧誓言",
                "why_needed": "plan the memory reload",
                "required_claim_scopes": ["current"],
                "suggested_facets": ["CURRENT_STATE"],
                "historical_time_scope": "main",
                "query_hints": ["teacher 伤势"],
            }
        ],
        "meta": {"rationale": "partial"},
    }
    repair_payload: dict[str, object] = {
        "drafts": [
            {
                "draft_id": "repair-21",
                "semantic_question": "在截止点前 teacher 的伤势是否仍未痊愈?",
                "entity_mentions": [{"label": "teacher", "role_in_need": "subject"}],
                "relation_mentions": [],
                "trigger_plan_chapters": [21],
                "trigger_plan_goal": "重申旧誓言",
                "why_needed": "repair keeps ch21",
                "required_claim_scopes": ["current"],
                "suggested_facets": ["CURRENT_STATE"],
                "historical_time_scope": "main",
                "query_hints": [],
            },
            {
                "draft_id": "repair-22",
                "semantic_question": "在截止点前 student 是否已承诺前往北塔?",
                "entity_mentions": [{"label": "student", "role_in_need": "subject"}],
                "relation_mentions": [],
                "trigger_plan_chapters": [22],
                "trigger_plan_goal": "进入北塔",
                "why_needed": "repair adds ch22",
                "required_claim_scopes": ["historical"],
                "suggested_facets": ["CAUSAL_HISTORY"],
                "historical_time_scope": "main",
                "query_hints": [],
            },
            {
                "draft_id": "repair-23",
                "semantic_question": "在截止点前 teacher 对 student 秘密的知情边界是什么?",
                "entity_mentions": [{"label": "teacher", "role_in_need": "subject"}],
                "relation_mentions": [],
                "trigger_plan_chapters": [23],
                "trigger_plan_goal": "进入北塔",
                "why_needed": "repair adds ch23",
                "required_claim_scopes": ["knowledge"],
                "suggested_facets": ["KNOWLEDGE_BOUNDARY"],
                "historical_time_scope": "main",
                "query_hints": [],
            },
        ],
        "meta": {"rationale": "repair covers missing chapters"},
    }
    endpoint = _PlannerEndpoint((first_payload, repair_payload))
    gateway = _gateway(endpoint)
    task = _task()
    world = _entity_world()
    plan = make_synthetic_bundle().plan_roots[0]
    context = _planner_context(task)
    generator = TaskPlanConditionedNeedGenerator(planner_gateway=gateway)
    result = generator.generate_with_lineage(task, world, plan, context)
    assert result.status is NeedGenerationStatus.READY
    assert result.fallback_used is False
    assert len(endpoint.requests) == 2
    assert "【修复要求】" in endpoint.requests[1].prompt
    assert "返回完整替代 drafts 批次" in endpoint.requests[1].prompt
    assert "22" in endpoint.requests[1].prompt and "23" in endpoint.requests[1].prompt
    assert result.planner_artifact is not None
    assert not result.planner_artifact.missing_goal_chapters
    assert len(result.planner_artifact.attempts) == 2


def test_bounded_repair_still_incomplete_fails_closed() -> None:
    """P0-4b: a repair that stays incomplete stops with the typed fallback."""
    first_payload: dict[str, object] = {
        "drafts": [
            {
                "draft_id": "only-21",
                "semantic_question": "在截止点前 teacher 的伤势是否仍未痊愈?",
                "entity_mentions": [{"label": "teacher", "role_in_need": "subject"}],
                "relation_mentions": [],
                "trigger_plan_chapters": [21],
                "trigger_plan_goal": "重申旧誓言",
                "why_needed": "plan the memory reload",
                "required_claim_scopes": ["current"],
                "suggested_facets": ["CURRENT_STATE"],
                "historical_time_scope": "main",
                "query_hints": ["teacher 伤势"],
            }
        ],
        "meta": {"rationale": "partial"},
    }
    still_partial: dict[str, object] = {
        "drafts": [
            {
                "draft_id": "still-21",
                "semantic_question": "在截止点前 teacher 的伤势是否仍未痊愈?",
                "entity_mentions": [{"label": "teacher", "role_in_need": "subject"}],
                "relation_mentions": [],
                "trigger_plan_chapters": [21],
                "trigger_plan_goal": "重申旧誓言",
                "why_needed": "still partial",
                "required_claim_scopes": ["current"],
                "suggested_facets": ["CURRENT_STATE"],
                "historical_time_scope": "main",
                "query_hints": [],
            }
        ],
        "meta": {"rationale": "repair still partial"},
    }
    endpoint = _PlannerEndpoint((first_payload, still_partial))
    gateway = _gateway(endpoint)
    task = _task()
    world = _entity_world()
    plan = make_synthetic_bundle().plan_roots[0]
    context = _planner_context(task)
    generator = TaskPlanConditionedNeedGenerator(planner_gateway=gateway)
    result = generator.generate_with_lineage(task, world, plan, context)
    assert result.status is NeedGenerationStatus.PLANNER_FALLBACK
    assert result.fallback_used is True
    assert result.planner_artifact is not None
    assert result.planner_artifact.fallback_reason == "insufficient_target_goal_coverage"
    assert set(result.planner_artifact.missing_goal_chapters) == {22, 23}


def test_planner_contract_finding_repairs_composite_label_with_label_map() -> None:
    """Review-25#1: an annotated composite explicit label is a typed finding,
    and the bounded repair prompt carries the canonical label map."""
    world = _chinese_world()
    plan = _chinese_goals_plan(world)
    context = AuthorPlanningContext(
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        task_intent="为写 81-85 章准备历史记忆",
        target_range=(81, 85),
        visible_outline_nodes=(),
        chapter_goals=plan.chapter_goals,
        source_hash=content_id({"goals": "contract-finding"}),
        planner_may_read_plan=True,
    )
    task = _task(task_intent="为写 81 章准备历史记忆")
    task = task.model_copy(update={"target_chapter_start": 81, "target_chapter_end": 81})
    composite_payload: dict[str, object] = {
        "drafts": [
            {
                "draft_id": "composite-81",
                "semantic_question": "陈长生与落落之间的教学关系如何发展?",
                "entity_mentions": [
                    {"label": "落衡(别名: 落落)", "role_in_need": "subject"},
                    {"label": "陈长生", "role_in_need": "object"},
                ],
                "relation_mentions": [],
                "trigger_plan_chapters": [81],
                "trigger_plan_goal": "师徒教学与感情进展",
                "why_needed": "决定第81章教学方式",
                "required_claim_scopes": ["current"],
                "suggested_facets": ["RELATION_STATE"],
                "historical_time_scope": "main",
                "query_hints": [],
            }
        ],
        "meta": {"rationale": "composite label"},
    }
    repaired_payload: dict[str, object] = {
        "drafts": [
            {
                "draft_id": "exact-81",
                "semantic_question": "陈长生对落落的教学方式是什么?",
                "entity_mentions": [
                    {"label": "落落", "role_in_need": "subject"},
                    {"label": "陈长生", "role_in_need": "object"},
                ],
                "relation_mentions": [],
                "trigger_plan_chapters": [81],
                "trigger_plan_goal": "师徒教学与感情进展",
                "why_needed": "决定第81章教学方式",
                "required_claim_scopes": ["current"],
                "suggested_facets": ["RELATION_STATE"],
                "historical_time_scope": "main",
                "query_hints": [],
            }
        ],
        "meta": {"rationale": "exact labels"},
    }
    endpoint = _PlannerEndpoint((composite_payload, repaired_payload))
    gateway = _gateway(endpoint)
    generator = TaskPlanConditionedNeedGenerator(planner_gateway=gateway)
    result = generator.generate_with_lineage(task, world, plan, context)
    assert result.status is NeedGenerationStatus.READY
    assert len(endpoint.requests) == 2
    repair_prompt = endpoint.requests[1].prompt
    assert "落衡(别名: 落落)" in repair_prompt  # named as the offending label
    map_block = repair_prompt.split("规范标签映射(只使用这些):", 1)[1]
    assert "落衡(别名: 落落)" not in map_block  # never advertised as usable
    for expected in ("- 陈长生", "- 长生", "- 落落", "- 黑龙"):
        assert expected in map_block  # each exact value advertised separately
    assert result.planner_artifact is not None
    assert result.planner_artifact.planner_contract_findings == {}
    assert result.needs


def test_planner_pins_configured_output_ceiling_on_gateway_request() -> None:
    """P004 regression: the Planner request pins the configured bounded budget
    (8,192 per Plan v13), not a hard-coded 4,096."""
    endpoint = _PlannerEndpoint((_planner_payload(),))
    gateway = _gateway(endpoint)
    task = _task()
    world = _entity_world()
    planner = PlanConditionedNeedPlanner(gateway=gateway)
    context = _planner_context(task)
    planner.plan(task=task, world=world, planning_context=context)
    assert endpoint.requests[0].max_output_tokens == 8192
    assert endpoint.requests[0].enable_thinking is False


def test_planner_parses_schema_maximal_bounded_draft_batch() -> None:
    """P004 regression: a max-size draft batch parses and validates."""
    drafts = [
        {
            "draft_id": f"draft-{index:02d}",
            "semantic_question": f"在截止点前 teacher 的第{index}项伤势状态是什么?",
            "entity_mentions": [{"label": "teacher", "role_in_need": "subject"}],
            "relation_mentions": [],
            "trigger_plan_chapters": [21],
            "trigger_plan_goal": "重申旧誓言",
            "why_needed": "batch draft",
            "required_claim_scopes": ["current"],
            "suggested_facets": ["CURRENT_STATE"],
            "historical_time_scope": "main",
            "query_hints": [],
        }
        for index in range(24)
    ]
    parsed = PlanConditionedNeedPlanner._parse_drafts(
        json.dumps({"drafts": drafts, "meta": {"rationale": "maximal"}}, ensure_ascii=False)
    )
    assert len(parsed) == 24
    assert len({draft.draft_id for draft in parsed}) == 24


def test_planner_parser_does_not_apply_legacy_twenty_four_draft_ceiling() -> None:
    drafts = [
        {
            "draft_id": f"draft-{index:02d}",
            "semantic_question": f"在截止点前 teacher 的第{index}项历史状态是什么?",
            "entity_mentions": [{"label": "teacher", "role_in_need": "subject"}],
            "relation_mentions": [],
            "trigger_plan_chapters": [21],
            "trigger_plan_goal": "重申旧誓言",
            "why_needed": "capacity regression",
            "required_claim_scopes": ["current"],
            "suggested_facets": ["CURRENT_STATE"],
            "historical_time_scope": "main",
            "query_hints": [],
        }
        for index in range(25)
    ]
    parsed = PlanConditionedNeedPlanner._parse_drafts(
        json.dumps({"drafts": drafts, "meta": {"rationale": "over legacy ceiling"}})
    )
    assert len(parsed) == 25


def test_planner_marks_single_oversized_page_capacity_exhausted() -> None:
    task = _task()
    planner = PlanConditionedNeedPlanner(max_input_tokens=256)
    pages = planner.partition_goal_pages(
        task=task,
        world=_entity_world(),
        planning_context=_planner_context(task),
    )
    assert pages
    assert any(page.status.value == "capacity_exhausted" for page, _context in pages)
    assert all(
        page.error_category == "planner_page_capacity_exhausted"
        for page, _context in pages
        if page.status.value == "capacity_exhausted"
    )


def test_planner_output_length_terminal_stays_typed_and_never_silent() -> None:
    """P004 regression: an output-length terminal result is a typed Planner
    fallback with the error category, never a silent empty success."""
    from novel_agent.adapters.model.openai_chat import OpenAIChatOutputLengthError

    class _LengthEndpoint(_PlannerEndpoint):
        is_external = False

        async def generate(self, request: ModelRequest) -> ProviderModelResult:
            self.requests.append(request)
            raise OpenAIChatOutputLengthError(
                "response exhausted its output-token allowance",
                finish_reason="length",
                input_tokens=100,
                output_tokens=8192,
            )

    endpoint = _LengthEndpoint(())
    gateway = _gateway(endpoint)
    task = _task()
    world = _entity_world()
    planner = PlanConditionedNeedPlanner(gateway=gateway)
    context = _planner_context(task)
    result = planner.plan(task=task, world=world, planning_context=context)
    assert result.fallback_status is PlannerFallbackStatus.PLANNER_FALLBACK
    assert result.error_category == "OpenAIChatOutputLengthError"
    assert result.drafts == ()
    assert result.metadata is not None
    assert result.metadata.fallback_used is True
    assert len(result.attempts) == 2  # bounded retries, all typed errors
    assert all(
        attempt.status is PlannerInvocationAttemptStatus.ERROR for attempt in result.attempts
    )
    assert all(
        attempt.error_category == "OpenAIChatOutputLengthError" for attempt in result.attempts
    )


def test_planner_cli_ceiling_threads_through_generator_to_request() -> None:
    """Review-26#3: a non-default model-max-output-tokens CLI value must reach
    ``ModelRequest.max_output_tokens`` through generator -> planner, and stays
    bounded by the planner's constructor validation."""
    endpoint = _PlannerEndpoint((_planner_payload(),))
    gateway = _gateway(endpoint)
    task = _task()
    world = _entity_world()
    plan = make_synthetic_bundle().plan_roots[0]
    context = _planner_context(task)
    generator = TaskPlanConditionedNeedGenerator(
        planner_gateway=gateway,
        planner_max_output_tokens=4096,
    )
    result = generator.generate_with_lineage(task, world, plan, context)
    assert result.status is NeedGenerationStatus.READY
    assert endpoint.requests[0].max_output_tokens == 4096

    endpoint2 = _PlannerEndpoint(())
    gateway2 = _gateway(endpoint2)
    with pytest.raises(ValueError):
        PlanConditionedNeedPlanner(gateway=gateway2, max_output_tokens=256)


def test_repair_exhausts_output_limit_persists_terminal_fallback() -> None:
    """Review-26#1: when the bounded repair invocation exhausts its output
    allowance, the chain must STOP with a typed PLANNER_FALLBACK artifact that
    persists BOTH invocations, the repair prompt/raw and the real error
    category; it never resumes the first drafts and never substitutes
    deterministic Needs."""
    from novel_agent.adapters.model.openai_chat import OpenAIChatOutputLengthError
    from novel_agent.domain.planning_memory import PlannerInvocationAttemptStatus

    repair_raw = '{"drafts": ['
    first_payload: dict[str, object] = {
        "drafts": [
            {
                "draft_id": "only-21",
                "semantic_question": "在截止点前 teacher 的伤势是否仍未痊愈?",
                "entity_mentions": [{"label": "teacher", "role_in_need": "subject"}],
                "relation_mentions": [],
                "trigger_plan_chapters": [21],
                "trigger_plan_goal": "重申旧誓言",
                "why_needed": "plan the memory reload",
                "required_claim_scopes": ["current"],
                "suggested_facets": ["CURRENT_STATE"],
                "historical_time_scope": "main",
                "query_hints": ["teacher 伤势"],
            }
        ],
        "meta": {"rationale": "partial"},
    }

    class _LengthEndpoint(_PlannerEndpoint):
        is_external = False

        async def generate(self, request: ModelRequest) -> ProviderModelResult:
            self.requests.append(request)
            if len(self.requests) == 1:
                return ProviderModelResult(
                    text=json.dumps(first_payload, ensure_ascii=False),
                    model_version=self.model,
                    usage=ModelUsage(input_tokens=10, output_tokens=10, cost_usd=Decimal("0")),
                )
            raise OpenAIChatOutputLengthError(
                "repair response exhausted its output-token allowance",
                finish_reason="length",
                input_tokens=100,
                output_tokens=8192,
                raw_content=repair_raw,
            )

    endpoint = _LengthEndpoint(())
    gateway = _gateway(endpoint)
    task = _task()
    world = _entity_world()
    plan = make_synthetic_bundle().plan_roots[0]
    context = _planner_context(task)
    generator = TaskPlanConditionedNeedGenerator(planner_gateway=gateway)
    result = generator.generate_with_lineage(task, world, plan, context)
    assert result.status is NeedGenerationStatus.PLANNER_FALLBACK
    assert result.fallback_used is True
    artifact = result.planner_artifact
    assert artifact is not None
    assert artifact.fallback_status is PlannerFallbackStatus.PLANNER_FALLBACK
    assert artifact.fallback_reason == "OpenAIChatOutputLengthError"
    assert len(artifact.attempts) == 2
    assert artifact.attempts[0].status is PlannerInvocationAttemptStatus.SUCCEEDED
    assert artifact.attempts[1].status is PlannerInvocationAttemptStatus.ERROR
    assert artifact.exact_prompt  # terminal repair prompt persisted
    assert artifact.raw_response == repair_raw
    assert artifact.attempts[1].raw_response == repair_raw
    assert artifact.metadata is not None
    assert artifact.metadata.input_tokens == 110
    assert artifact.metadata.output_tokens == 8202
    assert artifact.metadata.raw_response_hash == content_id({"raw": repair_raw})
    assert set(artifact.missing_goal_chapters) == {22, 23}
    assert artifact.planner_contract_findings == {}
    assert result.needs == ()  # never substituted deterministic Needs
