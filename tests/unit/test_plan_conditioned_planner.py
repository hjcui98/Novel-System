from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.benchmark import AuthorPlanningContext, VisibleOutlineNode
from novel_agent.domain.ids import ArtifactId, RunId, SchemaVersion, StableId
from novel_agent.domain.memory import ObligationStatus, WorldRootDocument
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

    def __init__(self, payloads: tuple[dict[str, object] | Exception, ...]) -> None:
        self.payloads: list[dict[str, object] | Exception] = list(payloads)
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
            }
        ],
        "meta": {"rationale": "backward chaining from chapter 22"},
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
    assert len(result.drafts) == 1
    draft = result.drafts[0]
    assert draft.draft_id == "marriage-knowledge"
    assert "秘密" in draft.semantic_question
    assert draft.trigger_plan_chapters == (22,)
    assert draft.suggested_facets == ("KNOWLEDGE_BOUNDARY",)
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
    draft_goal_mismatch = _draft("goal-mismatch", "question?", goal="not canonical")
    draft_unanchored = PlannedNeedDraft(
        draft_id="unanchored",
        semantic_question="ghost 当前状态是什么?",
        entity_mentions=(EntityMention(label="ghost", role_in_need="subject"),),
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
            draft_unanchored,
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
    assert [draft.draft_id for draft in result.accepted_drafts] == ["a"]
    assert set(result.rejected_draft_ids) == {
        "out",
        "fact",
        "scope",
        "unknown-facet",
        "missing-goal",
        "goal-mismatch",
        "unanchored",
    }
    assert result.rejected_reasons["out"] == "out_of_range_chapters"
    assert result.rejected_reasons["fact"] == "plan_goal_as_fact"
    assert result.rejected_reasons["scope"] == "unknown_time_scope"
    assert result.rejected_reasons["unknown-facet"] == "unknown_or_empty_scope_facet"
    assert result.rejected_reasons["missing-goal"] == "missing_trigger_goal_binding"
    assert result.rejected_reasons["goal-mismatch"] == "trigger_goal_mismatch"
    assert result.rejected_reasons["unanchored"] == "no_grounded_anchor"
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
    assert need.requirement.value == "optional"
    assert all(item is not None for item in (need.semantic_question,))
    # The plan-obligation channel remains explicit when the planner asks for it.
    assert all(item.claim_may_cite_plan is False for item in needs)


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


def test_grounder_fuzzy_and_ambiguous_fuzzy_mentions() -> None:
    world = _entity_world()
    grounder = NeedDraftGrounder()
    fuzzy = grounder.ground(
        PlannedNeedDraft(
            draft_id="fuzzy",
            semantic_question="question?",
            entity_mentions=(EntityMention(label="徒", role_in_need="student"),),
        ),
        world,
    )
    student = next(entity for entity in world.entities if entity.internal_label == "student")
    assert fuzzy.entity_mentions[0].grounding_status is GroundingStatus.GROUNDED
    assert fuzzy.entity_mentions[0].entity_id == student.entity_id

    student_b = student.model_copy(
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
    assert fuzzy_ambiguous.entity_mentions[0].grounding_status is GroundingStatus.AMBIGUOUS


def test_grounder_relation_context_disambiguates_shared_labels() -> None:
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
    assert shared_mention.grounding_status is GroundingStatus.GROUNDED
    assert shared_mention.entity_id == shared_a.entity_id
    assert shared_mention.grounding_method == "relation_context_match"


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


def test_grounder_relation_context_uses_resolved_other_endpoint() -> None:
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
    assert shared_mention.entity_id == shared_a.entity_id
    assert shared_mention.grounding_method == "relation_context_match"


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
