from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.cli import main
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import PlanRootDocument, TextRootDocument
from novel_agent.domain.creative_runtime import AutomationMode
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRole
from novel_agent.domain.planning_locks import (
    AuthorPlanningLocksDocument,
    author_planning_locks_content_id,
)
from novel_agent.domain.stage2 import (
    AgentMode,
    BootstrapStrategy,
    ContractRef,
    PlannerExecutionResult,
    ProjectIntentModel,
    ProjectProfileProposal,
    ProjectProfileRootDocument,
    PromptContractRef,
    ProposalProvenance,
    ProposedItem,
    ReferenceRootDocument,
    SkillContractRef,
    SourceClass,
    WorldDesignProposal,
    WorldPatchCandidate,
)
from novel_agent.domain.world import PlanLevel
from novel_agent.runtime.production_novel_bootstrap import (
    BOOTSTRAP_MAX_OUTPUT_TOKENS,
    BOOTSTRAP_REQUEST_TIMEOUT_SECONDS,
    COMPOSITE_BRIEF_CHARS,
    ProductionNovelBootstrap,
    _merge_world_patch,
    _profile_from_brief,
    _split_composite_brief,
    bind_bootstrap_model_agents,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.bootstrap_workflow import BootstrapRootBuilder
from novel_agent.services.model_gateway import RegisteredModelEndpoint
from tests.unit.test_production_assembly import _stamp_sqlite
from tests.unit.test_stage2_bootstrap_workflow import proposals

VERSION = SchemaVersion("1.0.0")


def test_bootstrap_planner_binds_a_missing_trusted_strategy(tmp_path: Path) -> None:
    valid = {
        "mode": "project_bootstrap",
        "strategy": "develop_candidates",
        "project_intent_items": [
            {
                "item_id": "intent.author-brief",
                "kind": "author_brief",
                "payload": {"summary": "A wounded heir enters the tower."},
                "provenance": "author_supplied",
                "source_ids": ["source.author-initial-brief"],
            }
        ],
        "coverage": 1.0,
    }
    invalid = dict(valid)
    invalid.pop("strategy")

    endpoint = FakeModelEndpoint(json.dumps(invalid))
    registered = RegisteredModelEndpoint(
        role=ModelRole.IMPLEMENTATION,
        endpoint_name="bootstrap-test-endpoint",
        model_name="bootstrap-test-model",
        adapter=endpoint,
    )
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    source_artifact = artifacts.put(b"author brief", "text/plain", VERSION)
    planner, _curator = bind_bootstrap_model_agents(
        artifacts=artifacts,
        endpoints=(registered,),
        project_id=ProjectId("project.bootstrap.retry"),
        run_id=RunId("run.bootstrap.retry"),
        source_ids=(StableId("source.author-initial-brief"),),
        source_payload="SOURCE=source.author-initial-brief\nA wounded heir enters the tower.",
        source_artifacts=(source_artifact,),
        max_output_tokens=48_000,
    )

    async def run_planner() -> PlannerExecutionResult:
        return await planner()

    result = asyncio.run(run_planner())

    assert result.plan_proposal.strategy is BootstrapStrategy.DEVELOP_CANDIDATES
    assert len(endpoint.requests) == 1
    assert endpoint.requests[0].max_output_tokens == 48_000
    schema = endpoint.requests[0].response_schema
    assert schema is not None
    required = schema.get("required")
    assert isinstance(required, list)
    assert "strategy" in required
    properties = schema.get("properties")
    assert isinstance(properties, dict)
    strategy_schema = properties.get("strategy")
    assert isinstance(strategy_schema, dict)
    assert strategy_schema.get("const") == "develop_candidates"


def test_bootstrap_prepare_rejects_a_non_positive_output_budget(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with pytest.raises(ValueError, match="bootstrap output budget must be positive"):
        ProductionNovelBootstrap(
            artifacts=ArtifactRepository(FilesystemObjectStore(tmp_path / "objects")),
            session_factory=build_session_factory(engine),
            bootstrap_max_output_tokens=0,
        )


def test_bootstrap_budget_and_timeout_defaults_stay_within_the_domain_ceiling(
    tmp_path: Path,
) -> None:
    from novel_agent.domain.model_calls import ModelRequest

    defaults = ProductionNovelBootstrap.__init__.__kwdefaults__
    assert defaults is not None
    assert defaults["bootstrap_max_output_tokens"] == BOOTSTRAP_MAX_OUTPUT_TOKENS
    assert defaults["bootstrap_request_timeout_seconds"] == BOOTSTRAP_REQUEST_TIMEOUT_SECONDS
    configured = ModelRequest(
        request_id=StableId("model-request.budget-ceiling"),
        run_id=RunId("run.budget-ceiling"),
        task_id=TaskId("task.budget-ceiling"),
        model_role=ModelRole.IMPLEMENTATION,
        purpose=ModelCallPurpose.DEVELOPMENT,
        trace_id="trace.budget-ceiling",
        prompt="",
        agent_mode="planner.project_bootstrap",
        max_output_tokens=BOOTSTRAP_MAX_OUTPUT_TOKENS,
        timeout_seconds=BOOTSTRAP_REQUEST_TIMEOUT_SECONDS,
    )
    assert configured.timeout_seconds == 900.0

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with pytest.raises(ValueError, match="bootstrap request timeout must be positive"):
        ProductionNovelBootstrap(
            artifacts=ArtifactRepository(FilesystemObjectStore(tmp_path / "objects")),
            session_factory=build_session_factory(engine),
            bootstrap_request_timeout_seconds=0.0,
        )


def test_the_frozen_policy_hash_matches_the_runtime_assembly_fingerprint(
    tmp_path: Path,
) -> None:
    """The freeze and the dispatch must hash the same endpoints.

    ``bootstrap-commit`` used to build the fingerprint from a fabricated endpoint
    list while the dispatcher used the registered profile, so every run frozen
    against a profile whose output allowance differed from the constant failed
    closed with RUN_CONFIGURATION_CHANGED before its first task.
    """

    from novel_agent.domain.stage5_manifest import load_stage5_manifest
    from novel_agent.runtime.creative_assembly import (
        ProductionAssemblyContext,
        build_production_assembly,
    )
    from novel_agent.runtime.production_bootstrap import (
        QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE,
        load_production_assembly_spec,
        resolve_registered_model_endpoints,
    )

    # One stamped database holds both the frozen project and the runtime assembly.
    database_url = _stamp_sqlite(tmp_path / "runtime.db")
    sessions = build_session_factory(create_engine(database_url))
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    project_id = ProjectId("project.bootstrap.novel")
    plan_proposal, world_patch = proposals(project_id)
    plan_proposal = plan_proposal.model_copy(
        update={"strategy": BootstrapStrategy.DEVELOP_CANDIDATES}
    )
    planner_result = PlannerExecutionResult(
        mode=AgentMode.PROJECT_BOOTSTRAP,
        project_intent=ProjectIntentModel(
            intent_id=StableId("intent.bootstrap"),
            project_id=project_id,
            strategy=BootstrapStrategy.DEVELOP_CANDIDATES,
            items=(
                ProposedItem(
                    item_id=StableId("intent.item"),
                    kind="premise",
                    payload={"summary": "story"},
                    provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                    source_ids=(StableId("source.author-initial-brief"),),
                ),
            ),
            source_ids=(StableId("source.author-initial-brief"),),
            coverage=1,
        ),
        plan_proposal=plan_proposal.model_copy(
            update={
                "items": (
                    ProposedItem(
                        item_id=StableId("plan.item"),
                        kind="premise",
                        payload={"summary": "story"},
                        provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                        source_ids=(StableId("source.author-initial-brief"),),
                    ),
                ),
                "strategy": BootstrapStrategy.DEVELOP_CANDIDATES,
            }
        ),
        output_artifact=artifacts.put(b"planner", "application/json", VERSION),
        receipt=plan_proposal.receipt,
    )
    world_patch = world_patch.model_copy(
        update={
            "origin_source_ids": (StableId("source.author-initial-brief"),),
            "items": (
                ProposedItem(
                    item_id=StableId("world.item"),
                    kind="baseline_state",
                    payload={"fact": "known", "label": "Lin"},
                    provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                    source_ids=(StableId("source.author-initial-brief"),),
                ),
            ),
        }
    )

    async def planner() -> PlannerExecutionResult:
        return planner_result

    async def curator() -> object:
        return world_patch

    endpoints = resolve_registered_model_endpoints(QWEN38_27B_NVFP4_8003_ENDPOINT_PROFILE)
    service = ProductionNovelBootstrap(
        artifacts=artifacts,
        session_factory=sessions,
        planner=planner,
        curator=curator,  # type: ignore[arg-type]
        endpoints=endpoints,
    )
    prepared = asyncio.run(
        service.prepare(project_id=project_id, brief_text="A wounded heir enters the tower.")
    )
    policy, _request, descriptor = service.commit(
        prepared=prepared.document,
        author_id=StableId("author.1"),
        reason="reviewed Plan/World/Profile",
        target_chapters=10,
        run_id=RunId("run.bootstrap.novel"),
        object_store_root=tmp_path / "objects",
        retrieval_backend_profile="memory",
    )

    assembly = build_production_assembly(
        ProductionAssemblyContext(
            database_url=database_url,
            object_store_root=tmp_path / "objects",
            project_id=descriptor.project_id,
            run_id=descriptor.run_id,
            policy=policy,
            manifest=load_stage5_manifest(
                Path(__file__).parents[2]
                / "src/novel_agent/runtime/stage5_development_manifest.json"
            ),
            model_endpoints=endpoints,
            retrieval_backend_profile="memory",
            spec=load_production_assembly_spec(),
        )
    )

    assert assembly.attestation is not None
    assert assembly.attestation.configuration_fingerprint.root == policy.policy_hash


def test_bootstrap_prepare_then_commit_emits_auto_dispatch_descriptor(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = build_session_factory(engine)
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    project_id = ProjectId("project.bootstrap.novel")
    plan_proposal, world_patch = proposals(project_id)
    plan_proposal = plan_proposal.model_copy(
        update={"strategy": BootstrapStrategy.DEVELOP_CANDIDATES}
    )
    planner_result = PlannerExecutionResult(
        mode=AgentMode.PROJECT_BOOTSTRAP,
        project_intent=ProjectIntentModel(
            intent_id=StableId("intent.bootstrap"),
            project_id=project_id,
            strategy=BootstrapStrategy.DEVELOP_CANDIDATES,
            items=(
                ProposedItem(
                    item_id=StableId("intent.item"),
                    kind="premise",
                    payload={"summary": "story"},
                    provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                    source_ids=(StableId("source.author-initial-brief"),),
                ),
            ),
            source_ids=(StableId("source.author-initial-brief"),),
            coverage=1,
        ),
        plan_proposal=plan_proposal.model_copy(
            update={
                "items": (
                    ProposedItem(
                        item_id=StableId("plan.item"),
                        kind="premise",
                        payload={"summary": "story"},
                        provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                        source_ids=(StableId("source.author-initial-brief"),),
                    ),
                ),
                "strategy": BootstrapStrategy.DEVELOP_CANDIDATES,
            }
        ),
        output_artifact=artifacts.put(b"planner", "application/json", VERSION),
        receipt=plan_proposal.receipt,
    )
    world_patch = world_patch.model_copy(
        update={
            "origin_source_ids": (StableId("source.author-initial-brief"),),
            "items": (
                ProposedItem(
                    item_id=StableId("world.item"),
                    kind="baseline_state",
                    payload={"fact": "known", "label": "Lin"},
                    provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                    source_ids=(StableId("source.author-initial-brief"),),
                ),
            ),
        }
    )

    async def planner() -> PlannerExecutionResult:
        return planner_result

    async def curator() -> object:
        return world_patch

    service = ProductionNovelBootstrap(
        artifacts=artifacts,
        session_factory=sessions,
        planner=planner,
        curator=curator,  # type: ignore[arg-type]
    )
    prepared = asyncio.run(
        service.prepare(project_id=project_id, brief_text="A wounded heir enters the tower.")
    )
    assert prepared.document.preview["validation_status"] == "passed"
    assert prepared.document.plan.nodes or prepared.document.plan.chapter_goals
    assert prepared.document.world.entities
    policy, request, descriptor = service.commit(
        prepared=prepared.document,
        author_id=StableId("author.1"),
        reason="reviewed Plan/World/Profile",
        target_chapters=10,
        run_id=RunId("run.bootstrap.novel"),
        object_store_root=tmp_path / "objects",
    )
    assert policy.automation_mode is AutomationMode.AUTO
    assert policy.auto_accept_plan is False
    assert policy.auto_accept_draft is False
    assert request.target_chapters == 10
    assert request.plan_level is PlanLevel.STORY
    assert descriptor.stop_after_chapter == 10
    engine.dispose()


def _composite_brief() -> str:
    return (
        "# Sample Outline\n\n"
        "## 基本信息\n"
        "- **书名**：《样例长篇》\n"  # noqa: RUF001
        "- **题材**：史诗奇幻\n"  # noqa: RUF001
        "- **预计章节数**：800\n"  # noqa: RUF001
        "- **目标字数**：每章 3000-5000 字\n"  # noqa: RUF001
        "- **一句话概括**：边镇学徒进入学院。\n\n"  # noqa: RUF001
        "## 世界观\n"
        "主要城邦：北城、南城。\n\n"  # noqa: RUF001
        "### 核心真相（后期揭露）\n"  # noqa: RUF001
        "后期真相只属于 Plan，不得写入 World。\n\n"  # noqa: RUF001
        "## 力量体系\n"
        "职业：守城、近战。\n" + ("设定填充。" * 900)  # noqa: RUF001
    )


def _item(
    item_id: str,
    kind: str,
    payload: dict[str, Any],
    source_id: str = "source.author-initial-brief",
) -> ProposedItem:
    return ProposedItem(
        item_id=StableId(item_id),
        kind=kind,
        payload=payload,
        provenance=ProposalProvenance.AUTHOR_SUPPLIED,
        source_ids=(StableId(source_id),),
    )


def _prepare(
    tmp_path: Path,
    *,
    brief_text: str,
    plan_items: tuple[ProposedItem, ...],
    world_items: tuple[ProposedItem, ...],
    planner_world_items: tuple[ProposedItem, ...] = (),
    profile_items: tuple[ProposedItem, ...] = (),
    world_coverage: float | None = None,
    origin_source_ids: tuple[StableId, ...] | None = None,
) -> Any:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = build_session_factory(engine)
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    project_id = ProjectId("project.bootstrap.novel")
    plan_proposal, world_patch = proposals(project_id)
    planner_result = PlannerExecutionResult(
        mode=AgentMode.PROJECT_BOOTSTRAP,
        project_intent=ProjectIntentModel(
            intent_id=StableId("intent.bootstrap"),
            project_id=project_id,
            strategy=BootstrapStrategy.DEVELOP_CANDIDATES,
            items=plan_items,
            source_ids=(StableId("source.author-initial-brief"),),
            coverage=1,
        ),
        plan_proposal=plan_proposal.model_copy(
            update={
                "items": plan_items,
                "strategy": BootstrapStrategy.DEVELOP_CANDIDATES,
            }
        ),
        world_design=(
            None
            if not planner_world_items
            else WorldDesignProposal(
                proposal_id=StableId("world-design.bootstrap"),
                project_id=project_id,
                items=planner_world_items,
            )
        ),
        project_profile=(
            None
            if not profile_items
            else ProjectProfileProposal(
                proposal_id=StableId("profile.bootstrap"),
                project_id=project_id,
                items=profile_items,
            )
        ),
        output_artifact=artifacts.put(b"planner", "application/json", VERSION),
        receipt=plan_proposal.receipt,
    )
    coverage = world_coverage
    if coverage is None:
        coverage = 0 if not world_items else 1
    world_patch = world_patch.model_copy(
        update={
            "origin_source_ids": origin_source_ids or (StableId("source.author-initial-brief"),),
            "items": world_items,
            "extraction_coverage": coverage,
        }
    )

    async def planner() -> PlannerExecutionResult:
        return planner_result

    async def curator() -> WorldPatchCandidate:
        return world_patch

    service = ProductionNovelBootstrap(
        artifacts=artifacts,
        session_factory=sessions,
        planner=planner,
        curator=curator,
    )
    prepared = asyncio.run(service.prepare(project_id=project_id, brief_text=brief_text))
    engine.dispose()
    return prepared, service


def test_plan_root_keeps_description_and_opening_chapter_cap(tmp_path: Path) -> None:
    description = (
        "The opening stage keeps a source-named direction instead of collapsing "
        "the brief into an empty planner_proposal label."
    )
    prepared, _service = _prepare(
        tmp_path,
        brief_text="A wounded heir enters the tower.",
        plan_items=(
            _item(
                "plan.opening",
                "opening_direction",
                {"title": "Opening stage", "description": description},
            ),
            _item(
                "plan.ch1",
                "chapter_goal",
                {"chapter_index": 1, "description": "Enter the academy."},
            ),
            _item(
                "plan.ch9",
                "chapter_goal",
                {
                    "chapter_index": 9,
                    "description": "Later-volume goals stay with rolling Planner.",
                },
            ),
        ),
        world_items=(_item("world.lin", "character", {"label": "Lin", "fact": "known"}),),
    )
    summaries = {node.summary for node in prepared.document.plan.nodes}
    assert description in summaries
    assert "planner_proposal" not in summaries
    goals = prepared.document.plan.chapter_goals
    assert [goal.chapter_index for goal in goals] == [1]
    later = [node for node in prepared.document.plan.nodes if "Later-volume" in node.summary]
    assert later
    assert all(goal.chapter_index <= 5 for goal in prepared.document.plan.chapter_goals)


def test_world_patch_rejects_empty_items_with_coverage() -> None:
    _plan, world = proposals(ProjectId("project.bootstrap.novel"))
    with pytest.raises(ValidationError, match="empty Curator bootstrap"):
        WorldPatchCandidate(
            proposal_id=world.proposal_id,
            project_id=world.project_id,
            items=(),
            origin_source_ids=world.origin_source_ids,
            extraction_coverage=0.85,
            receipt=world.receipt,
        )


def test_split_composite_brief_routes_setting_style_and_late_plot() -> None:
    brief = _composite_brief()
    assert len(brief) >= COMPOSITE_BRIEF_CHARS
    sources = _split_composite_brief(brief)
    classes = {source.source_class for source in sources}
    assert SourceClass.AUTHOR_INITIAL_BRIEF in classes
    assert SourceClass.BASELINE_SETTING in classes
    assert SourceClass.STYLE_GUIDE in classes
    assert SourceClass.AUTHOR_KNOWN_FUTURE_PLAN in classes
    future = next(
        source for source in sources if source.source_class is SourceClass.AUTHOR_KNOWN_FUTURE_PLAN
    )
    text = future.data.decode("utf-8")
    assert "核心真相" in text
    assert "后期真相只属于 Plan" in text
    setting = b"\n".join(
        source.data for source in sources if source.source_class is SourceClass.BASELINE_SETTING
    ).decode("utf-8")
    assert "北城" in setting
    assert "后期真相只属于 Plan" not in setting
    short = _split_composite_brief("A wounded heir enters the tower.")
    assert [source.source_class for source in short] == [SourceClass.AUTHOR_INITIAL_BRIEF]


def test_prepare_reroutes_split_sources_to_legal_roots(tmp_path: Path) -> None:
    brief = _composite_brief()
    description = (
        "The opening stage keeps a source-named direction instead of collapsing "
        "the brief into an empty planner_proposal label."
    )
    prepared, _service = _prepare(
        tmp_path,
        brief_text=brief,
        plan_items=tuple(
            _item(
                f"plan.stage.{index}",
                "opening_direction",
                {"title": f"Stage {index}", "description": description},
                "source.baseline-setting.1",
            )
            for index in range(1, 6)
        ),
        world_items=tuple(
            _item(
                f"world.named.{index}",
                "location",
                {"label": f"City {index}", "description": f"named place {index}"},
                "source.future-plan.1",
            )
            for index in range(1, 9)
        ),
        origin_source_ids=(
            StableId("source.author-initial-brief"),
            StableId("source.style-guide.1"),
            StableId("source.future-plan.1"),
        ),
    )
    assert prepared.document.preview["validation_status"] == "passed"
    assert prepared.document.approval_request is not None
    codes = {item["code"] for item in prepared.document.preview["validation_findings"]}
    assert "BOOTSTRAP_PLAN_ROUTE_DENIED" not in codes
    assert "BOOTSTRAP_WORLD_ROUTE_DENIED" not in codes
    assert all(
        item.source_ids == (StableId("source.author-initial-brief"),)
        for item in prepared.document.plan_proposal.items
    )
    assert all(
        item.source_ids == (StableId("source.author-initial-brief"),)
        for item in prepared.document.world_patch.items
    )


def test_profile_from_brief_reads_markdown_basic_info() -> None:
    profile = _profile_from_brief(_composite_brief())
    assert profile["title"] == "样例长篇"
    assert "史诗奇幻" in str(profile["genre"])
    assert profile["target_chapters"] == 800
    assert profile["minimum_characters"] == 3000
    assert profile["maximum_characters"] == 5000
    assert "边镇学徒" in str(profile["premise"])


def test_prepare_merges_planner_world_design_into_curator_world(tmp_path: Path) -> None:
    prepared, _service = _prepare(
        tmp_path,
        brief_text="A wounded heir enters the tower.",
        plan_items=(
            _item(
                "plan.opening",
                "opening_direction",
                {
                    "title": "Opening stage",
                    "description": (
                        "The heir enters the academy under a false rank "
                        "and must keep that constraint."
                    ),
                },
            ),
        ),
        world_items=(),
        planner_world_items=(
            _item(
                "world.city",
                "location",
                {"label": "North City", "description": "Capital of the surviving league."},
            ),
            _item(
                "world.job",
                "occupation",
                {"label": "Wall Guard", "fact": "Front-line defense occupation."},
            ),
        ),
    )
    labels = {entity.internal_label for entity in prepared.document.world.entities}
    assert "North City" in labels
    assert "Wall Guard" in labels
    assert prepared.document.preview["validation_status"] == "passed"
    _plan, world = proposals(ProjectId("project.bootstrap.novel"))
    empty = world.model_copy(update={"items": (), "extraction_coverage": 0})
    planner = PlannerExecutionResult(
        mode=AgentMode.PROJECT_BOOTSTRAP,
        project_intent=ProjectIntentModel(
            intent_id=StableId("intent.merge"),
            project_id=ProjectId("project.bootstrap.novel"),
            strategy=BootstrapStrategy.DEVELOP_CANDIDATES,
            items=(),
            source_ids=(StableId("source.author-initial-brief"),),
            coverage=1,
        ),
        plan_proposal=prepared.document.plan_proposal,
        world_design=WorldDesignProposal(
            proposal_id=StableId("world-design.merge"),
            project_id=ProjectId("project.bootstrap.novel"),
            items=(_item("world.extra", "location", {"label": "Border Town", "fact": "origin"}),),
        ),
        output_artifact=prepared.artifact,
        receipt=prepared.document.plan_proposal.receipt,
    )
    merged = _merge_world_patch(empty, planner)
    assert any(item.payload.get("label") == "Border Town" for item in merged.items)


def test_composite_brief_records_profile_and_rejects_sparse_world(
    tmp_path: Path,
) -> None:
    brief = _composite_brief()
    description = (
        "The opening stage keeps a source-named direction instead of collapsing "
        "the brief into an empty planner_proposal label."
    )
    prepared, service = _prepare(
        tmp_path,
        brief_text=brief,
        plan_items=(_item("plan.opening", "opening_direction", {"title": "起点"}),),
        world_items=(_item("world.lin", "character", {"label": "Lin", "fact": "known"}),),
    )
    assert prepared.document.approval_request is None
    assert prepared.document.preview["validation_status"] == "failed"
    codes = {item["code"] for item in prepared.document.preview["validation_findings"]}
    assert "BOOTSTRAP_WORLD_SPARSE" in codes
    assert "BOOTSTRAP_PLAN_SPARSE" in codes
    assert prepared.document.profile.style_profile["title"] == "样例长篇"
    assert "史诗奇幻" in str(prepared.document.profile.style_profile["genre"])
    classes = {item.source_class for item in prepared.document.classifications}
    assert SourceClass.BASELINE_SETTING in classes
    assert SourceClass.AUTHOR_KNOWN_FUTURE_PLAN in classes
    assert SourceClass.STYLE_GUIDE in classes
    with pytest.raises(ValueError, match="sufficiency"):
        service.commit(
            prepared=prepared.document,
            author_id=StableId("author.1"),
            reason="must not commit a failed Genesis",
            target_chapters=800,
            run_id=RunId("run.bootstrap.novel"),
            object_store_root=tmp_path / "objects",
        )

    rich_plan = tuple(
        _item(
            f"plan.stage.{index}",
            "opening_direction",
            {"title": f"阶段{index}", "description": description},
        )
        for index in range(1, 6)
    )
    rich_world = tuple(
        _item(
            f"world.named.{index}",
            "location",
            {"label": f"城邦{index}", "description": f"命名地点{index}"},
        )
        for index in range(1, 9)
    )
    passed, _service = _prepare(
        tmp_path / "rich",
        brief_text=brief,
        plan_items=rich_plan,
        world_items=rich_world,
    )
    assert passed.document.preview["validation_status"] == "passed"
    assert passed.document.approval_request is not None
    assert len(passed.document.world.states) >= 8
    assert "planner_proposal" not in {node.summary for node in passed.document.plan.nodes}


def test_bootstrap_prepare_cli_binds_after_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, object]] = []
    lock_payloads: list[object] = []

    class FakeBootstrap:
        def __init__(self, **kwargs: object) -> None:
            captured.append(kwargs)

        async def prepare(
            self,
            *,
            project_id: ProjectId,
            brief_text: str,
            planning_locks: object = None,
        ) -> Any:
            lock_payloads.append(planning_locks)
            artifacts = captured[0]["artifacts"]
            assert isinstance(artifacts, ArtifactRepository)
            return SimpleNamespace(
                artifact=artifacts.put(b"prepared", "application/json", VERSION),
                document=SimpleNamespace(
                    preview={"validation_status": "failed"},
                    approval_request=None,
                    validation=SimpleNamespace(status=SimpleNamespace(value="failed")),
                ),
            )

    def _fail_bind(**_kwargs: object) -> None:
        raise AssertionError("CLI must not bind Planner/Curator before source split")

    monkeypatch.setattr(
        "novel_agent.runtime.production_novel_bootstrap.ProductionNovelBootstrap",
        FakeBootstrap,
    )
    monkeypatch.setattr(
        "novel_agent.runtime.production_novel_bootstrap.bind_bootstrap_model_agents",
        _fail_bind,
    )
    brief = tmp_path / "brief.md"
    brief.write_text(_composite_brief(), encoding="utf-8")
    prepared = tmp_path / "genesis-prepared.json"
    preview = tmp_path / "genesis-preview.json"
    url = f"sqlite+pysqlite:///{tmp_path / 'cli.db'}"
    assert (
        main(
            [
                "runtime",
                "--database-url",
                url,
                "bootstrap-prepare",
                "--brief",
                str(brief),
                "--project-id",
                "project.bootstrap.cli",
                "--object-store-root",
                str(tmp_path / "objects"),
                "--endpoint-profile",
                "deterministic_fake",
                "--prepared",
                str(prepared),
                "--preview",
                str(preview),
                "--run-id",
                "run.bootstrap.cli",
            ]
        )
        == 0
    )
    assert captured[0].get("planner") is None
    assert captured[0].get("curator") is None
    assert captured[0]["endpoints"]
    assert captured[0]["run_id"] == RunId("run.bootstrap.cli")
    assert lock_payloads == [None]
    payload = json.loads(prepared.read_text(encoding="utf-8"))
    assert payload["approval_request"] is None
    assert payload["validation_status"] == "failed"
    assert json.loads(preview.read_text(encoding="utf-8"))["validation_status"] == "failed"


def test_bootstrap_prepare_cli_passes_author_planning_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_payloads: list[object] = []

    class FakeBootstrap:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def prepare(
            self,
            *,
            project_id: ProjectId,
            brief_text: str,
            planning_locks: object = None,
        ) -> Any:
            lock_payloads.append(planning_locks)
            return SimpleNamespace(
                artifact=ArtifactRef(
                    artifact_id=ArtifactId("sha256:" + "0" * 64),
                    byte_length=1,
                    media_type="application/json",
                    schema_version=VERSION,
                ),
                document=SimpleNamespace(
                    preview={"validation_status": "failed"},
                    approval_request=None,
                    validation=SimpleNamespace(status=SimpleNamespace(value="failed")),
                ),
            )

    monkeypatch.setattr(
        "novel_agent.runtime.production_novel_bootstrap.ProductionNovelBootstrap",
        FakeBootstrap,
    )
    brief = tmp_path / "brief.md"
    brief.write_text(_composite_brief(), encoding="utf-8")
    locks = tmp_path / "planning-locks.json"
    locks.write_text(
        json.dumps(
            {
                "schema_version": "2.0.0",
                "project_id": "project.bootstrap.locks",
                "locks": [
                    {
                        "lock_id": "lock.inner-court.101",
                        "category": "timeline",
                        "description": "内府资格不得早于第二卷",
                        "not_before_chapter": 101,
                        "chapter_latest": 200,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "runtime",
                "--database-url",
                f"sqlite+pysqlite:///{tmp_path / 'locks.db'}",
                "bootstrap-prepare",
                "--brief",
                str(brief),
                "--planning-locks",
                str(locks),
                "--project-id",
                "project.bootstrap.locks",
                "--object-store-root",
                str(tmp_path / "objects"),
                "--endpoint-profile",
                "deterministic_fake",
                "--prepared",
                str(tmp_path / "prepared.json"),
                "--run-id",
                "run.bootstrap.locks",
            ]
        )
        == 0
    )
    delivered = lock_payloads[0]
    assert isinstance(delivered, AuthorPlanningLocksDocument)
    assert delivered.project_id == "project.bootstrap.locks"
    assert delivered.root_hash == author_planning_locks_content_id(delivered)


def test_bootstrap_prepare_cli_rejects_foreign_planning_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeBootstrap:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def prepare(self, **_kwargs: object) -> Any:  # pragma: no cover - must not run
            raise AssertionError("a foreign lock document must be rejected before prepare")

    monkeypatch.setattr(
        "novel_agent.runtime.production_novel_bootstrap.ProductionNovelBootstrap",
        FakeBootstrap,
    )
    brief = tmp_path / "brief.md"
    brief.write_text(_composite_brief(), encoding="utf-8")
    locks = tmp_path / "planning-locks.json"
    locks.write_text(
        json.dumps(
            {
                "schema_version": "2.0.0",
                "project_id": "project.other",
                "locks": [
                    {
                        "lock_id": "lock.other.101",
                        "category": "timeline",
                        "description": "他人的锁",
                        "not_before_chapter": 101,
                        "chapter_latest": 200,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="belong to another project"):
        main(
            [
                "runtime",
                "--database-url",
                f"sqlite+pysqlite:///{tmp_path / 'foreign.db'}",
                "bootstrap-prepare",
                "--brief",
                str(brief),
                "--planning-locks",
                str(locks),
                "--project-id",
                "project.bootstrap.locks",
                "--object-store-root",
                str(tmp_path / "objects"),
                "--endpoint-profile",
                "deterministic_fake",
                "--prepared",
                str(tmp_path / "prepared.json"),
                "--run-id",
                "run.bootstrap.locks",
            ]
        )


def test_root_document_identity_is_the_manifest_content_address() -> None:
    """The fingerprint identity must be the manifest's artifact, not the inner field.

    BootstrapRootBuilder stores each root document *including* its ``root_hash``
    field, so the stored artifact id and the field value differ.  The bootstrap
    commit and the runtime assembly each picked a different one, so every frozen
    run failed closed with RUN_CONFIGURATION_CHANGED before its first task.
    """

    from novel_agent.services.bootstrap_workflow import project_profile_root_content_id

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    artifacts = ArtifactRepository(FilesystemObjectStore(Path("/tmp")))
    project_id = ProjectId("project.bootstrap.identity")
    plan_proposal, world_patch = proposals(project_id)
    plan_proposal = plan_proposal.model_copy(
        update={"strategy": BootstrapStrategy.DEVELOP_CANDIDATES}
    )
    profile = ProjectProfileRootDocument(
        root_hash=ArtifactId("sha256:" + "0" * 64),
        schema_version=VERSION,
        style_profile={"language": "zh-CN"},
        capability_profile={"planning_constraints": {}},
        agent_specs=(
            ContractRef(
                contract_id=StableId("agent.production-bootstrap"),
                version=VERSION,
                content_hash=ArtifactId("sha256:" + "1" * 64),
            ),
        ),
        prompt_contracts=(
            PromptContractRef(
                contract_id=StableId("prompt.system-policy"),
                version=VERSION,
                content_hash=ArtifactId("sha256:" + "1" * 64),
                render_fingerprint=ArtifactId("sha256:" + "1" * 64),
            ),
        ),
        skill_contracts=(
            SkillContractRef(
                contract_id=StableId("skill.scene-composition"),
                version=VERSION,
                content_hash=ArtifactId("sha256:" + "1" * 64),
            ),
        ),
        tool_policies=(
            ContractRef(
                contract_id=StableId("agent.production-bootstrap"),
                version=VERSION,
                content_hash=ArtifactId("sha256:" + "1" * 64),
            ),
        ),
        model_profiles=("qwen38-27b-nvfp4@8003",),
    )
    profile = profile.model_copy(
        update={"root_hash": project_profile_root_content_id(profile)}
    )
    builder = BootstrapRootBuilder(artifacts)
    candidate = builder.build(
        project_id,
        StableId("bootstrap.identity"),
        TextRootDocument(
            root_hash=ArtifactId("sha256:" + "0" * 64),
            schema_version=VERSION,
            chapters=(),
        ),
        PlanRootDocument(
            root_hash=ArtifactId("sha256:" + "0" * 64),
            schema_version=VERSION,
            nodes=(),
            chapter_goals=(),
        ),
        WorldRootDocument(
            root_hash=ArtifactId("sha256:" + "0" * 64),
            schema_version=VERSION,
            source_commit=CommitId("sha256:" + "0" * 64),
            entities=(),
            states=(),
        ),
        ReferenceRootDocument(
            root_hash=ArtifactId("sha256:" + "0" * 64),
            schema_version=VERSION,
            assets=(),
        ),
        profile,
        plan_proposal,
        world_patch,
        (),
    )

    assert (
        candidate.manifest.project_profile_root.artifact_id != candidate.profile.root_hash
    ), "the fixture must reproduce the two distinct identities"
    stored = artifacts.read_verified(candidate.manifest.project_profile_root)
    assert (
        ProjectProfileRootDocument.model_validate_json(stored, strict=True).root_hash
        == candidate.profile.root_hash
    )


def test_scheduled_later_states_are_not_written_as_accepted_facts() -> None:
    """A volume-4 event must not become canon current state at chapter 1.

    Live genesis recorded "唐钧 forges 沉曜" (a volume-4 event) as
    ``accepted_world_fact`` at ordinal 0, and the Writer turns a participating
    character's state into ``Canon current state``, so the chapter was told the
    event had already happened.
    """

    from novel_agent.domain.memory import TruthClass
    from novel_agent.domain.planning_locks import load_author_planning_locks
    from novel_agent.domain.stage2 import ProposalProvenance
    from novel_agent.runtime.production_novel_bootstrap import (
        _scheduled_later_lock_texts,
        _world_root,
    )

    locks = load_author_planning_locks(
        {
            "schema_version": "2.0.0",
            "project_id": "project.bootstrap.future",
            "locks": [
                {
                    "lock_id": "lock.forge.vol4",
                    "category": "reveal",
                    "description": "第四卷：唐钧在钧炉城锻打武器沉曜",
                    "not_before_chapter": 350,
                },
                {
                    "lock_id": "lock.court.vol2",
                    "category": "reveal",
                    "description": "内府资格最早第二卷",
                    "not_before_chapter": 101,
                },
            ],
        }
    )
    # _world_root reads only the patch items, and a real WorldPatchCandidate
    # requires a full agent receipt that has nothing to do with this decision.
    world_patch = SimpleNamespace(
        items=(
            ProposedItem(
                item_id=StableId("world.item.forge"),
                kind="character",
                payload={
                    "label": "唐钧",
                    "entity_type": "character",
                    "value": "灵械师。第四卷：在钧炉城以星铁为底锻打而成武器沉曜。",
                },
                provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                source_ids=(StableId("source.author-initial-brief"),),
            ),
            ProposedItem(
                item_id=StableId("world.item.faction"),
                kind="organization",
                payload={
                    "label": "斩星府",
                    "entity_type": "organization",
                    "value": "斩星武者所属组织，内部结构包括外府与内府。",
                },
                provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                source_ids=(StableId("source.author-initial-brief"),),
            ),
        ),
    )

    world, demoted = _world_root(
        world_patch,
        VERSION,
        future_lock_texts=_scheduled_later_lock_texts(locks),
    )

    classes = {state.state_id.root: state.truth_class for state in world.states}
    assert classes["state.bootstrap.1"] is TruthClass.PREDICTION
    assert classes["state.bootstrap.2"] is TruthClass.ACCEPTED_WORLD_FACT
    assert [item[0].root for item in demoted] == ["state.bootstrap.1"]
    assert "future volume reference" in demoted[0][1]


def test_an_explicit_curator_prediction_is_classified_without_prose_matching() -> None:
    """Prose with no volume reference cannot be classified by keywords.

    ``"在钧炉城锻打而成武器沉曜"`` carries no volume number, so the wording
    heuristic misses it.  The Curator states the time intent instead, and the host
    honours it.
    """

    from novel_agent.domain.memory import TruthClass
    from novel_agent.runtime.production_novel_bootstrap import _world_root

    world_patch = SimpleNamespace(
        items=(
            ProposedItem(
                item_id=StableId("world.item.tangjun.now"),
                kind="character",
                payload={
                    "label": "唐钧",
                    "entity_type": "character",
                    "truth_class": "accepted_world_fact",
                    "description": "钧炉城灵械师。",
                },
                provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                source_ids=(StableId("source.author-initial-brief"),),
            ),
            ProposedItem(
                item_id=StableId("world.item.tangjun.forge"),
                kind="character",
                payload={
                    "label": "唐钧",
                    "entity_type": "character",
                    "truth_class": "prediction",
                    "not_before_chapter": 350,
                    "description": "在钧炉城锻打而成武器沉曜。",
                },
                provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                source_ids=(StableId("source.author-initial-brief"),),
            ),
        ),
    )

    world, demoted = _world_root(world_patch, VERSION)

    classes = {state.state_id.root: state.truth_class for state in world.states}
    assert classes["state.bootstrap.1"] is TruthClass.ACCEPTED_WORLD_FACT
    assert classes["state.bootstrap.2"] is TruthClass.PREDICTION
    assert [item[0].root for item in demoted] == ["state.bootstrap.2"]
    assert "curator declared prediction" in demoted[0][1]


def test_a_scheduled_lock_overrides_a_curator_fact_claim() -> None:
    """The author's lock wins over the model labelling the same content a fact."""

    from novel_agent.domain.memory import TruthClass
    from novel_agent.runtime.production_novel_bootstrap import _world_root

    world_patch = SimpleNamespace(
        items=(
            ProposedItem(
                item_id=StableId("world.item.forge"),
                kind="character",
                payload={
                    "label": "唐钧",
                    "entity_type": "character",
                    "truth_class": "accepted_world_fact",
                    "description": "第四卷：在钧炉城锻打而成武器沉曜。",
                },
                provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                source_ids=(StableId("source.author-initial-brief"),),
            ),
        ),
    )

    world, demoted = _world_root(world_patch, VERSION)

    assert world.states[0].truth_class is TruthClass.PREDICTION
    assert "future volume reference" in demoted[0][1]
