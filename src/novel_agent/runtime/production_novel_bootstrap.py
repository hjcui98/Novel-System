"""Two-step novel initialization that reuses existing U1-U8 bootstrap owners."""

from __future__ import annotations

import re
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
from novel_agent.domain.changes import ValidationFinding, ValidationReport, ValidationStatus
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
    ProposedItem,
    ReferenceAsset,
    ReferenceRootDocument,
    SkillContractRef,
    SourceClass,
    SourceClassification,
    ToolPermission,
    ToolPolicy,
    WorldPatchCandidate,
)
from novel_agent.domain.world import Entity, PlanLevel, PlanNode, StateRecord, StoryTime, TruthClass
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
    approval_request: AuthorApprovalRequest | None = None
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
        endpoints: tuple[RegisteredModelEndpoint, ...] = (),
        run_id: RunId | None = None,
        schema_version: SchemaVersion = VERSION,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._artifacts = artifacts
        self._session_factory = session_factory
        self._planner = planner
        self._curator = curator
        self._endpoints = endpoints
        self._run_id = run_id
        self._schema_version = schema_version
        self._clock = clock or (lambda: datetime.now(UTC))

    async def prepare(
        self,
        *,
        project_id: ProjectId,
        brief_text: str,
    ) -> PreparedNovelBootstrap:
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
            _split_composite_brief(brief_text),
            self._schema_version,
        )
        planner = self._planner
        curator = self._curator
        if planner is None or curator is None:
            if not self._endpoints or self._run_id is None:
                raise ValueError("bootstrap prepare requires Planner and Curator owners")
            planner, curator = bind_bootstrap_model_agents(
                artifacts=self._artifacts,
                endpoints=self._endpoints,
                project_id=project_id,
                run_id=self._run_id,
                source_ids=tuple(item.source.source_id for item in ingested),
                source_payload=_joined_source_payload(ingested),
                source_artifacts=tuple(item.source.artifact_ref for item in ingested),
            )
        planner_result = await planner()
        world_patch = _merge_world_patch(await curator(), planner_result)
        planner_result, world_patch = _route_bootstrap_citations(
            planner_result, world_patch, ingested
        )
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
        profile = _profile_root(planner_result, self._schema_version, brief_text)
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
        structural = BootstrapCrossRootValidator(self._clock).validate(candidates)
        sufficiency = _production_genesis_sufficiency(candidates, brief_text)
        findings = tuple((*structural.findings, *sufficiency))
        validation = structural.model_copy(
            update={
                "findings": findings,
                "status": (ValidationStatus.PASSED if not findings else ValidationStatus.FAILED),
            }
        )
        approval = None
        if validation.status is ValidationStatus.PASSED:
            approval = GenesisCoordinator(
                CommitService(self._session_factory),
                SqlAuthorApprovalRepository(self._session_factory),
                self._clock,
            ).create_approval_request(candidates, validation)
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
            "validation_findings": [
                {"code": item.code, "message": item.message} for item in findings
            ],
            "approval_request_id": (
                None if approval is None else approval.approval_request_id.root
            ),
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
        if document.approval_request is None:
            raise ValueError("Genesis commit requires a passed production sufficiency check")
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
            input_artifact_refs=tuple(asset.artifact for asset in document.reference.assets),
            current_chapter=0,
            target_chapters=target_chapters,
            plan_level=PlanLevel.STORY,
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


OPENING_CHAPTER_GOAL_LIMIT = 5
DUMMY_WORLD_ENTITY_ID = StableId("entity.bootstrap.story-world")
COMPOSITE_BRIEF_CHARS = 4_000
_PLAN_SOURCE_CLASSES = frozenset(
    {SourceClass.AUTHOR_INITIAL_BRIEF, SourceClass.AUTHOR_KNOWN_FUTURE_PLAN}
)
_WORLD_SOURCE_CLASSES = frozenset({SourceClass.AUTHOR_INITIAL_BRIEF, SourceClass.BASELINE_SETTING})
_PROFILE_KEYS = (
    "title",
    "book_title",
    "genre",
    "genres",
    "题材",
    "chapter_length",
    "target_chapter_characters",
    "expected_chapter_characters",
    "minimum_characters",
    "target_characters",
    "maximum_characters",
    "target_chapters",
    "pov",
    "narrative_person",
    "style",
    "premise",
    "one_sentence_summary",
    "audience",
)


def _payload_text(item: ProposedItem, *keys: str) -> str:
    for key in keys:
        value = item.payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _plan_root(
    result: PlannerExecutionResult,
    schema_version: SchemaVersion,
) -> PlanRootDocument:
    nodes: list[PlanNode] = []
    goals: list[ChapterGoal] = []
    seen: set[StableId] = set()

    def add_node(item: ProposedItem) -> None:
        if item.item_id in seen:
            return
        seen.add(item.item_id)
        summary = _payload_text(item, "description", "summary", "text", "direction") or item.kind
        title = _payload_text(item, "title", "name") or item.kind
        if title == item.kind and summary != item.kind:
            title = summary[:48]
        chapter_index = item.payload.get("chapter_index")
        if type(chapter_index) is int and 1 <= chapter_index <= OPENING_CHAPTER_GOAL_LIMIT:
            goals.append(
                ChapterGoal(
                    goal_id=item.item_id,
                    chapter_index=chapter_index,
                    summary=summary or title,
                )
            )
            return
        nodes.append(
            PlanNode(
                plan_node_id=item.item_id,
                node_type=item.kind,
                title=title,
                summary=summary,
            )
        )

    for item in result.plan_proposal.items:
        add_node(item)
    if result.project_intent is not None:
        for item in result.project_intent.items:
            add_node(item)
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
    setting_id = DUMMY_WORLD_ENTITY_ID
    entities = [
        Entity(entity_id=setting_id, entity_type="setting", internal_label="故事世界"),
    ]
    states: list[StateRecord] = []
    for index, item in enumerate(world_patch.items, start=1):
        label = _payload_text(item, "label", "name", "title")
        entity_type = _payload_text(item, "entity_type", "type") or item.kind
        fact = item.payload.get("value")
        if fact is None:
            fact = _payload_text(item, "description", "fact", "summary") or item.payload
        if label:
            entity_id = StableId(f"entity.bootstrap.{index}")
            entities.append(
                Entity(
                    entity_id=entity_id,
                    entity_type=entity_type,
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
                value=fact,
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
    brief_text: str = "",
) -> ProjectProfileRootDocument:
    style: dict[str, JsonValue] = dict(_profile_from_brief(brief_text))

    def absorb(item: ProposedItem) -> None:
        for key, value in item.payload.items():
            if key in _PROFILE_KEYS and key not in style and value not in (None, ""):
                style[key] = value
        title = _payload_text(item, "title", "book_title")
        if title and "title" not in style:
            style["title"] = title
        genre = _payload_text(item, "genre", "genres", "题材")
        if genre and "genre" not in style:
            style["genre"] = genre
        premise = _payload_text(item, "premise", "one_sentence_summary", "summary", "description")
        if premise and "premise" not in style:
            style["premise"] = premise

    if result.project_profile is not None:
        for item in result.project_profile.items:
            absorb(item)
    if result.project_intent is not None:
        for item in result.project_intent.items:
            absorb(item)
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


def _profile_from_brief(brief_text: str) -> dict[str, JsonValue]:
    style: dict[str, JsonValue] = {}
    title = re.search(r"书名[^：:\n]*[：:]\s*[《“\"]?([^》”\"\n]+)[》”\"]?", brief_text)  # noqa: RUF001
    if title is not None:
        style["title"] = title.group(1).strip()
    genre = re.search(r"题材[^：:\n]*[：:]\s*(.+)", brief_text)  # noqa: RUF001
    if genre is not None:
        style["genre"] = genre.group(1).strip()
    chapters = re.search(r"预计章节数[^：:\n]*[：:]\s*(\d+)", brief_text)  # noqa: RUF001
    if chapters is not None:
        style["target_chapters"] = int(chapters.group(1))
    band = re.search(r"每章\s*(\d+)\s*[-~～到至]+\s*(\d+)\s*字", brief_text)  # noqa: RUF001
    if band is not None:
        style["minimum_characters"] = int(band.group(1))
        style["target_characters"] = int(band.group(1))
        style["maximum_characters"] = int(band.group(2))
    premise = re.search(r"一句话概括[^：:\n]*[：:]\s*(.+)", brief_text)  # noqa: RUF001
    if premise is not None:
        style["premise"] = premise.group(1).strip()
    return style


def _joined_source_payload(ingested: tuple[IngestedBootstrapSource, ...]) -> str:
    return "\n\n".join(
        f"SOURCE={item.source.source_id.root}\nCLASS={item.source.source_class.value}\n{item.parsed}"
        for item in ingested
    )


def _classify_heading(heading: str) -> SourceClass:
    if any(token in heading for token in ("核心真相", "后期", "长篇", "节拍", "规划确定", "卷")):
        return SourceClass.AUTHOR_KNOWN_FUTURE_PLAN
    if any(token in heading for token in ("基本信息", "目标字数", "预计章节", "风格", "POV")):
        return SourceClass.STYLE_GUIDE
    if any(
        token in heading
        for token in (
            "世界观",
            "大陆",
            "力量",
            "职业",
            "城邦",
            "派系",
            "人类现状",
            "设施",
            "能量",
            "等级",
            "人物",
            "主角",
        )
    ):
        return SourceClass.BASELINE_SETTING
    return SourceClass.AUTHOR_INITIAL_BRIEF


def _markdown_h2_sections(brief_text: str) -> tuple[tuple[str, str], ...]:
    sections: list[tuple[str, str]] = []
    heading = ""
    body: list[str] = []
    for line in brief_text.splitlines():
        match = re.fullmatch(r"##\s+(.+)", line.strip())
        if match is not None:
            if heading:
                sections.append((heading, "\n".join(body).strip()))
            heading = match.group(1).strip()
            body = []
            continue
        if heading:
            body.append(line)
    if heading:
        sections.append((heading, "\n".join(body).strip()))
    return tuple(sections)


def _flush_h3(
    remaining: list[str],
    futures: list[tuple[str, str]],
    heading: str,
    chunk: list[str],
    future: bool,
) -> None:
    if not heading:
        return
    text = "\n".join(chunk).strip()
    if future:
        if text:
            futures.append((heading, text))
        return
    remaining.append(f"### {heading}")
    if text:
        remaining.append(text)


def _peel_future_subsections(body: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    remaining: list[str] = []
    futures: list[tuple[str, str]] = []
    heading = ""
    chunk: list[str] = []
    future = False
    for line in body.splitlines():
        match = re.fullmatch(r"###\s+(.+)", line.strip())
        if match is not None:
            _flush_h3(remaining, futures, heading, chunk, future)
            heading = match.group(1).strip()
            chunk = []
            future = _classify_heading(heading) is SourceClass.AUTHOR_KNOWN_FUTURE_PLAN
            continue
        if heading:
            chunk.append(line)
        else:
            remaining.append(line)
    _flush_h3(remaining, futures, heading, chunk, future)
    return "\n".join(remaining).strip(), tuple(futures)


def _split_composite_brief(brief_text: str) -> tuple[RawBootstrapSource, ...]:
    sources = [
        RawBootstrapSource(
            source_id=StableId("source.author-initial-brief"),
            source_class=SourceClass.AUTHOR_INITIAL_BRIEF,
            media_type="text/plain",
            data=brief_text.encode("utf-8"),
        )
    ]
    if len(brief_text) < COMPOSITE_BRIEF_CHARS:
        return tuple(sources)
    counts = {
        SourceClass.BASELINE_SETTING: 0,
        SourceClass.AUTHOR_KNOWN_FUTURE_PLAN: 0,
        SourceClass.STYLE_GUIDE: 0,
    }

    def emit(heading: str, body: str, prefix: str) -> None:
        if not body:
            return
        classified = _classify_heading(heading)
        if classified is SourceClass.AUTHOR_INITIAL_BRIEF:
            return
        counts[classified] += 1
        if classified is SourceClass.BASELINE_SETTING:
            source_id = StableId(f"source.baseline-setting.{counts[classified]}")
        elif classified is SourceClass.AUTHOR_KNOWN_FUTURE_PLAN:
            source_id = StableId(f"source.future-plan.{counts[classified]}")
        else:
            source_id = StableId(f"source.style-guide.{counts[classified]}")
        sources.append(
            RawBootstrapSource(
                source_id=source_id,
                source_class=classified,
                media_type="text/plain",
                data=f"{prefix} {heading}\n{body}".encode(),
            )
        )

    for heading, body in _markdown_h2_sections(brief_text):
        kept, futures = _peel_future_subsections(body)
        emit(heading, kept, "##")
        for sub_heading, sub_body in futures:
            emit(sub_heading, sub_body, "###")
    return tuple(sources)


def _merge_world_patch(
    world_patch: WorldPatchCandidate,
    planner_result: PlannerExecutionResult,
) -> WorldPatchCandidate:
    extra = () if planner_result.world_design is None else planner_result.world_design.items
    combined: list[ProposedItem] = []
    seen: set[str] = set()
    for item in (*world_patch.items, *extra):
        key = (
            _payload_text(item, "label", "name", "title", "description", "fact", "summary")
            or item.item_id.root
        )
        if key in seen:
            continue
        seen.add(key)
        combined.append(item)
    if tuple(combined) == world_patch.items:
        return world_patch
    origin = tuple(
        dict.fromkeys(
            (
                *world_patch.origin_source_ids,
                *(source_id for item in extra for source_id in item.source_ids),
            )
        )
    )
    coverage = world_patch.extraction_coverage
    if combined and coverage == 0:
        coverage = min(1.0, len(combined) / 16)
    return world_patch.model_copy(
        update={
            "items": tuple(combined),
            "origin_source_ids": origin,
            "extraction_coverage": coverage,
        }
    )


def _brief_source_id(ingested: tuple[IngestedBootstrapSource, ...]) -> StableId:
    for item in ingested:
        if item.source.source_class is SourceClass.AUTHOR_INITIAL_BRIEF:
            return item.source.source_id
    return ingested[0].source.source_id


def _allowed_source_ids(
    source_ids: tuple[StableId, ...],
    allowed: frozenset[SourceClass],
    classes: dict[StableId, SourceClass],
    fallback: StableId,
) -> tuple[StableId, ...]:
    kept = tuple(source_id for source_id in source_ids if classes.get(source_id) in allowed)
    return kept or (fallback,)


def _route_items(
    items: tuple[ProposedItem, ...],
    allowed: frozenset[SourceClass],
    classes: dict[StableId, SourceClass],
    fallback: StableId,
) -> tuple[ProposedItem, ...]:
    return tuple(
        item.model_copy(
            update={"source_ids": _allowed_source_ids(item.source_ids, allowed, classes, fallback)}
        )
        for item in items
    )


def _route_bootstrap_citations(
    planner_result: PlannerExecutionResult,
    world_patch: WorldPatchCandidate,
    ingested: tuple[IngestedBootstrapSource, ...],
) -> tuple[PlannerExecutionResult, WorldPatchCandidate]:
    """Keep split sources as Reference; cite only legal Plan/World origins."""

    classes = {item.source.source_id: item.source.source_class for item in ingested}
    fallback = _brief_source_id(ingested)
    plan_proposal = planner_result.plan_proposal.model_copy(
        update={
            "items": _route_items(
                planner_result.plan_proposal.items, _PLAN_SOURCE_CLASSES, classes, fallback
            )
        }
    )
    return (
        planner_result.model_copy(update={"plan_proposal": plan_proposal}),
        world_patch.model_copy(
            update={
                "items": _route_items(world_patch.items, _WORLD_SOURCE_CLASSES, classes, fallback),
                "origin_source_ids": _allowed_source_ids(
                    world_patch.origin_source_ids, _WORLD_SOURCE_CLASSES, classes, fallback
                ),
            }
        ),
    )


def _production_genesis_sufficiency(
    candidates: BootstrapRootCandidates,
    brief_text: str,
) -> tuple[ValidationFinding, ...]:
    findings: list[ValidationFinding] = []
    named = tuple(
        entity for entity in candidates.world.entities if entity.entity_id != DUMMY_WORLD_ENTITY_ID
    )
    substantial = tuple(
        node
        for node in candidates.plan.nodes
        if len(node.summary.strip()) >= 40
        and node.summary.strip() not in {node.node_type, "planner_proposal"}
    )
    profile = candidates.profile.style_profile
    has_title = any(key in profile for key in ("title", "book_title"))
    has_genre = any(key in profile for key in ("genre", "genres", "题材"))
    composite = len(brief_text) >= COMPOSITE_BRIEF_CHARS
    if not candidates.world_patch.items:
        findings.append(
            ValidationFinding(
                code="BOOTSTRAP_WORLD_EMPTY",
                severity="error",
                message="production Genesis has no structured world items",
            )
        )
    if not candidates.plan.nodes and not candidates.plan.chapter_goals:
        findings.append(
            ValidationFinding(
                code="BOOTSTRAP_PLAN_EMPTY",
                severity="error",
                message="production Genesis has no Plan nodes or opening chapter goals",
            )
        )
    if len(candidates.plan.chapter_goals) > OPENING_CHAPTER_GOAL_LIMIT:
        findings.append(
            ValidationFinding(
                code="BOOTSTRAP_CHAPTER_GOALS_UNBOUNDED",
                severity="error",
                message="production Genesis must not pre-generate the full chapter ladder",
            )
        )
    if not composite:
        return tuple(findings)
    if len(named) < 8:
        findings.append(
            ValidationFinding(
                code="BOOTSTRAP_WORLD_SPARSE",
                severity="error",
                message="composite setting produced too few named world entities",
            )
        )
    if len(candidates.world.states) < 8:
        findings.append(
            ValidationFinding(
                code="BOOTSTRAP_WORLD_STATES_SPARSE",
                severity="error",
                message="composite setting produced too few baseline world states",
            )
        )
    if len(substantial) < 5:
        findings.append(
            ValidationFinding(
                code="BOOTSTRAP_PLAN_SPARSE",
                severity="error",
                message="composite setting needs first-stage Plan descriptions, not empty labels",
            )
        )
    if not has_title or not has_genre:
        findings.append(
            ValidationFinding(
                code="BOOTSTRAP_PROFILE_INCOMPLETE",
                severity="error",
                message="composite setting must record title and genre in ProjectProfile",
            )
        )
    return tuple(findings)


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
        structured_max_retries=1,
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
