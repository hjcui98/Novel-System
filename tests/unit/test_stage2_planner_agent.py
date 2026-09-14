from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.agents import (
    AgentRegistry,
    PlannerAgent,
    PlannerInvocationError,
    StructuredAgentRunner,
)
from novel_agent.agents.planner import (
    INQUIRY_OUTPUT_CONSTRAINTS,
    PLANNING_TURN_OUTPUT_CONSTRAINTS,
    _materialize_unresolved,
    _ModelPlannerProposalDraft,
    _unresolved_issue_id,
)
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
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.planning import (
    GoalProposal,
    PlanningInquiryDraft,
    PlanningProvenance,
    PlanningReference,
    PlanningTurnAction,
    PlanningTurnDraft,
)
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentSpec,
    AgentType,
    BootstrapStrategy,
    ContractRef,
    PlanDeviationRecordCandidate,
    PlannerExecutionResult,
    PlannerProposalDraft,
    PlanningTask,
    PlanUnresolvedIssueDraft,
    PlanUnresolvedKind,
    PlanUnresolvedOperation,
    PromptContractRef,
    ProposalProvenance,
    ProposedItem,
    SkillContractRef,
    ToolPermission,
    ToolPolicy,
)
from novel_agent.prompts import PromptRegistry, PromptTemplate
from novel_agent.prompts.registry import content_hash
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.skills import SkillRegistry, SkillTemplate

VERSION = SchemaVersion("1.0.0")
HASH = ArtifactId("sha256:" + "a" * 64)
BASE = CommitId("sha256:" + "b" * 64)
PROJECT = ProjectId("project.planner")
ROOT = Path(__file__).parents[2]
MODE_FILES = {
    AgentMode.PROJECT_BOOTSTRAP: (
        "planner_project_bootstrap_v1.md",
        "project_intent_modeling_v1.md",
    ),
    AgentMode.STORY: ("planner_story_v1.md", "story_architecture_v1.md"),
    AgentMode.ARC_VOLUME: ("planner_arc_volume_v1.md", "arc_volume_planning_v1.md"),
    AgentMode.CHAPTER: ("planner_chapter_v1.md", "chapter_goal_decomposition_v1.md"),
    AgentMode.SCENE: ("planner_scene_v1.md", "scene_contract_planning_v1.md"),
    AgentMode.REPLAN: ("planner_replan_v1.md", "plan_deviation_replanning_v1.md"),
}


def artifact(value: ArtifactId = HASH) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=value,
        media_type="text/plain",
        byte_length=6,
        schema_version=VERSION,
    )


def request(mode: AgentMode) -> ModelRequest:
    return ModelRequest(
        request_id=StableId(f"request.planner.{mode.value}"),
        run_id=RunId("run.planner"),
        task_id=TaskId(f"task.planner.{mode.value}"),
        model_role=ModelRole.BATCH_TEST,
        purpose=ModelCallPurpose.BATCH_TEST,
        trace_id=f"trace-planner-{mode.value}",
        prompt="caller prompt",
    )


def task(mode: AgentMode) -> PlanningTask:
    return PlanningTask(
        planning_task_id=StableId(f"planning.{mode.value}"),
        project_id=PROJECT,
        mode=mode,
        base_commit=None if mode is AgentMode.PROJECT_BOOTSTRAP else BASE,
        source_ids=(StableId("source.brief"),),
        creative_scope=("structure",),
        strategy=(
            BootstrapStrategy.NORMALIZE_ONLY if mode is AgentMode.PROJECT_BOOTSTRAP else None
        ),
    )


def item(mode: AgentMode, *, author: bool = False) -> ProposedItem:
    return ProposedItem(
        item_id=StableId(f"plan-item.{mode.value}"),
        kind="plan_node",
        payload={"title": mode.value, "status": "candidate"},
        provenance=(
            ProposalProvenance.AUTHOR_SUPPLIED if author else ProposalProvenance.PLANNER_PROPOSED
        ),
        source_ids=(StableId("source.brief"),) if author else (),
    )


def draft(mode: AgentMode) -> PlannerProposalDraft:
    bootstrap = mode is AgentMode.PROJECT_BOOTSTRAP
    return PlannerProposalDraft(
        mode=mode,
        strategy=BootstrapStrategy.NORMALIZE_ONLY if bootstrap else None,
        project_intent_items=(item(mode, author=True),) if bootstrap else (),
        plan_items=(item(mode, author=bootstrap),),
        world_design_items=(
            ProposedItem(
                item_id=StableId("world-design.bootstrap"),
                kind="world_design",
                payload={"candidate": "baseline"},
                provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                source_ids=(StableId("source.brief"),),
            ),
        )
        if bootstrap
        else (),
        profile_items=(
            ProposedItem(
                item_id=StableId("profile.bootstrap"),
                kind="audience",
                payload={"audience": "adult"},
                provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                source_ids=(StableId("source.brief"),),
            ),
        )
        if bootstrap
        else (),
        deviations=(
            PlanDeviationRecordCandidate(
                deviation_id=StableId("deviation.replan"),
                summary="chapter diverged from planned route",
                affected_plan_item_ids=(StableId("plan-item.chapter"),),
                invalidated_artifact_ids=(HASH,),
                replacement_item_ids=(item(mode).item_id,),
            ),
        )
        if mode is AgentMode.REPLAN
        else (),
        unresolved=(PlanUnresolvedIssueDraft(summary="author choice pending"),),
        coverage=0.8,
    )


def prompt_ref(path: Path, identity: str) -> PromptContractRef:
    digest = content_hash(path.read_bytes())
    return PromptContractRef(
        contract_id=StableId(identity),
        version=VERSION,
        content_hash=digest,
        render_fingerprint=digest,
    )


def harness(
    tmp_path: Path,
    mode: AgentMode,
    output: BaseModel,
) -> tuple[PlannerAgent, FakeModelEndpoint, ArtifactRepository]:
    system_path = ROOT / "src/novel_agent/prompts/system_policy_v1.md"
    prompt_name, skill_name = MODE_FILES[mode]
    task_path = ROOT / "src/novel_agent/prompts" / prompt_name
    skill_path = ROOT / "src/novel_agent/skills" / skill_name
    system = prompt_ref(system_path, "prompt.system-policy")
    mode_prompt = prompt_ref(task_path, f"prompt.planner.{mode.value}")
    skill = SkillContractRef(
        contract_id=StableId(f"skill.planner.{mode.value}"),
        version=VERSION,
        content_hash=content_hash(skill_path.read_bytes()),
    )
    schema = ContractRef(
        contract_id=StableId("schema.planner-proposal-draft"),
        version=VERSION,
        content_hash=HASH,
    )
    spec = AgentSpec(
        agent_id=StableId(f"agent.planner.{mode.value}"),
        agent_type=AgentType.PLANNER,
        mode=mode,
        version=VERSION,
        content_hash=HASH,
        input_schema=schema,
        output_schema=schema,
        system_prompt=system,
        task_prompt=mode_prompt,
        skills=(skill,),
        tool_policy=ToolPolicy(
            policy_id=StableId(f"policy.planner.{mode.value}"),
            version=VERSION,
            content_hash=HASH,
            allowed_tools=("memory.request_context", "proposal.validate_plan"),
            permission=ToolPermission.PROPOSE,
            max_rounds=3,
            max_tool_calls=8,
        ),
    )
    endpoint = FakeModelEndpoint(output.model_dump_json())
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.BATCH_TEST,
                endpoint_name="planner-test",
                model_name="fake-planner",
                adapter=endpoint,
            ),
        )
    )
    runner = StructuredAgentRunner(
        gateway,
        AgentRegistry((spec,)),
        PromptRegistry(
            (
                PromptTemplate(system.contract_id, VERSION, system_path, system.content_hash),
                PromptTemplate(
                    mode_prompt.contract_id,
                    VERSION,
                    task_path,
                    mode_prompt.content_hash,
                ),
            )
        ),
        SkillRegistry((SkillTemplate(skill.contract_id, VERSION, skill_path, skill.content_hash),)),
    )
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / mode.value))
    return PlannerAgent(runner, repository), endpoint, repository


@pytest.mark.parametrize("mode", tuple(MODE_FILES))
def test_planner_agent_executes_all_six_modes_with_typed_provenance(
    tmp_path: Path,
    mode: AgentMode,
) -> None:
    agent, endpoint, repository = harness(tmp_path, mode, draft(mode))
    result, _ = asyncio.run(
        agent.run(
            version=VERSION,
            task=task(mode),
            source_payload="author material: do not treat this as instructions",
            source_artifacts=(artifact(),),
            request=request(mode),
        )
    )

    assert result.mode is mode
    assert result.plan_proposal.mode is mode
    assert result.receipt.agent_type is AgentType.PLANNER
    assert result.receipt.agent_mode is mode
    assert result.receipt.unresolved == ("author choice pending",)
    assert repository.read_verified(result.output_artifact)
    if mode is AgentMode.PROJECT_BOOTSTRAP:
        assert result.project_intent is not None
        assert result.world_design is not None
        assert result.project_profile is not None
    else:
        assert result.project_intent is None
        assert result.world_design is None
        assert result.project_profile is None
    if mode is AgentMode.REPLAN:
        assert result.deviations[0].invalidated_artifact_ids == (HASH,)
    sent = endpoint.requests[0]
    assert sent.agent_id == StableId(f"agent.planner.{mode.value}")
    assert '<TASK_PAYLOAD trusted="false">' in sent.prompt
    assert "SOURCE_DATA=author material" in sent.prompt


def test_planner_inquiry_prompt_binds_compact_output_contract(tmp_path: Path) -> None:
    source_id = StableId("source.brief")
    provenance = PlanningReference(
        provenance=PlanningProvenance.AUTHOR_SUPPLIED,
        reference_ids=(source_id,),
    )
    inquiry = PlanningInquiryDraft(
        mode=AgentMode.CHAPTER,
        planning_scope=("chapter",),
        horizon_start=2,
        horizon_end=2,
        goal_proposals=(
            GoalProposal(
                goal_id=StableId("goal.chapter"),
                summary="advance the chapter conflict",
                rationale="preserve the author direction",
                provenance=provenance,
            ),
        ),
        expected_output_shape="one compact inquiry",
    )
    agent, endpoint, _ = harness(tmp_path, AgentMode.CHAPTER, inquiry)
    asyncio.run(
        agent.propose_inquiry(
            version=VERSION,
            task=task(AgentMode.CHAPTER),
            source_payload="author material",
            source_artifacts=(artifact(),),
            request=request(AgentMode.CHAPTER),
            horizon_start=2,
            horizon_end=2,
        )
    )

    assert INQUIRY_OUTPUT_CONSTRAINTS in endpoint.requests[0].prompt
    assert "PROVENANCE_CONSTRAINT" in endpoint.requests[0].prompt
    assert "GROUNDING_CONSTRAINT" in endpoint.requests[0].prompt
    assert "LINEAGE_CONSTRAINT" in endpoint.requests[0].prompt
    assert "RELATION_CONSTRAINT" in endpoint.requests[0].prompt
    assert "PLANNING_SCOPE_CONSTRAINT" in endpoint.requests[0].prompt
    assert "HORIZON_CONSTRAINT" in endpoint.requests[0].prompt


def test_planner_turn_prompt_binds_memory_grounding(tmp_path: Path) -> None:
    turn = PlanningTurnDraft(
        action=PlanningTurnAction.REQUEST_MEMORY,
        memory_questions=("陈长生当前状态是什么?",),
    )
    agent, endpoint, _ = harness(tmp_path, AgentMode.CHAPTER, turn)
    asyncio.run(
        agent.run_turn(
            version=VERSION,
            task=task(AgentMode.CHAPTER),
            source_payload="WORLD_ENTITY_LABELS=陈长生",
            source_artifacts=(artifact(),),
            request=request(AgentMode.CHAPTER),
        )
    )

    assert PLANNING_TURN_OUTPUT_CONSTRAINTS in endpoint.requests[0].prompt
    assert "WORLD_ENTITY_LABELS" in endpoint.requests[0].prompt
    assert "at most three unique memory_questions" in endpoint.requests[0].prompt


def test_planner_agent_rejects_untrusted_mode_source_and_strategy_changes(tmp_path: Path) -> None:
    mode = AgentMode.STORY
    agent, endpoint, _ = harness(tmp_path, mode, draft(mode))
    with pytest.raises(PlannerInvocationError, match="artifact bindings"):
        asyncio.run(
            agent.run(
                version=VERSION,
                task=task(mode),
                source_payload="data",
                source_artifacts=(),
                request=request(mode),
            )
        )
    wrong_mode = draft(mode).model_copy(update={"mode": AgentMode.CHAPTER})
    wrong_agent, wrong_endpoint, _ = harness(tmp_path, mode, wrong_mode)
    with pytest.raises(PlannerInvocationError, match="mode/strategy"):
        asyncio.run(
            wrong_agent.run(
                version=VERSION,
                task=task(mode),
                source_payload="data",
                source_artifacts=(artifact(),),
                request=request(mode),
            )
        )
    foreign = draft(mode).model_copy(
        update={
            "plan_items": (
                item(mode, author=True).model_copy(
                    update={"source_ids": (StableId("source.foreign"),)}
                ),
            )
        }
    )
    foreign_agent, _, _ = harness(tmp_path, mode, foreign)
    with pytest.raises(PlannerInvocationError, match="outside PlanningTask"):
        asyncio.run(
            foreign_agent.run(
                version=VERSION,
                task=task(mode),
                source_payload="data",
                source_artifacts=(artifact(),),
                request=request(mode),
            )
        )
    assert endpoint.requests == []
    assert len(wrong_endpoint.requests) == 1


def test_planning_contracts_reject_mode_and_provenance_contradictions() -> None:
    bootstrap = task(AgentMode.PROJECT_BOOTSTRAP).model_dump()
    with pytest.raises(ValidationError, match="Planner mode"):
        PlanningTask.model_validate(bootstrap | {"mode": AgentMode.REPLAY})
    with pytest.raises(ValidationError, match="strategy and no base"):
        PlanningTask.model_validate(bootstrap | {"base_commit": BASE})
    story = task(AgentMode.STORY).model_dump()
    with pytest.raises(ValidationError, match="post-Genesis"):
        PlanningTask.model_validate(story | {"base_commit": None})

    base = draft(AgentMode.STORY).model_dump()
    with pytest.raises(ValidationError, match="Planner mode"):
        PlannerProposalDraft.model_validate(base | {"mode": AgentMode.REPLAY})
    with pytest.raises(ValidationError, match="requires a strategy"):
        PlannerProposalDraft.model_validate(
            draft(AgentMode.PROJECT_BOOTSTRAP).model_dump() | {"strategy": None}
        )
    with pytest.raises(ValidationError, match="bootstrap intent"):
        PlannerProposalDraft.model_validate(
            base
            | {
                "strategy": BootstrapStrategy.DEVELOP_CANDIDATES,
                "project_intent_items": (item(AgentMode.STORY),),
            }
        )
    with pytest.raises(ValidationError, match="explicit deviation"):
        PlannerProposalDraft.model_validate(
            draft(AgentMode.REPLAN).model_dump() | {"deviations": ()}
        )
    with pytest.raises(ValidationError, match="only REPLAN"):
        PlannerProposalDraft.model_validate(
            base | {"deviations": draft(AgentMode.REPLAN).deviations}
        )
    with pytest.raises(ValidationError, match="plan items or explicit unresolved"):
        PlannerProposalDraft.model_validate(base | {"plan_items": (), "unresolved": ()})
    normalized = PlannerProposalDraft.model_validate(
        base | {"alternatives": ("A: preserve tension", "B: reduce scope")}
    )
    assert normalized.selection_rationale == (
        "Embedded in alternatives: A: preserve tension | B: reduce scope"
    )
    chapter_goal = ProposedItem(
        item_id=StableId("plan-item.chapter-set-alias"),
        kind="goal",
        payload={"chapter_index": 21, "summary": "Advance the chapter conflict."},
        provenance=ProposalProvenance.PLANNER_PROPOSED,
    )
    aliased = PlannerProposalDraft.model_validate(
        {
            "mode": AgentMode.CHAPTER_SET,
            "project_intent_items": (chapter_goal,),
            "unresolved": ("historical detail remains open",),
            "coverage": 0.6,
        }
    )
    assert aliased.project_intent_items == ()
    assert aliased.plan_items == (chapter_goal,)
    proposed = item(AgentMode.PROJECT_BOOTSTRAP)
    with pytest.raises(ValidationError, match="NORMALIZE_ONLY"):
        PlannerProposalDraft(
            mode=AgentMode.PROJECT_BOOTSTRAP,
            strategy=BootstrapStrategy.NORMALIZE_ONLY,
            project_intent_items=(proposed,),
            plan_items=(proposed,),
            coverage=1,
        )


def test_structured_unresolved_identity_ignores_wording_and_output_position() -> None:
    base = PlanUnresolvedIssueDraft(
        kind=PlanUnresolvedKind.CURRENT_STATE_UNKNOWN,
        summary="状态资料缺失, 不能安全规划第 1 章",
        affected_chapters=(1,),
        blocking=True,
        resolution_owner="MEMORY_CURATOR",
        forbidden_assumptions=("不得把未知状态写成已知事实",),
        source_ids=(StableId("source.brief"),),
    )
    revised_wording = base.model_copy(update={"summary": "当前状态仍无法核验"})

    first = _unresolved_issue_id(base, output_digest="first", index=0)
    second = _unresolved_issue_id(revised_wording, output_digest="second", index=9)

    assert first == second


def test_model_supplied_unresolved_identity_is_rejected_by_materialization() -> None:
    issue = PlanUnresolvedIssueDraft(
        issue_id=StableId("plan-issue.model-owned"),
        kind=PlanUnresolvedKind.CURRENT_STATE_UNKNOWN,
        summary="需要核验当前状态",
        affected_chapters=(1,),
        blocking=True,
    )

    with pytest.raises(PlannerInvocationError, match="may not assign issue_id"):
        _materialize_unresolved((issue,), output_digest="output")


def test_provider_draft_rejects_model_supplied_unresolved_identity() -> None:
    with pytest.raises(ValidationError, match="must omit issue_id"):
        _ModelPlannerProposalDraft.model_validate(
            {
                "mode": AgentMode.ARC_VOLUME,
                "plan_items": (),
                "unresolved": [
                    {
                        "issue_id": "plan-issue.model-owned",
                        "summary": "需要核验当前状态",
                    }
                ],
                "coverage": 0.0,
            }
        )


def test_close_operation_keeps_auditable_history_without_an_active_issue() -> None:
    parent = StableId("plan-issue.draft.existing")
    close = PlanUnresolvedIssueDraft(
        operation=PlanUnresolvedOperation.CLOSE,
        parent_issue_id=parent,
        kind=PlanUnresolvedKind.CURRENT_STATE_UNKNOWN,
        summary="已由宿主核验并关闭状态缺口",
        blocking=True,
        closure_reason="宿主已持久化对应 Memory 证据",
    )

    issues, operations = _materialize_unresolved((close,), output_digest="output")

    assert issues == ()
    assert operations[0].issue_id == parent
    assert operations[0].operation is PlanUnresolvedOperation.CLOSE
    assert operations[0].closure_reason == "宿主已持久化对应 Memory 证据"


def test_an_arc_volume_revision_lands_in_plan_items_not_bootstrap_intent() -> None:
    """The live ARC_VOLUME rejection: correct volumes filed under the wrong container.

    run.yujin-jiuxu.v24 attempt 7 settled ``validation_rejected`` with
    "only PROJECT_BOOTSTRAP may emit bootstrap intent/strategy".  The response held
    eight well-formed ``arc_volume`` items, each declaring ``plan_level: arc_volume``
    and its chapter range, under ``project_intent_items``; every retry reproduced it.
    The alias that repairs this shape existed but was gated to CHAPTER_SET only.
    """

    volumes = tuple(
        ProposedItem(
            item_id=StableId(f"vol-{index}"),
            kind="arc_volume",
            payload={
                "plan_level": "arc_volume",
                "chapter_start": (index - 1) * 100 + 1,
                "chapter_end": index * 100,
            },
            provenance=ProposalProvenance.PLANNER_PROPOSED,
        )
        for index in range(1, 9)
    )
    aliased = PlannerProposalDraft.model_validate(
        {
            "mode": AgentMode.ARC_VOLUME,
            "project_intent_items": volumes,
            "unresolved": ("an open historical detail",),
            "coverage": 1.0,
        }
    )

    assert aliased.project_intent_items == ()
    assert aliased.plan_items == volumes
    assert [item.item_id.root for item in aliased.plan_items] == [
        f"vol-{index}" for index in range(1, 9)
    ]


def test_genuine_bootstrap_intent_is_still_refused_outside_bootstrap() -> None:
    """The alias must not become a way to smuggle bootstrap content past the mode check."""

    genesis_shaped = ProposedItem(
        item_id=StableId("plan.bootstrap.001"),
        kind="plan",
        payload={"title": "黑月坠世三百年后", "description": "故事世界基线"},
        provenance=ProposalProvenance.AUTHOR_SUPPLIED,
        source_ids=(StableId("source.author-intent"),),
    )
    for mode in (AgentMode.STORY, AgentMode.ARC_VOLUME, AgentMode.CHAPTER_SET):
        with pytest.raises(ValidationError, match="bootstrap intent"):
            PlannerProposalDraft.model_validate(
                {
                    "mode": mode,
                    "project_intent_items": (genesis_shaped,),
                    "unresolved": ("an open historical detail",),
                    "coverage": 1.0,
                }
            )
    # A planner-proposed item that declares no plan level is not a plan item either,
    # so the alias cannot be reached by provenance alone.
    levelless = ProposedItem(
        item_id=StableId("plan.levelless"),
        kind="plan",
        payload={"title": "no declared level"},
        provenance=ProposalProvenance.PLANNER_PROPOSED,
    )
    with pytest.raises(ValidationError, match="bootstrap intent"):
        PlannerProposalDraft.model_validate(
            {
                "mode": AgentMode.ARC_VOLUME,
                "project_intent_items": (levelless,),
                "unresolved": ("an open historical detail",),
                "coverage": 1.0,
            }
        )


def test_chapter_set_prompt_binds_each_horizon_chapter_to_a_goal() -> None:
    prompt = (ROOT / "src/novel_agent/prompts/planner_chapter_set_v1.md").read_text(
        encoding="utf-8"
    )

    assert "PLANNING_TASK.creative_scope" in prompt
    assert "exactly one `plan_items` entry" in prompt
    assert "`chapter_index` integer" in prompt
    assert "non-empty `summary` string" in prompt
    assert "`project_intent_items: []`" in prompt
    assert "`strategy: null`" in prompt
    assert "Put missing historical details in `unresolved`" in prompt
    assert "`operation`" in prompt
    assert "`parent_issue_id`" in prompt
    assert "不要输出 `issue_id`" in prompt


def test_planner_result_rejects_receipt_and_mode_contradictions(tmp_path: Path) -> None:
    mode = AgentMode.STORY
    result, _ = asyncio.run(
        harness(tmp_path, mode, draft(mode))[0].run(
            version=VERSION,
            task=task(mode),
            source_payload="data",
            source_artifacts=(artifact(),),
            request=request(mode),
        )
    )
    cases: tuple[tuple[dict[str, object], dict[str, object], str], ...] = (
        ({"agent_type": AgentType.MEMORY_CURATOR}, {}, "Planner receipt"),
        ({"agent_mode": AgentMode.CHAPTER}, {}, "mode must match"),
        ({}, {"receipt": result.receipt.model_copy(update={"unresolved": ()})}, "enclosing"),
    )
    for receipt_update, result_update, message in cases:
        payload = result.model_dump()
        payload["receipt"] = result.receipt.model_copy(update=receipt_update).model_dump()
        payload.update(result_update)
        with pytest.raises(ValidationError, match=message):
            PlannerExecutionResult.model_validate(payload)
    bootstrap_receipt = result.receipt.model_copy(
        update={"agent_mode": AgentMode.PROJECT_BOOTSTRAP}
    )
    bootstrap_plan = result.plan_proposal.model_copy(
        update={"mode": AgentMode.PROJECT_BOOTSTRAP, "receipt": bootstrap_receipt}
    )
    with pytest.raises(ValidationError, match="requires ProjectIntentModel"):
        PlannerExecutionResult.model_validate(
            result.model_dump()
            | {
                "mode": AgentMode.PROJECT_BOOTSTRAP,
                "plan_proposal": bootstrap_plan,
                "receipt": bootstrap_receipt,
            }
        )
    bootstrap_result, _ = asyncio.run(
        harness(
            tmp_path,
            AgentMode.PROJECT_BOOTSTRAP,
            draft(AgentMode.PROJECT_BOOTSTRAP),
        )[0].run(
            version=VERSION,
            task=task(AgentMode.PROJECT_BOOTSTRAP),
            source_payload="data",
            source_artifacts=(artifact(),),
            request=request(AgentMode.PROJECT_BOOTSTRAP),
        )
    )
    story_receipt = bootstrap_result.receipt.model_copy(update={"agent_mode": AgentMode.STORY})
    story_plan = bootstrap_result.plan_proposal.model_copy(
        update={"mode": AgentMode.STORY, "receipt": story_receipt}
    )
    with pytest.raises(ValidationError, match="only PROJECT_BOOTSTRAP"):
        PlannerExecutionResult.model_validate(
            bootstrap_result.model_dump()
            | {
                "mode": AgentMode.STORY,
                "plan_proposal": story_plan,
                "receipt": story_receipt,
            }
        )
    replan_receipt = result.receipt.model_copy(update={"agent_mode": AgentMode.REPLAN})
    replan_plan = result.plan_proposal.model_copy(
        update={"mode": AgentMode.REPLAN, "receipt": replan_receipt}
    )
    with pytest.raises(ValidationError, match="requires deviation"):
        PlannerExecutionResult.model_validate(
            result.model_dump()
            | {
                "mode": AgentMode.REPLAN,
                "plan_proposal": replan_plan,
                "receipt": replan_receipt,
            }
        )


def test_proposed_item_lifts_nested_source_ids_from_payload() -> None:
    raw = {
        "item_id": "plan.story.premise",
        "kind": "premise",
        "payload": {
            "summary": "hero premise",
            "source_ids": ["source.author-intent.123", "source.author-intent.456"],
        },
        "provenance": "author_supplied",
    }
    item = ProposedItem.model_validate_json(json.dumps(raw))
    assert item.source_ids == (
        StableId("source.author-intent.123"),
        StableId("source.author-intent.456"),
    )
    assert "source_ids" not in item.payload
    assert item.payload == {"summary": "hero premise"}
