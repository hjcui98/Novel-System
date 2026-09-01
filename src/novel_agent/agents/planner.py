"""Version-pinned seven-mode Planner facade and Stage 4 contract bundle."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from novel_agent.agents.registry import AgentRegistry, seal_agent_spec
from novel_agent.agents.runner import PreparedAgentRun, StructuredAgentRunner
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.planning import (
    PlanningInquiry,
    PlanningInquiryDraft,
    PlanningTurnAction,
    PlanningTurnDraft,
    PlanningTurnOutput,
)
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentSpec,
    AgentType,
    ContractRef,
    PlannerExecutionResult,
    PlannerProposalDraft,
    PlanningTask,
    PlanProposal,
    ProjectIntentModel,
    ProjectProfileProposal,
    PromptContractRef,
    ProposalProvenance,
    SkillContractRef,
    ToolPermission,
    ToolPolicy,
    WorldDesignProposal,
)
from novel_agent.domain.text import EvidenceRef
from novel_agent.prompts.registry import PromptRegistry, PromptTemplate, content_hash
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.skills.registry import SkillRegistry, SkillTemplate

PLANNER_MODES = (
    AgentMode.PROJECT_BOOTSTRAP,
    AgentMode.STORY,
    AgentMode.ARC_VOLUME,
    AgentMode.CHAPTER_SET,
    AgentMode.CHAPTER,
    AgentMode.SCENE,
    AgentMode.REPLAN,
)
DEFAULT_PLANNER_CONTRACT_VERSION = SchemaVersion("1.0.0")

INQUIRY_OUTPUT_CONSTRAINTS = (
    "OUTPUT_CONSTRAINTS=Return only compact JSON matching the schema. Do not quote or restate "
    "SOURCE_DATA; do not emit markdown, reasoning, or commentary outside JSON. Use at most "
    "three goal_proposals, three assumptions, and three questions, and keep every free-text "
    "field under 240 characters. PROVENANCE_CONSTRAINT=For every goal_proposals, assumptions, "
    'and questions item, set provenance exactly to {"provenance":"planner_proposed", '
    '"reference_ids":[],"artifact_refs":[]}; never put source IDs in those arrays and '
    "never use author_supplied, accepted_plan_derived, canon_derived, or reviewer_derived. "
    "GROUNDING_CONSTRAINT=For fact or relation questions, use exact labels from "
    "WORLD_ENTITY_LABELS in entity_labels or relation_subject/relation_object; never invent "
    "translated labels that are not listed. "
    "LINEAGE_CONSTRAINT=For every assumptions and questions item, goal_id must exactly match "
    "one of the goal_proposals goal_id values; never use the item's own question_id as goal_id. "
    "RELATION_CONSTRAINT=For every assumptions and questions item, either omit "
    "relation_subject, relation_predicate, and relation_object entirely, or provide all three; "
    "never provide only one or two relation fields. PLANNING_SCOPE_CONSTRAINT=planning_scope "
    "must contain at least one string; for CHAPTER_SET include exactly the horizon scope "
    "chapters:{horizon_start}-{horizon_end}. HORIZON_CONSTRAINT=Always include numeric "
    "horizon_start and horizon_end, copying the HORIZON values exactly; never omit them."
)

PLANNING_TURN_OUTPUT_CONSTRAINTS = (
    "TURN_OUTPUT_CONSTRAINTS=Return only compact JSON matching the schema. If action is "
    "REQUEST_MEMORY, every memory_questions item must be a concrete fact or relation question "
    "and must copy at least one exact internal label or alias from WORLD_ENTITY_LABELS in "
    "SOURCE_DATA; never use translated or generic descriptors when an exact label is available. "
    "Keep the request bounded to at most three unique memory_questions. "
    "For a relation question, use exact labels for every named subject and object. Do not emit "
    "markdown, reasoning, or commentary outside JSON."
)

_MODE_ASSETS = {
    AgentMode.PROJECT_BOOTSTRAP: (
        "stage4_planner_project_bootstrap_v1.md",
        "project_intent_modeling_v1.md",
    ),
    AgentMode.STORY: ("stage4_planner_story_v1.md", "story_architecture_v1.md"),
    AgentMode.ARC_VOLUME: (
        "stage4_planner_arc_volume_v1.md",
        "arc_volume_planning_v1.md",
    ),
    AgentMode.CHAPTER_SET: ("planner_chapter_set_v1.md", "chapter_set_planning_v1.md"),
    AgentMode.CHAPTER: (
        "stage4_planner_chapter_v1.md",
        "chapter_goal_decomposition_v1.md",
    ),
    AgentMode.SCENE: ("stage4_planner_scene_v1.md", "scene_contract_planning_v1.md"),
    AgentMode.REPLAN: ("stage4_planner_replan_v1.md", "plan_deviation_replanning_v1.md"),
}


@dataclass(frozen=True, slots=True)
class PlannerContractBundle:
    agents: AgentRegistry
    prompts: PromptRegistry
    skills: SkillRegistry
    agent_specs: tuple[AgentSpec, ...]
    configuration_fingerprint: ArtifactId


def build_planner_contract_bundle(
    *,
    package_root: Path | None = None,
    version: SchemaVersion = DEFAULT_PLANNER_CONTRACT_VERSION,
) -> PlannerContractBundle:
    """Build the only production registration path for Planner and Reviewer."""

    root = package_root or Path(__file__).parents[1]
    prompt_root = root / "prompts"
    skill_root = root / "skills"
    system_path = prompt_root / "system_policy_v1.md"
    reviewer_prompt_path = prompt_root / "plan_reviewer_v1.md"
    inquiry_skill_path = skill_root / "planning_inquiry_v1.md"
    reviewer_skill_path = skill_root / "plan_review_v1.md"
    shared_planner_skill_paths = (
        ("skill.alternative-comparison", skill_root / "alternative_comparison_v1.md"),
        ("skill.obligation-scheduling", skill_root / "obligation_scheduling_v1.md"),
        (
            "skill.character-arc-hook-payoff",
            skill_root / "character_arc_hook_payoff_planning_v1.md",
        ),
    )
    prompt_templates: dict[tuple[str, str], PromptTemplate] = {}
    skill_templates: dict[tuple[str, str], SkillTemplate] = {}

    def prompt_ref(identity: str, path: Path) -> PromptContractRef:
        digest = content_hash(path.read_bytes())
        prompt_templates[(identity, version.root)] = PromptTemplate(
            StableId(identity), version, path, digest
        )
        return PromptContractRef(
            contract_id=StableId(identity),
            version=version,
            content_hash=digest,
            render_fingerprint=digest,
        )

    def skill_ref(identity: str, path: Path) -> SkillContractRef:
        digest = content_hash(path.read_bytes())
        skill_templates[(identity, version.root)] = SkillTemplate(
            StableId(identity), version, path, digest
        )
        return SkillContractRef(
            contract_id=StableId(identity),
            version=version,
            content_hash=digest,
        )

    system = prompt_ref("prompt.system-policy", system_path)
    reviewer_prompt = prompt_ref("prompt.plan-reviewer", reviewer_prompt_path)
    inquiry_skill = skill_ref("skill.planning-inquiry", inquiry_skill_path)
    reviewer_skill = skill_ref("skill.plan-review", reviewer_skill_path)
    shared_planner_skills = tuple(
        skill_ref(identity, path) for identity, path in shared_planner_skill_paths
    )
    planning_input = ContractRef(
        contract_id=StableId("schema.planning-loop-request"),
        version=version,
        content_hash=content_id({"schema": "PlanningLoopRequest", "version": version.root}),
    )
    planner_output = ContractRef(
        contract_id=StableId("schema.planner-structured-output"),
        version=version,
        content_hash=content_id(
            {
                "schemas": (
                    "PlanningInquiryDraft",
                    "PlannerProposalDraft",
                    "PlanningTurnDraft",
                ),
                "version": version.root,
            }
        ),
    )
    review_output = ContractRef(
        contract_id=StableId("schema.plan-review-draft"),
        version=version,
        content_hash=content_id({"schema": "PlanReviewDraft", "version": version.root}),
    )
    zero = ArtifactId("sha256:" + "0" * 64)
    specs: list[AgentSpec] = []
    for mode in PLANNER_MODES:
        prompt_name, skill_name = _MODE_ASSETS[mode]
        mode_prompt = prompt_ref(f"prompt.planner.{mode.value}", prompt_root / prompt_name)
        mode_skill = skill_ref(f"skill.planner.{mode.value}", skill_root / skill_name)
        planner_policy = ToolPolicy(
            policy_id=StableId(f"policy.planner.{mode.value}"),
            version=version,
            content_hash=zero,
            allowed_tools=("memory.request_context", "proposal.validate_plan"),
            denied_tools=(
                "memory.write",
                "plan_root.write",
                "world_root.write",
                "text_root.write",
                "commit",
            ),
            permission=ToolPermission.PROPOSE,
            max_rounds=3,
            max_tool_calls=8,
        )
        specs.append(
            seal_agent_spec(
                AgentSpec(
                    agent_id=StableId(f"agent.planner.{mode.value}"),
                    agent_type=AgentType.PLANNER,
                    mode=mode,
                    version=version,
                    content_hash=zero,
                    input_schema=planning_input,
                    output_schema=planner_output,
                    system_prompt=system,
                    task_prompt=mode_prompt,
                    skills=(inquiry_skill, mode_skill, *shared_planner_skills),
                    tool_policy=planner_policy,
                )
            )
        )
        reviewer_policy = ToolPolicy(
            policy_id=StableId(f"policy.plan-reviewer.{mode.value}"),
            version=version,
            content_hash=zero,
            allowed_tools=("proposal.validate_plan",),
            denied_tools=(
                "retrieval.query",
                "memory.write",
                "plan_root.write",
                "world_root.write",
                "text_root.write",
                "commit",
            ),
            permission=ToolPermission.READ,
            max_rounds=1,
            max_tool_calls=1,
        )
        specs.append(
            seal_agent_spec(
                AgentSpec(
                    agent_id=StableId(f"agent.plan-reviewer.{mode.value}"),
                    agent_type=AgentType.PLAN_REVIEWER,
                    mode=mode,
                    version=version,
                    content_hash=zero,
                    input_schema=planning_input,
                    output_schema=review_output,
                    system_prompt=system,
                    task_prompt=reviewer_prompt,
                    skills=(reviewer_skill,),
                    tool_policy=reviewer_policy,
                )
            )
        )
    sealed = tuple(specs)
    return PlannerContractBundle(
        agents=AgentRegistry(sealed),
        prompts=PromptRegistry(prompt_templates.values()),
        skills=SkillRegistry(skill_templates.values()),
        agent_specs=sealed,
        configuration_fingerprint=content_id(
            tuple(spec.model_dump(mode="json") for spec in sealed)
        ),
    )


class PlannerInvocationError(ValueError):
    pass


class PlannerAgent:
    def __init__(
        self,
        runner: StructuredAgentRunner,
        artifacts: ArtifactRepository,
    ) -> None:
        self._runner = runner
        self._artifacts = artifacts

    async def run(
        self,
        *,
        version: SchemaVersion,
        task: PlanningTask,
        source_payload: str,
        source_artifacts: tuple[ArtifactRef, ...],
        request: ModelRequest,
        trusted_context_artifacts: tuple[ArtifactRef, ...] = (),
        reviewed_inquiry_ref: ArtifactRef | None = None,
        memory_need_ids: tuple[StableId, ...] = (),
        evidence_refs: tuple[EvidenceRef, ...] = (),
        graph_path_receipt_refs: tuple[ArtifactRef, ...] = (),
        parent_proposal_id: StableId | None = None,
    ) -> tuple[PlannerExecutionResult, ModelCallRecord]:
        if len(source_artifacts) != len(task.source_ids) or len(
            {artifact.artifact_id for artifact in source_artifacts}
        ) != len(source_artifacts):
            raise PlannerInvocationError("PlanningTask sources require unique artifact bindings")
        prepared = self._runner.prepare(
            AgentType.PLANNER,
            task.mode,
            version.root,
            request,
            f"PLANNING_PHASE=plan\nPLANNING_TASK={task.model_dump_json()}\n"
            f"SOURCE_DATA={source_payload}",
            source_hashes=tuple(artifact.artifact_id for artifact in source_artifacts),
            input_artifacts=(*source_artifacts, *trusted_context_artifacts),
            base_commit=task.base_commit,
        )
        execution = await self._runner.execute(prepared, PlannerProposalDraft)
        result = self._materialize_plan(
            version=version,
            task=task,
            draft=execution.output,
            prepared=prepared,
            model_call=execution.model_call,
            reviewed_inquiry_ref=reviewed_inquiry_ref,
            memory_need_ids=memory_need_ids,
            evidence_refs=evidence_refs,
            graph_path_receipt_refs=graph_path_receipt_refs,
            parent_proposal_id=parent_proposal_id,
        )
        return result, execution.model_call

    async def run_turn(
        self,
        *,
        version: SchemaVersion,
        task: PlanningTask,
        source_payload: str,
        source_artifacts: tuple[ArtifactRef, ...],
        request: ModelRequest,
        trusted_context_artifacts: tuple[ArtifactRef, ...] = (),
        reviewed_inquiry_ref: ArtifactRef | None = None,
        memory_need_ids: tuple[StableId, ...] = (),
        evidence_refs: tuple[EvidenceRef, ...] = (),
        graph_path_receipt_refs: tuple[ArtifactRef, ...] = (),
        parent_proposal_id: StableId | None = None,
    ) -> tuple[PlanningTurnOutput, PlannerExecutionResult | None, ModelCallRecord]:
        """Run one autonomous Planner turn without granting direct retrieval access."""

        if len(source_artifacts) != len(task.source_ids) or len(
            {artifact.artifact_id for artifact in source_artifacts}
        ) != len(source_artifacts):
            raise PlannerInvocationError("PlanningTask sources require unique artifact bindings")
        prepared = self._runner.prepare(
            AgentType.PLANNER,
            task.mode,
            version.root,
            request,
            f"PLANNING_PHASE=plan_turn\nPLANNING_TASK={task.model_dump_json()}\n"
            "Return PLAN_READY with plan_proposal_draft, or REQUEST_MEMORY with only "
            f"memory_questions.\n{PLANNING_TURN_OUTPUT_CONSTRAINTS}\nSOURCE_DATA={source_payload}",
            source_hashes=tuple(artifact.artifact_id for artifact in source_artifacts),
            input_artifacts=(*source_artifacts, *trusted_context_artifacts),
            base_commit=task.base_commit,
        )
        execution = await self._runner.execute(prepared, PlanningTurnDraft)
        draft = execution.output
        if draft.action is PlanningTurnAction.REQUEST_MEMORY:
            output_artifact = self._artifacts.put(
                canonical_json_bytes(draft.model_dump(mode="json")),
                "application/vnd.novel-agent.planning-turn-draft+json",
                version,
            )
            self._runner.receipt(
                prepared,
                execution.model_call,
                output_artifacts=(output_artifact,),
                unresolved=draft.unresolved,
            )
            return (
                PlanningTurnOutput(
                    action=draft.action,
                    memory_questions=draft.memory_questions,
                    assumptions=draft.assumptions,
                    unresolved=draft.unresolved,
                    selected_skill_ids=draft.selected_skill_ids,
                    used_context_item_ids=draft.used_context_item_ids,
                ),
                None,
                execution.model_call,
            )
        assert draft.plan_proposal_draft is not None
        result = self._materialize_plan(
            version=version,
            task=task,
            draft=draft.plan_proposal_draft,
            prepared=prepared,
            model_call=execution.model_call,
            reviewed_inquiry_ref=reviewed_inquiry_ref,
            memory_need_ids=memory_need_ids,
            evidence_refs=evidence_refs,
            graph_path_receipt_refs=graph_path_receipt_refs,
            parent_proposal_id=parent_proposal_id,
        )
        return (
            PlanningTurnOutput(
                action=PlanningTurnAction.PLAN_READY,
                plan_proposal=result.plan_proposal,
                assumptions=draft.assumptions,
                unresolved=draft.unresolved,
                selected_skill_ids=draft.selected_skill_ids,
                used_context_item_ids=draft.used_context_item_ids,
            ),
            result,
            execution.model_call,
        )

    def _materialize_plan(
        self,
        *,
        version: SchemaVersion,
        task: PlanningTask,
        draft: PlannerProposalDraft,
        prepared: PreparedAgentRun,
        model_call: ModelCallRecord,
        reviewed_inquiry_ref: ArtifactRef | None,
        memory_need_ids: tuple[StableId, ...],
        evidence_refs: tuple[EvidenceRef, ...],
        graph_path_receipt_refs: tuple[ArtifactRef, ...],
        parent_proposal_id: StableId | None,
    ) -> PlannerExecutionResult:
        if draft.mode is not task.mode or draft.strategy is not task.strategy:
            raise PlannerInvocationError("Planner draft mode/strategy differs from trusted task")
        allowed_sources = set(task.source_ids)
        authored_items = (
            *draft.project_intent_items,
            *draft.plan_items,
            *draft.world_design_items,
            *draft.profile_items,
        )
        if any(
            item.provenance is ProposalProvenance.AUTHOR_SUPPLIED
            and not set(item.source_ids).issubset(allowed_sources)
            for item in authored_items
        ):
            raise PlannerInvocationError("Planner draft cites a source outside PlanningTask")
        output_artifact = self._artifacts.put(
            canonical_json_bytes(draft.model_dump(mode="json")),
            "application/vnd.novel-agent.planner-proposal-draft+json",
            version,
        )
        receipt = self._runner.receipt(
            prepared,
            model_call,
            output_artifacts=(output_artifact,),
            unresolved=draft.unresolved,
        )
        digest = output_artifact.artifact_id.root.removeprefix("sha256:")[:24]
        plan = PlanProposal(
            proposal_id=StableId(f"plan-proposal.{digest}"),
            project_id=task.project_id,
            mode=task.mode,
            strategy=task.strategy,
            base_commit=task.base_commit,
            items=draft.plan_items,
            unresolved=draft.unresolved,
            coverage=draft.coverage,
            receipt=receipt,
            reviewed_inquiry_ref=reviewed_inquiry_ref,
            memory_need_ids=memory_need_ids,
            evidence_refs=evidence_refs,
            graph_path_receipt_refs=graph_path_receipt_refs,
            parent_proposal_id=parent_proposal_id,
        )
        intent = (
            ProjectIntentModel(
                intent_id=StableId(f"project-intent.{digest}"),
                project_id=task.project_id,
                strategy=task.strategy,
                items=draft.project_intent_items,
                source_ids=task.source_ids,
                unresolved=draft.unresolved,
                coverage=draft.coverage,
            )
            if task.strategy is not None
            else None
        )
        world = (
            WorldDesignProposal(
                proposal_id=StableId(f"world-design.{digest}"),
                project_id=task.project_id,
                items=draft.world_design_items,
                unresolved=draft.unresolved,
            )
            if draft.world_design_items
            else None
        )
        profile = (
            ProjectProfileProposal(
                proposal_id=StableId(f"profile-proposal.{digest}"),
                project_id=task.project_id,
                items=draft.profile_items,
                unresolved=draft.unresolved,
            )
            if draft.profile_items
            else None
        )
        return PlannerExecutionResult(
            mode=task.mode,
            project_intent=intent,
            plan_proposal=plan,
            world_design=world,
            project_profile=profile,
            deviations=draft.deviations,
            output_artifact=output_artifact,
            receipt=receipt,
        )

    async def propose_inquiry(
        self,
        *,
        version: SchemaVersion,
        task: PlanningTask,
        source_payload: str,
        source_artifacts: tuple[ArtifactRef, ...],
        request: ModelRequest,
        horizon_start: int | None = None,
        horizon_end: int | None = None,
        explicit_overrides: tuple[str, ...] = (),
        parent_inquiry_id: StableId | None = None,
        generation: int | None = None,
    ) -> tuple[PlanningInquiry, ArtifactRef, AgentExecutionReceipt, ModelCallRecord]:
        if len(source_artifacts) != len(task.source_ids) or len(
            {artifact.artifact_id for artifact in source_artifacts}
        ) != len(source_artifacts):
            raise PlannerInvocationError("PlanningTask sources require unique artifact bindings")
        prepared = self._runner.prepare(
            AgentType.PLANNER,
            task.mode,
            version.root,
            request,
            (
                "PLANNING_PHASE=inquiry\n"
                f"PLANNING_TASK={task.model_dump_json()}\n"
                f"HORIZON={horizon_start}:{horizon_end}\n"
                f"AUTHOR_OVERRIDES={explicit_overrides}\n"
                f"{INQUIRY_OUTPUT_CONSTRAINTS}\n"
                f"SOURCE_DATA={source_payload}"
            ),
            source_hashes=tuple(artifact.artifact_id for artifact in source_artifacts),
            input_artifacts=source_artifacts,
            base_commit=task.base_commit,
        )
        execution = await self._runner.execute(prepared, PlanningInquiryDraft)
        draft = execution.output
        if draft.mode is not task.mode:
            raise PlannerInvocationError("Planning inquiry mode differs from trusted task")
        if (draft.horizon_start, draft.horizon_end) != (horizon_start, horizon_end):
            raise PlannerInvocationError("Planning inquiry horizon differs from trusted request")
        allowed_sources = set(task.source_ids)
        references = (
            *(item.provenance for item in draft.goal_proposals),
            *(item.provenance for item in draft.assumptions),
            *(item.provenance for item in draft.questions),
        )
        if any(
            reference.provenance.value == "author_supplied"
            and not set(reference.reference_ids).issubset(allowed_sources)
            for reference in references
        ):
            raise PlannerInvocationError("Planning inquiry cites a foreign author source")
        identity = content_id(
            {
                "task": task.model_dump(mode="json"),
                "draft": draft.model_dump(mode="json"),
                "parent": None if parent_inquiry_id is None else parent_inquiry_id.root,
            }
        ).root.removeprefix("sha256:")[:24]
        if parent_inquiry_id is None:
            if generation not in {None, 1}:
                raise PlannerInvocationError("Initial planning inquiry must use generation 1")
            resolved_generation = 1
        else:
            resolved_generation = 2 if generation is None else generation
        if resolved_generation < 1 or (parent_inquiry_id is not None and resolved_generation < 2):
            raise PlannerInvocationError("Planning inquiry generation is inconsistent")
        inquiry = PlanningInquiry(
            inquiry_id=StableId(f"planning-inquiry.{identity}"),
            project_id=task.project_id,
            mode=task.mode,
            planning_scope=draft.planning_scope,
            horizon_start=draft.horizon_start,
            horizon_end=draft.horizon_end,
            author_intent_refs=source_artifacts,
            explicit_overrides=explicit_overrides,
            goal_proposals=draft.goal_proposals,
            alternatives=draft.alternatives,
            assumptions=draft.assumptions,
            questions=draft.questions,
            decision_criteria=draft.decision_criteria,
            expected_output_shape=draft.expected_output_shape,
            human_choices=draft.human_choices,
            parent_inquiry_id=parent_inquiry_id,
            generation=resolved_generation,
        )
        output_artifact = self._artifacts.put(
            canonical_json_bytes(inquiry.model_dump(mode="json")),
            "application/vnd.novel-agent.planning-inquiry+json",
            version,
        )
        receipt = self._runner.receipt(
            prepared,
            execution.model_call,
            output_artifacts=(output_artifact,),
            unresolved=inquiry.human_choices,
        )
        return inquiry, output_artifact, receipt, execution.model_call
