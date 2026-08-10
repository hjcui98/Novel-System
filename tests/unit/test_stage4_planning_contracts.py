from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.agents.planner import PLANNER_MODES, build_planner_contract_bundle
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
    ChannelHit,
    ContextBudgetReport,
    NeedFacetKind,
    RetrievalChannel,
    RetrievalUnit,
    RetrievalUnitKind,
    Stage1ContextPackage,
    Stage1QueryIntent,
    TypedGraphPathReceipt,
)
from novel_agent.domain.planning import (
    GoalProposal,
    PlannerContextBudgetReport,
    PlannerContextItem,
    PlannerContextPackage,
    PlannerContextProjection,
    PlannerContextSection,
    PlanningBudgets,
    PlanningEvaluationCase,
    PlanningEvaluationManifest,
    PlanningInquiry,
    PlanningLoopRequest,
    PlanningLoopResult,
    PlanningLoopTerminal,
    PlanningProvenance,
    PlanningQuestion,
    PlanningQuestionKind,
    PlanningReference,
    PlanningTurnAction,
    PlanningTurnOutput,
    PlanReview,
    PlanReviewDraft,
    PlanReviewIssue,
    ReviewDecision,
    ReviewIssueKind,
    ReviewTargetKind,
)
from novel_agent.domain.planning_memory import PlannerNeedGenerationResult
from novel_agent.domain.retrieval_routing import ResolutionTier
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentType,
    BootstrapStrategy,
    ContextBudget,
    ContractRef,
    ExecutionStatus,
    PlanningTask,
    PlanProposal,
    RetrievalBudget,
)
from novel_agent.runtime.memory_controller import RouteBoundControllerPolicy
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.planner_context_assembler import PlannerContextAssembler
from novel_agent.services.planning_inquiry_need_generation import (
    PlanningInquiryConditionedNeedGenerator,
    PlanningInquiryNeedError,
)
from novel_agent.services.retrieval_routing import profile_for
from tests.fixtures.stage1_synthetic import make_synthetic_bundle

VERSION = SchemaVersion("1.0.0")
HASH = ArtifactId("sha256:" + "a" * 64)
BASE = CommitId("sha256:" + "1" * 64)
PROJECT = ProjectId("project.synthetic")


def _repo(tmp_path: Path) -> ArtifactRepository:
    return ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))


def _put(repo: ArtifactRepository, text: str, media_type: str = "text/plain") -> ArtifactRef:
    return repo.put(text.encode(), media_type, VERSION)


def _receipt(mode: AgentMode, agent_type: AgentType) -> AgentExecutionReceipt:
    now = datetime.now(UTC)
    return AgentExecutionReceipt(
        receipt_id=StableId(f"receipt.{agent_type.value}.{mode.value}"),
        run_id=RunId("run.stage4"),
        task_id=TaskId("task.stage4"),
        agent_spec=ContractRef(
            contract_id=StableId(f"agent.{agent_type.value}.{mode.value}"),
            version=VERSION,
            content_hash=HASH,
        ),
        agent_type=agent_type,
        agent_mode=mode,
        prompt_fingerprint=HASH,
        configuration_fingerprint=HASH,
        base_commit=None if mode is AgentMode.PROJECT_BOOTSTRAP else BASE,
        status=ExecutionStatus.SUCCEEDED,
        started_at=now,
        completed_at=now,
        latency_ms=0,
    )


def _inquiry(mode: AgentMode, source: ArtifactRef) -> PlanningInquiry:
    goal = GoalProposal(
        goal_id=StableId("goal.stage4.primary"),
        summary="林澈在滚动窗口中处理伤势与北塔义务",
        rationale="保持已接受事实和长期义务一致",
        provenance=PlanningReference(provenance=PlanningProvenance.PLANNER_PROPOSED),
        decision_criteria=("可实现", "不违背伤势"),
    )
    question = PlanningQuestion(
        question_id=StableId("question.stage4.injury"),
        kind=PlanningQuestionKind.FACT,
        question="林澈当前的伤势状态是什么?",
        provenance=PlanningReference(
            provenance=PlanningProvenance.AUTHOR_SUPPLIED,
            reference_ids=(StableId("source.brief"),),
        ),
        goal_id=goal.goal_id,
        entity_labels=("林澈",),
        blocking=True,
    )
    return PlanningInquiry(
        inquiry_id=StableId(f"inquiry.{mode.value}"),
        project_id=PROJECT,
        mode=mode,
        planning_scope=("rolling",),
        horizon_start=None if mode is AgentMode.PROJECT_BOOTSTRAP else 21,
        horizon_end=None if mode is AgentMode.PROJECT_BOOTSTRAP else 23,
        author_intent_refs=(source,),
        goal_proposals=(goal,),
        questions=(question,),
        expected_output_shape="bounded PlanProposal",
    )


def _request(
    mode: AgentMode,
    source: ArtifactRef,
    *,
    accepted: tuple[ArtifactRef, ArtifactRef, ArtifactRef] | None = None,
) -> PlanningLoopRequest:
    bootstrap = mode is AgentMode.PROJECT_BOOTSTRAP
    task = PlanningTask(
        planning_task_id=StableId(f"planning.{mode.value}"),
        project_id=PROJECT,
        mode=mode,
        base_commit=None if bootstrap else BASE,
        source_ids=(StableId("source.brief"),),
        creative_scope=("rolling",),
        strategy=BootstrapStrategy.DEVELOP_CANDIDATES if bootstrap else None,
    )
    refs: tuple[ArtifactRef | None, ArtifactRef | None, ArtifactRef | None] = (
        accepted if accepted is not None else (None, None, None)
    )
    return PlanningLoopRequest(
        request_id=StableId(f"planning-request.{mode.value}"),
        run_id=RunId("run.stage4"),
        task_id=TaskId("task.stage4"),
        project_id=PROJECT,
        task=task,
        author_intent_artifacts=(source,),
        accepted_plan_ref=refs[0],
        accepted_world_ref=refs[1],
        accepted_text_ref=refs[2],
        snapshot_id=None if bootstrap else StableId("snapshot.stage4"),
        horizon_start=None if bootstrap else 21,
        horizon_end=None if bootstrap else 23,
        budgets=PlanningBudgets(
            retrieval=RetrievalBudget(),
            context=ContextBudget(token_budget=8_000),
        ),
        configuration_fingerprint=HASH,
        model_fingerprint=HASH,
    )


def test_planner_contract_bundle_is_the_single_sealed_seven_mode_registration() -> None:
    bundle = build_planner_contract_bundle()

    assert len(PLANNER_MODES) == 7
    assert len(bundle.agent_specs) == 14
    for mode in PLANNER_MODES:
        planner = bundle.agents.resolve(AgentType.PLANNER, mode, "1.0.0")
        reviewer = bundle.agents.resolve(AgentType.PLAN_REVIEWER, mode, "1.0.0")
        assert "commit" in planner.tool_policy.denied_tools
        assert "plan_root.write" in reviewer.tool_policy.denied_tools
        assert reviewer.tool_policy.allowed_tools == ("proposal.validate_plan",)
        assert planner.content_hash != HASH
        assert reviewer.content_hash != HASH


def test_bootstrap_has_no_basis_or_memory_while_chapter_set_requires_rolling_basis(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    source = _put(repo, "author brief")
    bootstrap = _request(AgentMode.PROJECT_BOOTSTRAP, source)
    assert bootstrap.task.base_commit is None
    assert bootstrap.snapshot_id is None

    raw_refs = tuple(_put(repo, f"accepted-{index}") for index in range(3))
    refs = (raw_refs[0], raw_refs[1], raw_refs[2])
    chapter_set = _request(AgentMode.CHAPTER_SET, source, accepted=refs)
    assert chapter_set.horizon_start == 21
    with pytest.raises(ValidationError, match="rolling horizon"):
        PlanningLoopRequest.model_validate(
            chapter_set.model_dump() | {"horizon_start": None, "horizon_end": None}
        )


def test_reviewed_inquiry_generates_stable_author_planning_needs_and_query_bundles(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    source = _put(repo, "林澈需要处理伤势与北塔义务。")
    inquiry = _inquiry(AgentMode.CHAPTER_SET, source)
    inquiry_ref = repo.put(inquiry.model_dump_json().encode(), "application/json", VERSION)
    review = PlanReview(
        review_id=StableId("review.inquiry.accept"),
        target_kind=ReviewTargetKind.INQUIRY,
        target_artifact_ref=inquiry_ref,
        decision=ReviewDecision.ACCEPT,
        receipt=_receipt(AgentMode.CHAPTER_SET, AgentType.PLAN_REVIEWER),
    )
    review_ref = repo.put(review.model_dump_json().encode(), "application/json", VERSION)
    world = make_synthetic_bundle().world_roots[0]
    generator = PlanningInquiryConditionedNeedGenerator()

    first = generator.generate(
        inquiry=inquiry,
        inquiry_ref=inquiry_ref,
        review=review,
        review_ref=review_ref,
        world=world,
        run_id=RunId("run.stage4"),
        task_id=TaskId("task.stage4"),
    )
    second = generator.generate(
        inquiry=inquiry,
        inquiry_ref=inquiry_ref,
        review=review,
        review_ref=review_ref,
        world=world,
        run_id=RunId("run.stage4"),
        task_id=TaskId("task.stage4"),
    )

    assert first == second
    assert len(first.needs) == 1
    need = first.needs[0]
    assert need.access_scope == "author_planning"
    assert need.entity_ids == (world.entities[0].entity_id,)
    assert need.planner_artifact_ref == inquiry_ref.artifact_id
    assert first.query_bundles[need.need_id.root].exact_entity_ids == need.entity_ids
    with pytest.raises(ValidationError, match="cover the final Need set"):
        PlannerNeedGenerationResult.model_validate(first.model_dump() | {"query_bundles": {}})
    with pytest.raises(ValidationError, match="identities must be unique"):
        PlannerNeedGenerationResult.model_validate(first.model_dump() | {"needs": (need, need)})
    with pytest.raises(PlanningInquiryNeedError, match="accepted inquiry"):
        generator.generate(
            inquiry=inquiry,
            inquiry_ref=inquiry_ref,
            review=review.model_copy(update={"decision": ReviewDecision.HUMAN_REQUIRED}),
            review_ref=review_ref,
            world=world,
            run_id=RunId("run.stage4"),
            task_id=TaskId("task.stage4"),
        )


def test_planner_need_generator_covers_question_kinds_rejections_and_review_binding(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="positive"):
        PlanningInquiryConditionedNeedGenerator(max_total_needs=0)
    repo = _repo(tmp_path)
    source = _put(repo, "author")
    base = _inquiry(AgentMode.CHAPTER_SET, source)
    goal_id = base.goal_proposals[0].goal_id
    trusted = PlanningReference(
        provenance=PlanningProvenance.AUTHOR_SUPPLIED,
        reference_ids=(StableId("source.brief"),),
    )
    questions = (
        PlanningQuestion(
            question_id=StableId("question.relation"),
            kind=PlanningQuestionKind.RELATION_CAUSAL,
            question="林澈与北塔的关系和因果是什么?",
            provenance=trusted,
            goal_id=goal_id,
            relation_subject="林澈",
            relation_predicate="关联",
            relation_object="北塔",
        ),
        PlanningQuestion(
            question_id=StableId("question.obligation"),
            kind=PlanningQuestionKind.OBLIGATION_PACING,
            question="林澈的北塔义务何时兑现?",
            provenance=trusted,
            goal_id=goal_id,
        ),
        PlanningQuestion(
            question_id=StableId("question.style"),
            kind=PlanningQuestionKind.STYLE_REFERENCE,
            question="历史段落的风格参考是什么?",
            provenance=trusted,
            goal_id=goal_id,
        ),
        PlanningQuestion(
            question_id=StableId("question.human"),
            kind=PlanningQuestionKind.HUMAN_CHOICE,
            question="作者选择哪个方向?",
            provenance=trusted,
            goal_id=goal_id,
        ),
        PlanningQuestion(
            question_id=StableId("question.unknown-goal"),
            kind=PlanningQuestionKind.FACT,
            question="林澈现在如何?",
            provenance=trusted,
            goal_id=StableId("goal.unknown"),
        ),
        PlanningQuestion(
            question_id=StableId("question.ungrounded"),
            kind=PlanningQuestionKind.FACT,
            question="不存在者现在如何?",
            provenance=trusted,
            goal_id=goal_id,
            entity_labels=("不存在者",),
        ),
    )
    inquiry = base.model_copy(update={"questions": questions})
    inquiry_ref = _put(repo, inquiry.model_dump_json(), "application/json")
    review = PlanReview(
        review_id=StableId("review.kinds"),
        target_kind=ReviewTargetKind.INQUIRY,
        target_artifact_ref=inquiry_ref,
        decision=ReviewDecision.ACCEPT,
        receipt=_receipt(AgentMode.CHAPTER_SET, AgentType.PLAN_REVIEWER),
    )
    review_ref = _put(repo, review.model_dump_json(), "application/json")
    result = PlanningInquiryConditionedNeedGenerator().generate(
        inquiry=inquiry,
        inquiry_ref=inquiry_ref,
        review=review,
        review_ref=review_ref,
        world=make_synthetic_bundle().world_roots[0],
        run_id=RunId("run.kinds"),
        task_id=TaskId("task.kinds"),
    )
    assert result.rejection_reasons["question.human"] == "human_choice_is_not_a_memory_fact"
    assert result.rejection_reasons["question.unknown-goal"] == "unknown_goal_lineage"
    generator = PlanningInquiryConditionedNeedGenerator()
    assert (
        generator._routing(PlanningQuestionKind.RELATION_CAUSAL)[0]
        is Stage1QueryIntent.CAUSAL_MULTI_HOP
    )
    assert (
        generator._routing(PlanningQuestionKind.OBLIGATION_PACING)[0]
        is Stage1QueryIntent.PLAN_OBLIGATION
    )
    assert (
        generator._routing(PlanningQuestionKind.STYLE_REFERENCE)[0] is Stage1QueryIntent.STYLE_VOICE
    )
    no_anchor_question = questions[2].model_copy(
        update={"question": "文风和句式参考是什么?", "entity_labels": ()}
    )
    no_anchor_inquiry = base.model_copy(update={"questions": (no_anchor_question,)})
    no_anchor_ref = _put(repo, no_anchor_inquiry.model_dump_json(), "application/json")
    no_anchor_review = review.model_copy(update={"target_artifact_ref": no_anchor_ref})
    no_anchor = generator.generate(
        inquiry=no_anchor_inquiry,
        inquiry_ref=no_anchor_ref,
        review=no_anchor_review,
        review_ref=review_ref,
        world=make_synthetic_bundle().world_roots[0],
        run_id=RunId("run.no-anchor"),
        task_id=TaskId("task.no-anchor"),
    )
    assert no_anchor.inquiry_ref == no_anchor_ref
    wrong_target = review.model_copy(update={"target_artifact_ref": _put(repo, "other")})
    with pytest.raises(PlanningInquiryNeedError, match="target differs"):
        generator.generate(
            inquiry=inquiry,
            inquiry_ref=inquiry_ref,
            review=wrong_target,
            review_ref=review_ref,
            world=make_synthetic_bundle().world_roots[0],
            run_id=RunId("run.kinds"),
            task_id=TaskId("task.kinds"),
        )
    with pytest.raises(PlanningInquiryNeedError, match="inquiry review"):
        generator.generate(
            inquiry=inquiry,
            inquiry_ref=inquiry_ref,
            review=review.model_copy(update={"target_kind": ReviewTargetKind.PLAN_PROPOSAL}),
            review_ref=review_ref,
            world=make_synthetic_bundle().world_roots[0],
            run_id=RunId("run.kinds"),
            task_id=TaskId("task.kinds"),
        )
    bootstrap = _inquiry(AgentMode.PROJECT_BOOTSTRAP, source)
    with pytest.raises(PlanningInquiryNeedError, match="must not call"):
        generator.generate(
            inquiry=bootstrap,
            inquiry_ref=inquiry_ref,
            review=review.model_copy(update={"target_artifact_ref": inquiry_ref}),
            review_ref=review_ref,
            world=make_synthetic_bundle().world_roots[0],
            run_id=RunId("run.kinds"),
            task_id=TaskId("task.kinds"),
        )
    assert generator._scope_for_facet(NeedFacetKind.KNOWLEDGE_BOUNDARY).value == "knowledge"


def test_planner_context_is_consumer_specific_budgeted_and_bootstrap_safe(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    source = _put(repo, "author brief")
    inquiry = _inquiry(AgentMode.PROJECT_BOOTSTRAP, source)
    inquiry_ref = repo.put(inquiry.model_dump_json().encode(), "application/json", VERSION)
    package, artifact = PlannerContextAssembler(repo, schema_version=VERSION).assemble(
        request=_request(AgentMode.PROJECT_BOOTSTRAP, source),
        inquiry=inquiry,
        inquiry_ref=inquiry_ref,
    )

    assert package.base_commit is None
    assert package.stage1_context_ref is None
    assert repo.read_verified(artifact)
    assert {item.section for item in package.items} >= {
        PlannerContextSection.AUTHOR_INTENT,
        PlannerContextSection.WORKING_PROPOSAL,
        PlannerContextSection.UNRESOLVED,
    }
    assert all(item.protected for item in package.items)


def test_post_genesis_context_rejects_wrong_memory_basis(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source = _put(repo, "author brief")
    raw_refs = tuple(_put(repo, f"accepted-{index}") for index in range(3))
    refs = (raw_refs[0], raw_refs[1], raw_refs[2])
    request = _request(AgentMode.CHAPTER_SET, source, accepted=refs)
    inquiry = _inquiry(AgentMode.CHAPTER_SET, source)
    inquiry_ref = repo.put(inquiry.model_dump_json().encode(), "application/json", VERSION)
    wrong = Stage1ContextPackage(
        context_id=StableId("context.wrong"),
        base_commit=CommitId("sha256:" + "2" * 64),
        snapshot_id=StableId("snapshot.stage4"),
        task_contract="stage4",
        budget_report=ContextBudgetReport(
            token_budget=100,
            mandatory_tokens=0,
            optional_tokens=0,
            full_chapter_read_count=0,
        ),
    )
    wrong_ref = repo.put(wrong.model_dump_json().encode(), "application/json", VERSION)

    with pytest.raises(ValueError, match="basis mismatch"):
        PlannerContextAssembler(repo, schema_version=VERSION).assemble(
            request=request,
            inquiry=inquiry,
            inquiry_ref=inquiry_ref,
            stage1_context=wrong,
            stage1_context_ref=wrong_ref,
        )


def test_relation_and_causal_routes_are_anchor_first_then_conditional_graph() -> None:
    for intent in (Stage1QueryIntent.RELATION_CHAIN, Stage1QueryIntent.CAUSAL_MULTI_HOP):
        profile = profile_for(intent, ResolutionTier.R2)
        assert tuple(step.channel for step in profile.primary_groups[0].steps) == (
            RetrievalChannel.ANCHOR_BM25,
            RetrievalChannel.ANCHOR_DENSE,
        )
        assert profile.conditional_fallbacks[0].condition == ("relation_or_causal_facets_unclosed")
        assert tuple(step.channel for step in profile.conditional_fallbacks[0].steps) == (
            RetrievalChannel.TYPED_GRAPH,
        )
    assert RouteBoundControllerPolicy._fallback_applies("relation_or_causal_facets_unclosed", False)
    assert not RouteBoundControllerPolicy._fallback_applies(
        "relation_or_causal_facets_unclosed", True
    )


def test_stage4_domain_validators_fail_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source = _put(repo, "author")
    accepted = tuple(_put(repo, f"root-{index}") for index in range(3))
    bootstrap_inquiry = _inquiry(AgentMode.PROJECT_BOOTSTRAP, source)
    chapter_inquiry = _inquiry(AgentMode.CHAPTER_SET, source)

    invalid_references = (
        {"provenance": PlanningProvenance.AUTHOR_SUPPLIED},
        {
            "provenance": PlanningProvenance.PLANNER_PROPOSED,
            "reference_ids": (StableId("source.brief"),),
        },
    )
    for reference_payload in invalid_references:
        with pytest.raises(ValidationError):
            PlanningReference.model_validate(reference_payload)

    question = chapter_inquiry.questions[0]
    with pytest.raises(ValidationError, match="subject/predicate/object"):
        PlanningQuestion.model_validate(question.model_dump() | {"relation_subject": "林澈"})

    inquiry_mutations = (
        ({"mode": AgentMode.BOOTSTRAP}, "Planner mode"),
        ({"planning_scope": ()}, "planning scope"),
        ({"horizon_end": None}, "bounds"),
        ({"horizon_start": 24}, "precedes"),
        ({"horizon_start": None, "horizon_end": None}, "explicit rolling"),
        ({"generation": 1, "parent_inquiry_id": StableId("parent.bad")}, "cannot have"),
        ({"generation": 2, "parent_inquiry_id": None}, "requires its parent"),
    )
    for inquiry_update, message in inquiry_mutations:
        with pytest.raises(ValidationError, match=message):
            PlanningInquiry.model_validate(chapter_inquiry.model_dump() | inquiry_update)
    with pytest.raises(ValidationError, match="author-approved"):
        PlanningInquiry.model_validate(bootstrap_inquiry.model_dump() | {"author_intent_refs": ()})

    issue = PlanReviewIssue(
        issue_id=StableId("issue.blocking"),
        kind=ReviewIssueKind.COVERAGE,
        summary="missing",
        blocking=True,
    )
    invalid_reviews: tuple[dict[str, object], ...] = (
        {
            "target_kind": ReviewTargetKind.INQUIRY,
            "decision": ReviewDecision.ACCEPT,
            "issues": (issue,),
        },
        {
            "target_kind": ReviewTargetKind.INQUIRY,
            "decision": ReviewDecision.REVISE,
        },
        {
            "target_kind": ReviewTargetKind.INQUIRY,
            "decision": ReviewDecision.HUMAN_REQUIRED,
            "revision_instruction": "not allowed",
        },
    )
    for review_payload in invalid_reviews:
        with pytest.raises(ValidationError):
            PlanReviewDraft.model_validate(review_payload)

    request = _request(
        AgentMode.CHAPTER_SET,
        source,
        accepted=(accepted[0], accepted[1], accepted[2]),
    )
    request_mutations: tuple[tuple[dict[str, object], str], ...] = (
        ({"project_id": ProjectId("project.other")}, "project differs"),
        ({"author_intent_artifacts": ()}, "exact artifact"),
        ({"accepted_plan_ref": None}, "accepted roots"),
        ({"horizon_end": None}, "bounds"),
        ({"horizon_start": 24}, "precedes"),
    )
    for request_update, message in request_mutations:
        with pytest.raises(ValidationError, match=message):
            PlanningLoopRequest.model_validate(request.model_dump() | request_update)
    bootstrap = _request(AgentMode.PROJECT_BOOTSTRAP, source)
    with pytest.raises(ValidationError, match="commit-scoped"):
        PlanningLoopRequest.model_validate(
            bootstrap.model_dump() | {"snapshot_id": StableId("snapshot.bad")}
        )
    with pytest.raises(ValidationError, match="author-approved"):
        raw = bootstrap.model_dump()
        raw["task"]["source_ids"] = ()
        raw["author_intent_artifacts"] = ()
        PlanningLoopRequest.model_validate(raw)

    with pytest.raises(ValidationError, match="mandatory"):
        PlannerContextBudgetReport(token_budget=3, mandatory_tokens=2, selected_tokens=1)
    with pytest.raises(ValidationError, match="token budget"):
        PlannerContextBudgetReport(token_budget=1, mandatory_tokens=1, selected_tokens=2)
    inquiry_ref = _put(repo, bootstrap_inquiry.model_dump_json(), "application/json")
    budget = PlannerContextBudgetReport(token_budget=10, mandatory_tokens=1, selected_tokens=1)
    item = PlannerContextItem(
        context_item_id=StableId("context.unprotected"),
        section=PlannerContextSection.AUTHOR_INTENT,
        text="x",
        token_count=1,
    )
    package_payload = {
        "package_id": StableId("context.package"),
        "contract_version": "v1",
        "project_id": PROJECT,
        "mode": AgentMode.PROJECT_BOOTSTRAP,
        "planning_scope": ("x",),
        "reviewed_inquiry_ref": inquiry_ref,
        "items": (item,),
        "budget_report": budget,
        "rendered_context": "x",
    }
    with pytest.raises(ValidationError, match="must be protected"):
        PlannerContextPackage.model_validate(package_payload)
    with pytest.raises(ValidationError, match="cannot claim"):
        PlannerContextPackage.model_validate(package_payload | {"items": (), "base_commit": BASE})
    with pytest.raises(ValidationError, match="cannot bind Memory"):
        PlannerContextPackage.model_validate(
            package_payload | {"items": (), "stage1_context_ref": inquiry_ref}
        )
    with pytest.raises(ValidationError, match="exact Memory basis"):
        PlannerContextPackage.model_validate(
            package_payload | {"items": (), "mode": AgentMode.STORY}
        )

    projection_payload = {
        "run_id": RunId("run.projection"),
        "task_id": TaskId("task.projection"),
        "seed_ref": inquiry_ref,
        "view_ref": inquiry_ref,
        "generation": 1,
        "basis_event_position": 0,
        "rendered_context": "x",
        "token_count": 1,
        "exposed_context_item_ids": (StableId("item.exposed"),),
    }
    with pytest.raises(ValidationError, match="subset"):
        PlannerContextProjection.model_validate(
            projection_payload | {"used_context_item_ids": (StableId("item.hidden"),)}
        )
    with pytest.raises(ValidationError, match="flag and reason"):
        PlannerContextProjection.model_validate(projection_payload | {"suspended": True})

    with pytest.raises(ValidationError, match="requires a proposal"):
        PlanningTurnOutput(action=PlanningTurnAction.PLAN_READY)
    with pytest.raises(ValidationError, match="requires questions"):
        PlanningTurnOutput(action=PlanningTurnAction.REQUEST_MEMORY)
    proposal = PlanProposal(
        proposal_id=StableId("proposal.ready"),
        project_id=PROJECT,
        mode=AgentMode.PROJECT_BOOTSTRAP,
        strategy=BootstrapStrategy.DEVELOP_CANDIDATES,
        items=(),
        coverage=1.0,
        receipt=_receipt(AgentMode.PROJECT_BOOTSTRAP, AgentType.PLANNER),
    )
    assert (
        PlanningTurnOutput(
            action=PlanningTurnAction.PLAN_READY,
            plan_proposal=proposal,
        ).plan_proposal
        == proposal
    )
    assert PlanningTurnOutput(
        action=PlanningTurnAction.REQUEST_MEMORY,
        memory_questions=("what happened?",),
    ).memory_questions
    with pytest.raises(ValidationError, match="non-degraded"):
        PlanningLoopResult(
            request_id=bootstrap.request_id,
            terminal=PlanningLoopTerminal.PLAN_CANDIDATE_READY,
            proposal=proposal,
            degraded=True,
            plan_review_ref=inquiry_ref,
        )
    with pytest.raises(ValidationError, match="accepted independent review"):
        PlanningLoopResult(
            request_id=bootstrap.request_id,
            terminal=PlanningLoopTerminal.PLAN_CANDIDATE_READY,
            proposal=proposal,
        )


def test_graph_receipt_and_binding_validators(tmp_path: Path) -> None:
    del tmp_path
    evidence = make_synthetic_bundle().world_roots[0].states[0].evidence_refs[0]
    payload = {
        "receipt_id": HASH,
        "base_commit": BASE,
        "snapshot_id": StableId("snapshot.graph"),
        "access_scope": "author_planning",
        "seed_entity_ids": (StableId("entity.a"),),
        "relation_row_ids": (StableId("row.a"),),
        "relation_ids": (StableId("relation.a"),),
        "entity_path": (StableId("entity.a"), StableId("entity.b")),
        "directions": ("outgoing",),
        "edge_semantics": ("canonical",),
        "evidence_refs": (evidence,),
        "depth": 1,
    }
    for update in (
        {"relation_ids": (StableId("relation.a"), StableId("relation.b"))},
        {"entity_path": (StableId("entity.a"), StableId("entity.b"), StableId("entity.c"))},
        {"directions": ("outgoing", "incoming")},
        {"depth": 2},
        {"edge_semantics": ("inferred",)},
    ):
        with pytest.raises(ValidationError):
            TypedGraphPathReceipt.model_validate(payload | update)
    receipt = TypedGraphPathReceipt.model_validate(payload)
    unit = RetrievalUnit(
        unit_id=StableId("unit.graph"),
        unit_kind=RetrievalUnitKind.RELATION_ANCHOR,
        source_commit=BASE,
        snapshot_id=StableId("snapshot.graph"),
        text="A relates to B",
        graph_path_receipt=receipt,
    )
    with pytest.raises(ValidationError, match="basis differs"):
        RetrievalUnit.model_validate(
            unit.model_dump() | {"source_commit": CommitId("sha256:" + "2" * 64)}
        )
    with pytest.raises(ValidationError, match="requires a parent"):
        RetrievalUnit.model_validate(
            unit.model_dump() | {"expanded_from_handle": StableId("compact.a")}
        )
    with pytest.raises(ValidationError, match="bound"):
        ChannelHit(
            unit=unit,
            channel=RetrievalChannel.ANCHOR_BM25,
            channel_rank=1,
            raw_score=1.0,
            candidate_count=1,
            hit_reason="bad channel",
            graph_path_receipt=receipt,
        )


def test_formal_manifest_requires_seven_unique_same_corpus_cases(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source = _put(repo, "author")
    roots = tuple(_put(repo, f"root-{index}") for index in range(3))
    cases = tuple(
        PlanningEvaluationCase(
            case_id=StableId(f"case.{mode.value}"),
            mode=mode,
            request=_request(
                mode,
                source,
                accepted=None
                if mode is AgentMode.PROJECT_BOOTSTRAP
                else (roots[0], roots[1], roots[2]),
            ),
            corpus_fingerprint=HASH,
        )
        for mode in PLANNER_MODES
    )
    manifest = PlanningEvaluationManifest(
        manifest_id=StableId("manifest.valid"),
        schema_version="v1",
        cases=cases,
        configuration_fingerprint=HASH,
        corpus_fingerprint=HASH,
        pilot_fingerprint=HASH,
        rubric_fingerprint=HASH,
        threshold_fingerprint=HASH,
    )
    with pytest.raises(ValidationError, match="exactly one"):
        PlanningEvaluationManifest.model_validate(manifest.model_dump() | {"cases": cases[:-1]})
    bad_case = cases[-1].model_copy(update={"corpus_fingerprint": ArtifactId("sha256:" + "b" * 64)})
    with pytest.raises(ValidationError, match="same frozen corpus"):
        PlanningEvaluationManifest.model_validate(
            manifest.model_dump() | {"cases": (*cases[:-1], bad_case)}
        )


def test_post_genesis_context_preserves_graph_expansion_diversity_and_budget(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    source = _put(repo, "author intent")
    roots = tuple(_put(repo, f"accepted-{index}") for index in range(3))
    request = _request(
        AgentMode.CHAPTER_SET,
        source,
        accepted=(roots[0], roots[1], roots[2]),
    )
    inquiry = _inquiry(AgentMode.CHAPTER_SET, source)
    inquiry = inquiry.model_copy(
        update={
            "assumptions": (
                inquiry.questions[0].model_copy(
                    update={
                        "question_id": StableId("question.nonblocking"),
                        "blocking": False,
                    }
                ),
            )
        }
    )
    inquiry_ref = _put(repo, inquiry.model_dump_json(), "application/json")
    world = make_synthetic_bundle().world_roots[0]
    evidence = world.states[0].evidence_refs[0]
    receipt = TypedGraphPathReceipt(
        receipt_id=ArtifactId("sha256:" + "b" * 64),
        base_commit=BASE,
        snapshot_id=StableId("snapshot.stage4"),
        access_scope="author_planning",
        seed_entity_ids=(StableId("entity.a"),),
        relation_row_ids=(StableId("row.a"),),
        relation_ids=(StableId("relation.a"),),
        entity_path=(StableId("entity.a"), StableId("entity.b")),
        directions=("outgoing",),
        edge_semantics=("evidence",),
        evidence_refs=(evidence,),
        depth=1,
    )

    def unit(
        identity: str,
        text: str,
        unit_kind: RetrievalUnitKind = RetrievalUnitKind.STATE_ANCHOR,
        **updates: object,
    ) -> RetrievalUnit:
        return RetrievalUnit.model_validate(
            {
                "unit_id": StableId(identity),
                "unit_kind": unit_kind,
                "source_commit": BASE,
                "snapshot_id": StableId("snapshot.stage4"),
                "text": text,
                "evidence_refs": (evidence,),
                **updates,
            }
        )

    compact = unit(
        "unit.compact",
        "compact state",
        compact_handle=StableId("compact.handle"),
    )
    graph = unit(
        "unit.graph",
        "relation path",
        unit_kind=RetrievalUnitKind.RELATION_ANCHOR,
        graph_path_receipt=receipt,
    )
    plan = unit("unit.plan", "accepted obligation", information_label="plan", mandatory=True)
    history = unit("unit.history", "historical event", narrative_start=20)
    expanded = unit(
        "unit.expanded",
        "full evidence span",
        unit_kind=RetrievalUnitKind.GROUNDED_SPAN,
        parent_unit_id=compact.unit_id,
        expanded_from_handle=StableId("compact.handle"),
        graph_path_receipt=receipt,
    )
    expanded_without_graph = unit(
        "unit.expanded-plain",
        "second full evidence span",
        unit_kind=RetrievalUnitKind.GROUNDED_SPAN,
        parent_unit_id=compact.unit_id,
        expanded_from_handle=StableId("compact.handle"),
    )
    raw_unselected = unit("unit.raw-unselected", "raw without compact selection")
    context = Stage1ContextPackage(
        context_id=StableId("context.stage4.rich"),
        base_commit=BASE,
        snapshot_id=StableId("snapshot.stage4"),
        task_contract="stage4",
        current_world_state=(compact, graph),
        active_plan_obligations=(plan,),
        relevant_historical_events=(history,),
        raw_evidence_spans=(expanded, expanded_without_graph, raw_unselected),
        unresolved_gaps=("missing pacing fact",),
        budget_report=ContextBudgetReport(
            token_budget=8_000,
            mandatory_tokens=0,
            optional_tokens=0,
            full_chapter_read_count=0,
        ),
    )
    context_ref = _put(repo, context.model_dump_json(), "application/json")
    assembler = PlannerContextAssembler(repo, schema_version=VERSION)
    package, _ = assembler.assemble(
        request=request,
        inquiry=inquiry,
        inquiry_ref=inquiry_ref,
        stage1_context=context,
        stage1_context_ref=context_ref,
    )
    assert package.graph_path_receipt_refs
    assert package.expansion_receipt_refs
    assert package.unresolved_gaps == ("missing pacing fact",)
    assert {item.section for item in package.items} >= {
        PlannerContextSection.ACCEPTED_PLAN,
        PlannerContextSection.RELATION_CAUSAL,
        PlannerContextSection.HISTORY_DEVIATION,
        PlannerContextSection.CURRENT_STATE,
    }
    assert assembler._context_units(context, include_raw=True)[-1] == raw_unselected

    mandatory_tokens = sum(item.token_count for item in package.items if item.mandatory)
    tight_budgets = request.budgets.model_copy(
        update={"context": ContextBudget(token_budget=mandatory_tokens + 1)}
    )
    tight_request = request.model_copy(update={"budgets": tight_budgets})
    tight, _ = assembler.assemble(
        request=tight_request,
        inquiry=inquiry,
        inquiry_ref=inquiry_ref,
        stage1_context=context,
        stage1_context_ref=context_ref,
    )
    assert tight.budget_report.dropped_item_ids
    assert set(tight.budget_report.drop_reasons.values()) == {"optional_token_budget"}

    tiny_budgets = request.budgets.model_copy(update={"context": ContextBudget(token_budget=1)})
    with pytest.raises(ValueError, match="protected Planner context"):
        assembler.assemble(
            request=request.model_copy(update={"budgets": tiny_budgets}),
            inquiry=inquiry,
            inquiry_ref=inquiry_ref,
            stage1_context=context,
            stage1_context_ref=context_ref,
        )
    with pytest.raises(ValueError, match="differs from loop request"):
        assembler.assemble(
            request=request,
            inquiry=inquiry.model_copy(update={"project_id": ProjectId("project.other")}),
            inquiry_ref=inquiry_ref,
            stage1_context=context,
            stage1_context_ref=context_ref,
        )
    with pytest.raises(ValueError, match="requires Memory"):
        assembler.assemble(request=request, inquiry=inquiry, inquiry_ref=inquiry_ref)
    bootstrap = _request(AgentMode.PROJECT_BOOTSTRAP, source)
    with pytest.raises(ValueError, match="cannot consume Memory"):
        assembler.assemble(
            request=bootstrap,
            inquiry=_inquiry(AgentMode.PROJECT_BOOTSTRAP, source),
            inquiry_ref=inquiry_ref,
            stage1_context=context,
            stage1_context_ref=context_ref,
        )
    binary = repo.put(b"\xff", "application/octet-stream", VERSION)
    bad_request = bootstrap.model_copy(update={"author_intent_artifacts": (binary,)})
    with pytest.raises(ValueError, match="not UTF-8"):
        assembler.assemble(
            request=bad_request,
            inquiry=_inquiry(AgentMode.PROJECT_BOOTSTRAP, binary),
            inquiry_ref=inquiry_ref,
        )
