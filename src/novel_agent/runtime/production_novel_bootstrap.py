"""Two-step novel initialization that reuses existing U1-U8 bootstrap owners."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, JsonValue
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.agents.curator_bootstrap import CuratorBootstrapAgent
from novel_agent.agents.planner import PlannerAgent, build_planner_contract_bundle
from novel_agent.agents.registry import AgentRegistry, seal_agent_spec, seal_tool_policy
from novel_agent.agents.runner import StructuredAgentRunner
from novel_agent.domain.artifacts import ArtifactRef, RootManifest
from novel_agent.domain.base import DomainModel
from novel_agent.domain.benchmark import ChapterGoal, PlanRootDocument, TextRootDocument
from novel_agent.domain.changes import ValidationReport
from novel_agent.domain.creative_runtime import (
    AutomationMode,
    CreativeRunPolicy,
    CreativeRunRequest,
)
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
    bounded_stable_id,
)
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.model_calls import (
    BudgetResolutionProfile,
    ModelCallPurpose,
    ModelRequest,
    ModelRole,
)
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentSpec,
    AgentType,
    AuthorApprovalDecision,
    AuthorApprovalRequest,
    AuthorApprovalStatus,
    BootstrapStrategy,
    ContractRef,
    PlannerExecutionResult,
    PlanningTask,
    PlanProposal,
    ProjectProfileRootDocument,
    PromptContractRef,
    ReferenceAsset,
    ReferenceRootDocument,
    SkillContractRef,
    SourceClass,
    SourceClassification,
    ToolPermission,
    ToolPolicy,
    WorldPatchCandidate,
)
from novel_agent.domain.world import Entity, PlanNode, StateRecord, StoryTime, TruthClass
from novel_agent.prompts.registry import PromptRegistry, PromptTemplate, content_hash
from novel_agent.runtime.production_dispatch_coordinator import ProductionRunDescriptor
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.bootstrap import (
    BootstrapIngestionService,
    IngestedBootstrapSource,
    RawBootstrapSource,
)
from novel_agent.services.bootstrap_workflow import (
    BootstrapCrossRootValidator,
    BootstrapRootBuilder,
    BootstrapRootCandidates,
    GenesisCoordinator,
    SqlAuthorApprovalRepository,
    project_profile_root_content_id,
    reference_root_content_id,
)
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import (
    canonical_json_bytes,
    content_id,
    plan_root_content_id,
    world_root_content_id,
)
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.projection import snapshot_id_for_commit
from novel_agent.skills.registry import SkillRegistry, SkillTemplate

PREPARED_BOOTSTRAP_MEDIA_TYPE = "application/vnd.novel-agent.production-bootstrap-prepared+json"
PREPARED_BOOTSTRAP_CONTRACT = "production_novel_bootstrap.prepared.v1"
ZERO_COMMIT = CommitId("sha256:" + "0" * 64)
ZERO_HASH = ArtifactId("sha256:" + "0" * 64)
VERSION = SchemaVersion("1.0.0")

PlannerBootstrap = Callable[[], Awaitable[PlannerExecutionResult]]
CuratorBootstrap = Callable[[], Awaitable[WorldPatchCandidate]]


class PreparedNovelBootstrapDocument(DomainModel):
    contract_version: Literal["production_novel_bootstrap.prepared.v1"] = (
        "production_novel_bootstrap.prepared.v1"
    )
    project_id: ProjectId
    bootstrap_bundle_id: StableId
    manifest: RootManifest
    text: TextRootDocument
    plan: PlanRootDocument
    world: WorldRootDocument
    reference: ReferenceRootDocument
    profile: ProjectProfileRootDocument
    plan_proposal: PlanProposal
    world_patch: WorldPatchCandidate
    classifications: tuple[SourceClassification, ...]
    validation: ValidationReport
    approval_request: AuthorApprovalRequest
    preview: dict[str, JsonValue] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PreparedNovelBootstrap:
    document: PreparedNovelBootstrapDocument
    artifact: ArtifactRef
    candidates: BootstrapRootCandidates


class ProductionNovelBootstrap:
    """Prepare and commit a Genesis novel without the Stage 0 demo graph."""

    def __init__(
        self,
        *,
        artifacts: ArtifactRepository,
        session_factory: sessionmaker[Session],
        planner: PlannerBootstrap | None = None,
        curator: CuratorBootstrap | None = None,
        schema_version: SchemaVersion = VERSION,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._artifacts = artifacts
        self._session_factory = session_factory
        self._planner = planner
        self._curator = curator
        self._schema_version = schema_version
        self._clock = clock or (lambda: datetime.now(UTC))

    async def prepare(
        self,
        *,
        project_id: ProjectId,
        brief_text: str,
    ) -> PreparedNovelBootstrap:
        if self._planner is None or self._curator is None:
            raise ValueError("bootstrap prepare requires Planner and Curator owners")
        if not brief_text.strip():
            raise ValueError("bootstrap prepare requires a non-empty author brief")
        ingestion = BootstrapIngestionService(self._artifacts)
        bundle_id = bounded_stable_id(
            f"bootstrap.{project_id.root}",
            f"bootstrap.{content_id({'project': project_id.root, 'brief': brief_text}).root}",
        )
        bundle, ingested = ingestion.ingest(
            project_id,
            bundle_id,
            (
                RawBootstrapSource(
                    source_id=StableId("source.author-initial-brief"),
                    source_class=SourceClass.AUTHOR_INITIAL_BRIEF,
                    media_type="text/plain",
                    data=brief_text.encode("utf-8"),
                ),
            ),
            self._schema_version,
        )
        planner_result = await self._planner()
        world_patch = await self._curator()
        if planner_result.plan_proposal.project_id != project_id:
            raise ValueError("Planner bootstrap proposal belongs to another project")
        if world_patch.project_id != project_id:
            raise ValueError("Curator bootstrap proposal belongs to another project")
        text = TextRootDocument(
            root_hash=ZERO_HASH,
            schema_version=self._schema_version,
            chapters=(),
        )
        plan = _plan_root(planner_result, self._schema_version)
        world = _world_root(world_patch, self._schema_version)
        reference = _reference_root(ingested, self._schema_version)
        profile = _profile_root(planner_result, self._schema_version)
        candidates = BootstrapRootBuilder(self._artifacts).build(
            project_id,
            bundle.bundle_id,
            text,
            plan,
            world,
            reference,
            profile,
            planner_result.plan_proposal,
            world_patch,
            tuple(item.classification for item in ingested),
        )
        validation = BootstrapCrossRootValidator(self._clock).validate(candidates)
        coordinator = GenesisCoordinator(
            CommitService(self._session_factory),
            SqlAuthorApprovalRepository(self._session_factory),
            self._clock,
        )
        approval = coordinator.create_approval_request(candidates, validation)
        preview = {
            "project_id": project_id.root,
            "plan_nodes": [
                {"id": node.plan_node_id.root, "title": node.title, "summary": node.summary}
                for node in candidates.plan.nodes
            ],
            "chapter_goals": [
                {
                    "id": goal.goal_id.root,
                    "chapter_index": goal.chapter_index,
                    "summary": goal.summary,
                }
                for goal in candidates.plan.chapter_goals
            ],
            "world_entities": [
                {
                    "id": entity.entity_id.root,
                    "type": entity.entity_type,
                    "label": entity.internal_label,
                }
                for entity in candidates.world.entities
            ],
            "world_states": [
                {
                    "id": state.state_id.root,
                    "subject": state.subject_id.root,
                    "predicate": state.predicate,
                    "value": state.value,
                }
                for state in candidates.world.states
            ],
            "style_profile": candidates.profile.style_profile,
            "unresolved_plan": list(planner_result.plan_proposal.unresolved),
            "unresolved_world": list(world_patch.unresolved_claims),
            "validation_status": validation.status.value,
            "approval_request_id": approval.approval_request_id.root,
        }
        document = PreparedNovelBootstrapDocument(
            project_id=project_id,
            bootstrap_bundle_id=candidates.bootstrap_bundle_id,
            manifest=candidates.manifest,
            text=candidates.text,
            plan=candidates.plan,
            world=candidates.world,
            reference=candidates.reference,
            profile=candidates.profile,
            plan_proposal=candidates.plan_proposal,
            world_patch=candidates.world_patch,
            classifications=candidates.classifications,
            validation=validation,
            approval_request=approval,
            preview=cast(dict[str, JsonValue], preview),
        )
        artifact = self._artifacts.put(
            canonical_json_bytes(document.model_dump(mode="json")),
            PREPARED_BOOTSTRAP_MEDIA_TYPE,
            self._schema_version,
        )
        return PreparedNovelBootstrap(
            document=document,
            artifact=artifact,
            candidates=candidates,
        )

    def commit(
        self,
        *,
        prepared: PreparedNovelBootstrapDocument | ArtifactRef,
        author_id: StableId,
        reason: str,
        target_chapters: int,
        run_id: RunId,
        object_store_root: Path,
    ) -> tuple[CreativeRunPolicy, CreativeRunRequest, ProductionRunDescriptor]:
        if target_chapters < 1:
            raise ValueError("target_chapters must be positive")
        document = (
            prepared
            if isinstance(prepared, PreparedNovelBootstrapDocument)
            else PreparedNovelBootstrapDocument.model_validate_json(
                self._artifacts.read_verified(prepared),
                strict=True,
            )
        )
        candidates = BootstrapRootCandidates(
            bootstrap_bundle_id=document.bootstrap_bundle_id,
            manifest=document.manifest,
            text=document.text,
            plan=document.plan,
            world=document.world,
            reference=document.reference,
            profile=document.profile,
            plan_proposal=document.plan_proposal,
            world_patch=document.world_patch,
            classifications=document.classifications,
        )
        approvals = SqlAuthorApprovalRepository(self._session_factory)
        decision = AuthorApprovalDecision(
            decision_id=bounded_stable_id(
                f"decision.{document.approval_request.approval_request_id.root}",
                f"decision.{document.project_id.root}.{author_id.root}",
            ),
            approval_request_id=document.approval_request.approval_request_id,
            project_id=document.project_id,
            candidate_manifest_hash=document.approval_request.candidate_manifest_hash,
            validation_report_id=document.approval_request.validation_report_id,
            status=AuthorApprovalStatus.APPROVED,
            author_id=author_id,
            reason=reason,
            decided_at=self._clock(),
        )
        approvals.decide(decision)
        genesis = GenesisCoordinator(
            CommitService(self._session_factory),
            approvals,
            self._clock,
        ).commit(candidates, document.validation, document.approval_request.approval_request_id)
        policy_hash = content_id(
            {
                "automation_mode": AutomationMode.AUTO.value,
                "auto_accept_plan": True,
                "auto_accept_draft": True,
                "project_id": document.project_id.root,
                "run_id": run_id.root,
            }
        ).root
        policy = CreativeRunPolicy(
            automation_mode=AutomationMode.AUTO,
            policy_hash=policy_hash,
            permission_hash=policy_hash,
            auto_accept_plan=True,
            auto_accept_draft=True,
            max_task_attempts=3,
            max_tasks_per_advance=1,
            planning_horizon=5,
            runtime_parallelism=1,
            enable_planner_lookahead=False,
        )
        request = CreativeRunRequest(
            run_id=run_id,
            project_id=document.project_id,
            basis_commit=genesis.commit_id,
            basis_snapshot=snapshot_id_for_commit(genesis.commit_id),
            policy=policy,
            current_chapter=0,
            target_chapters=target_chapters,
        )
        descriptor = ProductionRunDescriptor(
            project_id=document.project_id,
            run_id=run_id,
            object_store_root=object_store_root,
            policy=policy,
            request=request,
            stop_after_chapter=target_chapters,
        )
        return policy, request, descriptor


def load_prepared_bootstrap(
    artifacts: ArtifactRepository,
    reference: ArtifactRef,
) -> PreparedNovelBootstrapDocument:
    if reference.media_type != PREPARED_BOOTSTRAP_MEDIA_TYPE:
        raise ValueError("prepared bootstrap artifact has an unexpected media type")
    return PreparedNovelBootstrapDocument.model_validate_json(
        artifacts.read_verified(reference),
        strict=True,
    )


def _plan_root(
    result: PlannerExecutionResult,
    schema_version: SchemaVersion,
) -> PlanRootDocument:
    nodes: list[PlanNode] = []
    goals: list[ChapterGoal] = []
    for item in result.plan_proposal.items:
        summary = str(item.payload.get("summary") or item.payload.get("text") or item.kind)
        title = str(item.payload.get("title") or item.kind)
        chapter_index = item.payload.get("chapter_index")
        if type(chapter_index) is int and chapter_index >= 1:
            goals.append(
                ChapterGoal(
                    goal_id=item.item_id,
                    chapter_index=chapter_index,
                    summary=summary or title,
                )
            )
            continue
        nodes.append(
            PlanNode(
                plan_node_id=item.item_id,
                node_type=item.kind,
                title=title,
                summary=summary,
            )
        )
    if result.project_intent is not None:
        for item in result.project_intent.items:
            if any(node.plan_node_id == item.item_id for node in nodes):
                continue
            nodes.append(
                PlanNode(
                    plan_node_id=item.item_id,
                    node_type=item.kind,
                    title=str(item.payload.get("title") or item.kind),
                    summary=str(
                        item.payload.get("summary") or item.payload.get("text") or item.kind
                    ),
                )
            )
    provisional = PlanRootDocument(
        root_hash=ZERO_HASH,
        schema_version=schema_version,
        nodes=tuple(nodes),
        chapter_goals=tuple(goals),
    )
    return provisional.model_copy(update={"root_hash": plan_root_content_id(provisional)})


def _world_root(
    world_patch: WorldPatchCandidate,
    schema_version: SchemaVersion,
) -> WorldRootDocument:
    setting_id = StableId("entity.bootstrap.story-world")
    entities = [
        Entity(entity_id=setting_id, entity_type="setting", internal_label="故事世界"),
    ]
    states: list[StateRecord] = []
    for index, item in enumerate(world_patch.items, start=1):
        label = str(item.payload.get("label") or item.payload.get("name") or "")
        if label:
            entity_id = StableId(f"entity.bootstrap.{index}")
            entities.append(
                Entity(
                    entity_id=entity_id,
                    entity_type=str(item.payload.get("entity_type") or item.kind),
                    internal_label=label,
                )
            )
            subject = entity_id
        else:
            subject = setting_id
        states.append(
            StateRecord(
                state_id=StableId(f"state.bootstrap.{index}"),
                subject_id=subject,
                predicate=str(item.payload.get("predicate") or item.kind),
                value=item.payload.get("value", item.payload.get("fact", item.payload)),
                valid_time=StoryTime(worldline="main", start_ordinal=0),
                truth_class=TruthClass.ACCEPTED_WORLD_FACT,
            )
        )
    provisional = WorldRootDocument(
        root_hash=ZERO_HASH,
        schema_version=schema_version,
        source_commit=ZERO_COMMIT,
        entities=tuple(entities),
        states=tuple(states),
    )
    return provisional.model_copy(update={"root_hash": world_root_content_id(provisional)})


def _reference_root(
    ingested: tuple[IngestedBootstrapSource, ...],
    schema_version: SchemaVersion,
) -> ReferenceRootDocument:
    assets: list[ReferenceAsset] = []
    for item in ingested:
        source = item.source
        candidate = item.reference_candidate
        if candidate is None:
            continue
        assets.append(
            ReferenceAsset(
                asset_id=StableId(f"reference.{source.source_id.root}"),
                source_id=source.source_id,
                source_class=source.source_class,
                artifact=source.artifact_ref,
            )
        )
    provisional = ReferenceRootDocument(
        root_hash=ZERO_HASH,
        schema_version=schema_version,
        assets=tuple(assets),
    )
    return provisional.model_copy(update={"root_hash": reference_root_content_id(provisional)})


def _profile_root(
    result: PlannerExecutionResult,
    schema_version: SchemaVersion,
) -> ProjectProfileRootDocument:
    style: dict[str, JsonValue] = {}
    if result.project_profile is not None:
        for item in result.project_profile.items:
            if "pov" in item.payload:
                style["pov"] = item.payload["pov"]
            if "narrative_person" in item.payload:
                style["narrative_person"] = item.payload["narrative_person"]
            if "style" in item.payload:
                style["style"] = item.payload["style"]
    contract = ContractRef(
        contract_id=StableId("agent.production-bootstrap"),
        version=schema_version,
        content_hash=content_id({"bootstrap": "profile"}),
    )
    prompt = PromptContractRef(
        contract_id=StableId("prompt.system-policy"),
        version=schema_version,
        content_hash=contract.content_hash,
        render_fingerprint=contract.content_hash,
    )
    skill = SkillContractRef(
        contract_id=StableId("skill.scene-composition"),
        version=schema_version,
        content_hash=contract.content_hash,
    )
    provisional = ProjectProfileRootDocument(
        root_hash=ZERO_HASH,
        schema_version=schema_version,
        style_profile=style,
        agent_specs=(contract,),
        prompt_contracts=(prompt,),
        skill_contracts=(skill,),
        tool_policies=(contract,),
        model_profiles=("qwen38-27b-fp8@8005",),
    )
    return provisional.model_copy(
        update={"root_hash": project_profile_root_content_id(provisional)}
    )


def bind_bootstrap_model_agents(
    *,
    artifacts: ArtifactRepository,
    endpoints: tuple[RegisteredModelEndpoint, ...],
    project_id: ProjectId,
    run_id: RunId,
    source_ids: tuple[StableId, ...],
    source_payload: str,
    source_artifacts: tuple[ArtifactRef, ...],
) -> tuple[PlannerBootstrap, CuratorBootstrap]:
    """Wire the existing Planner PROJECT_BOOTSTRAP and Curator BOOTSTRAP owners."""

    from novel_agent.runtime.production_bootstrap import PACKAGE_ROOT

    gateway = ModelGateway(
        endpoints,
        forbid_external_calls=all(not endpoint.adapter.is_external for endpoint in endpoints),
        budget_profile=BudgetResolutionProfile.STRICT,
    )
    planner_bundle = build_planner_contract_bundle(package_root=PACKAGE_ROOT, version=VERSION)
    planner_agent = PlannerAgent(
        StructuredAgentRunner(
            gateway, planner_bundle.agents, planner_bundle.prompts, planner_bundle.skills
        ),
        artifacts,
    )
    curator_agent = CuratorBootstrapAgent(
        _curator_bootstrap_runner(gateway, PACKAGE_ROOT),
        artifacts,
    )
    task = PlanningTask(
        planning_task_id=StableId("task.bootstrap.planner"),
        project_id=project_id,
        mode=AgentMode.PROJECT_BOOTSTRAP,
        source_ids=source_ids,
        strategy=BootstrapStrategy.DEVELOP_CANDIDATES,
    )

    async def planner() -> PlannerExecutionResult:
        result, _record = await planner_agent.run(
            version=VERSION,
            task=task,
            source_payload=source_payload,
            source_artifacts=source_artifacts,
            request=_bootstrap_model_request(
                run_id,
                TaskId("task.bootstrap.planner"),
                "planner.project_bootstrap",
            ),
        )
        return result

    async def curator() -> WorldPatchCandidate:
        patch, _record = await curator_agent.run(
            version=VERSION,
            project_id=project_id,
            source_ids=source_ids,
            source_payload=source_payload,
            source_artifacts=source_artifacts,
            request=_bootstrap_model_request(
                run_id,
                TaskId("task.bootstrap.curator"),
                "curator.bootstrap",
            ),
        )
        return patch

    return planner, curator


def _bootstrap_model_request(run_id: RunId, task_id: TaskId, phase: str) -> ModelRequest:
    return ModelRequest(
        request_id=bounded_stable_id(
            f"model-request.{run_id.root}.{phase}",
            f"model-request.{task_id.root}.{phase}",
        ),
        run_id=run_id,
        task_id=task_id,
        model_role=ModelRole.IMPLEMENTATION,
        purpose=ModelCallPurpose.DEVELOPMENT,
        trace_id=f"trace.{run_id.root}.{phase}",
        prompt="",
        agent_mode=phase,
        max_output_tokens=8_000,
        timeout_seconds=120.0,
        enable_thinking=False,
    )


def _curator_bootstrap_runner(gateway: ModelGateway, package_root: Path) -> StructuredAgentRunner:
    prompt_root = package_root / "prompts"
    skill_root = package_root / "skills"
    system_path = prompt_root / "system_policy_v1.md"
    task_path = prompt_root / "curator_bootstrap_v1.md"
    skill_path = skill_root / "memory_delta_extraction_v1.md"
    system_digest = content_hash(system_path.read_bytes())
    task_digest = content_hash(task_path.read_bytes())
    skill_digest = content_hash(skill_path.read_bytes())
    system_prompt = PromptContractRef(
        contract_id=StableId("prompt.system-policy"),
        version=VERSION,
        content_hash=system_digest,
        render_fingerprint=system_digest,
    )
    task_prompt = PromptContractRef(
        contract_id=StableId("prompt.curator-bootstrap"),
        version=VERSION,
        content_hash=task_digest,
        render_fingerprint=task_digest,
    )
    skill = SkillContractRef(
        contract_id=StableId("skill.memory-delta-extraction"),
        version=VERSION,
        content_hash=skill_digest,
    )
    schema = ContractRef(
        contract_id=StableId("schema.curator-bootstrap-draft"),
        version=VERSION,
        content_hash=content_id({"schema": "CuratorBootstrapDraft"}),
    )
    spec = seal_agent_spec(
        AgentSpec(
            agent_id=StableId("agent.memory-curator.bootstrap"),
            agent_type=AgentType.MEMORY_CURATOR,
            mode=AgentMode.BOOTSTRAP,
            version=VERSION,
            content_hash=content_id({"agent": "curator-bootstrap"}),
            input_schema=schema,
            output_schema=schema,
            system_prompt=system_prompt,
            task_prompt=task_prompt,
            skills=(skill,),
            tool_policy=seal_tool_policy(
                ToolPolicy(
                    policy_id=StableId("policy.curator-bootstrap"),
                    version=VERSION,
                    content_hash=content_id({"policy": "curator-bootstrap"}),
                    allowed_tools=(),
                    permission=ToolPermission.READ,
                    max_tool_calls=0,
                )
            ),
        )
    )
    return StructuredAgentRunner(
        gateway,
        AgentRegistry((spec,)),
        PromptRegistry(
            (
                PromptTemplate(system_prompt.contract_id, VERSION, system_path, system_digest),
                PromptTemplate(task_prompt.contract_id, VERSION, task_path, task_digest),
            )
        ),
        SkillRegistry((SkillTemplate(skill.contract_id, VERSION, skill_path, skill_digest),)),
    )


__all__ = [
    "PREPARED_BOOTSTRAP_CONTRACT",
    "PREPARED_BOOTSTRAP_MEDIA_TYPE",
    "PreparedNovelBootstrap",
    "PreparedNovelBootstrapDocument",
    "ProductionNovelBootstrap",
    "bind_bootstrap_model_agents",
    "load_prepared_bootstrap",
]
