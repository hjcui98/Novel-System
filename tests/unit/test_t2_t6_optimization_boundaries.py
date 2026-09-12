# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.runtime.materializers import (
    PlanCandidateMaterializer,
)
from novel_agent.adapters.runtime.stage4_planner import (
    ProductionStage4InvocationFactory,
    Stage4InvocationPolicy,
)
from novel_agent.agents.plan_reviewer import apply_host_plan_review_constraints
from novel_agent.domain.artifacts import (
    ArtifactRef,
    PlanRootRef,
    ProjectProfileRootRef,
    ReferenceRootRef,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.benchmark import ChapterGoal, PlanRootDocument, TextRootDocument
from novel_agent.domain.changes import (
    ChangeOperationType,
    CuratedOperationDraftV2,
    CuratorEventRecord,
    CuratorObligationRecord,
    EvidenceCandidate,
    EvidenceSupportDisposition,
    WorldRecordKind,
)
from novel_agent.domain.creative_runtime import (
    AcceptedCandidateBinding,
    ActorKind,
    CandidateBinding,
    CandidateKind,
    PlanningLoopRequest,
)
from novel_agent.domain.generation import WritingLengthPolicy, WritingTaskContract
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
    ObligationStatus,
    WorldRootDocument,
)
from novel_agent.domain.model_calls import ModelRole
from novel_agent.domain.planning import (
    VOLUME_STRUCTURE_REQUIRED_KEYS,
    PlanningBudgets,
    PlanningLoopEventReceipt,
    PlanningLoopPhase,
    PlanReview,
    PlanReviewDraft,
    ReviewDecision,
    ReviewIssueKind,
    ReviewTargetKind,
)
from novel_agent.domain.retrieval_decision import HistoryRetrievalRequirement
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentType,
    BootstrapStrategy,
    ContextBudget,
    ContractRef,
    ExecutionStatus,
    PlannerExecutionResult,
    PlanProposal,
    ProjectIntentModel,
    ProposalProvenance,
    ProposedItem,
    RetrievalBudget,
    WorldPatchCandidate,
)
from novel_agent.domain.world import Entity, PlanLevel, PlanNode, TruthClass
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
)
from novel_agent.ports.creative_runtime import CandidateMaterializationError
from novel_agent.runtime.production_bootstrap import load_production_assembly_spec
from novel_agent.runtime.production_components import MEMORY_CONTEXT_BUDGET_TIERS
from novel_agent.runtime.production_novel_bootstrap import ProductionNovelBootstrap
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import (
    canonical_json_bytes,
    plan_root_content_id,
    sha256_id,
)
from novel_agent.services.evidence_support import EvidenceSupportGate
from novel_agent.services.task_conditioned_need_generation import (
    NeedGenerationStatus,
    TaskPlanConditionedNeedGenerator,
)
from tests.factories import make_manifest
from tests.fixtures.stage1_synthetic import make_synthetic_bundle

VERSION = SchemaVersion("1.0.0")
HASH = ArtifactId("sha256:" + "a" * 64)
COMMIT = CommitId("sha256:" + "b" * 64)
HERO = StableId("entity.hero")


def _receipt(
    agent_type: AgentType,
    mode: AgentMode,
    *,
    base_commit: CommitId | None = COMMIT,
    run_id: RunId | None = None,
    task_id: TaskId | None = None,
) -> AgentExecutionReceipt:
    run_id = run_id or RunId("run.t2-t6.boundary")
    task_id = task_id or TaskId("task.t2-t6.boundary")
    return AgentExecutionReceipt(
        receipt_id=StableId(f"receipt.{agent_type.value}.{mode.value}"),
        run_id=run_id,
        task_id=task_id,
        agent_spec=ContractRef(
            contract_id=StableId(f"agent.{agent_type.value}.{mode.value}"),
            version=VERSION,
            content_hash=HASH,
        ),
        agent_type=agent_type,
        agent_mode=mode,
        prompt_fingerprint=HASH,
        configuration_fingerprint=HASH,
        base_commit=base_commit,
        status=ExecutionStatus.SUCCEEDED,
        started_at=datetime(2026, 9, 7, tzinfo=UTC),
        completed_at=datetime(2026, 9, 7, tzinfo=UTC),
        latency_ms=1,
    )


def _bootstrap_service(
    tmp_path: Path,
    *,
    brief_text: str,
    plan_items: tuple[ProposedItem, ...],
    world_items: tuple[ProposedItem, ...],
) -> tuple[ProductionNovelBootstrap, ProjectId, object]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = build_session_factory(engine)
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    project_id = ProjectId("project.t2-t6.genesis")
    planner_receipt = _receipt(AgentType.PLANNER, AgentMode.PROJECT_BOOTSTRAP, base_commit=None)
    plan_proposal = PlanProposal(
        proposal_id=StableId("proposal.t2-t6.genesis.plan"),
        project_id=project_id,
        mode=AgentMode.PROJECT_BOOTSTRAP,
        strategy=BootstrapStrategy.DEVELOP_CANDIDATES,
        items=plan_items,
        coverage=1.0,
        receipt=planner_receipt,
    )
    planner_result = PlannerExecutionResult(
        mode=AgentMode.PROJECT_BOOTSTRAP,
        project_intent=ProjectIntentModel(
            intent_id=StableId("intent.t2-t6.genesis"),
            project_id=project_id,
            strategy=BootstrapStrategy.DEVELOP_CANDIDATES,
            items=plan_items,
            source_ids=(StableId("source.author-initial-brief"),),
            coverage=1.0,
        ),
        plan_proposal=plan_proposal,
        output_artifact=artifacts.put(b"planner", "application/json", VERSION),
        receipt=planner_receipt,
    )
    world_patch = WorldPatchCandidate(
        proposal_id=StableId("proposal.t2-t6.genesis.world"),
        project_id=project_id,
        items=world_items,
        origin_source_ids=(StableId("source.baseline-setting.1"),),
        extraction_coverage=1.0,
        receipt=_receipt(AgentType.MEMORY_CURATOR, AgentMode.BOOTSTRAP, base_commit=None),
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
    return service, project_id, engine


def _composite_brief() -> str:
    return (
        "## 基本信息\n"
        "书名：《边界测试》\n题材：史诗奇幻\n预计章节数：800\n目标字数：每章 3000-5000 字\n\n"
        "## 世界观\n北城与南城组成脆弱联盟。\n"
        "## 写作风格\nSTYLE_GUIDE_SENTINEL：叙事保持克制，避免解释性重复。\n"
        "## 核心真相（后期揭露）\nSTYLE_GUIDE_FUTURE_SENTINEL：该真相只供后期规划使用。\n"
        + ("设定填充。" * 800)
    )


def _stage4_factory(
    tmp_path: Path,
    *,
    allowed_skill_ids: tuple[StableId, ...] | None = None,
) -> tuple[ProductionStage4InvocationFactory, CommitId, ProjectId]:
    bundle = make_synthetic_bundle()
    text = next(root for root in bundle.text_roots if len(root.chapters) == 20)
    plan = bundle.plan_roots[0]
    world = bundle.world_roots[0]
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "stage4-objects"))
    text_artifact = artifacts.put(
        canonical_json_bytes(text.model_dump(mode="json")),
        "application/vnd.novel-agent.text-root+json",
        VERSION,
    )
    plan_artifact = artifacts.put(
        canonical_json_bytes(plan.model_dump(mode="json")),
        "application/vnd.novel-agent.plan-root+json",
        VERSION,
    )
    world_artifact = artifacts.put(
        canonical_json_bytes(world.model_dump(mode="json")),
        "application/vnd.novel-agent.world-root+json",
        VERSION,
    )
    profile_artifact = artifacts.put(b"{}", "application/json", VERSION)
    reference_artifact = artifacts.put(b"{}", "application/json", VERSION)
    project_id = ProjectId("project.t2-t6.stage4")
    manifest = make_manifest(project_id).model_copy(
        update={
            "text_root": TextRootRef(**text_artifact.model_dump(mode="python")),
            "plan_root": PlanRootRef(**plan_artifact.model_dump(mode="python")),
            "world_root": WorldRootRef(**world_artifact.model_dump(mode="python")),
            "project_profile_root": ProjectProfileRootRef(
                **profile_artifact.model_dump(mode="python")
            ),
            "reference_root": ReferenceRootRef(**reference_artifact.model_dump(mode="python")),
        }
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    commits = CommitService(build_session_factory(engine))
    base_commit = commits.initialize_project(manifest)
    policy = Stage4InvocationPolicy(
        budgets=PlanningBudgets(
            retrieval=RetrievalBudget(),
            context=ContextBudget(token_budget=4_000),
        ),
        configuration_fingerprint=HASH,
        model_fingerprint=HASH,
        allowed_skill_ids=allowed_skill_ids
        or (
            StableId("skill.planning-inquiry"),
            StableId("skill.planner.story"),
            StableId("skill.planner.arc_volume"),
        ),
        model_role=ModelRole.IMPLEMENTATION,
    )
    return (
        ProductionStage4InvocationFactory(
            commits=commits,
            artifacts=artifacts,
            policy=policy,
        ),
        base_commit,
        project_id,
    )


def _plan_candidate_fixture(
    tmp_path: Path,
    *,
    mode: AgentMode,
    items: tuple[ProposedItem, ...],
    horizon: tuple[int, int] | None = None,
) -> tuple[
    PlanCandidateMaterializer,
    AcceptedCandidateBinding,
    CommitService,
    ProjectId,
    ArtifactRepository,
]:
    project_id = ProjectId("project.t2-t6.materializer")
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    commits = CommitService(build_session_factory(engine))
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "materializer-objects"))
    story = PlanNode(
        plan_node_id=StableId("plan.story"),
        node_type="story",
        title="Story",
        summary="The complete story arc.",
        plan_level=PlanLevel.STORY,
        chapter_start=1,
        chapter_end=100,
    )
    volume = PlanNode(
        plan_node_id=StableId("plan.volume.1"),
        node_type="arc_volume",
        title="Volume 1",
        summary="The first volume.",
        parent_id=story.plan_node_id,
        plan_level=PlanLevel.ARC_VOLUME,
        chapter_start=1,
        chapter_end=10,
    )
    plan = PlanRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        nodes=(story, volume),
    ).model_copy(
        update={
            "root_hash": plan_root_content_id(
                PlanRootDocument(root_hash=HASH, schema_version=VERSION, nodes=(story, volume))
            )
        }
    )
    text = TextRootDocument(root_hash=HASH, schema_version=VERSION, chapters=())
    world = _world()
    refs = {
        "text_root": artifacts.put(
            canonical_json_bytes(text.model_dump(mode="json")),
            "application/vnd.novel-agent.text-root+json",
            VERSION,
        ),
        "plan_root": artifacts.put(
            canonical_json_bytes(plan.model_dump(mode="json")),
            "application/vnd.novel-agent.plan-root+json",
            VERSION,
        ),
        "world_root": artifacts.put(
            canonical_json_bytes(world.model_dump(mode="json")),
            "application/vnd.novel-agent.world-root+json",
            VERSION,
        ),
        "profile_root": artifacts.put(b"{}", "application/json", VERSION),
        "reference_root": artifacts.put(b"{}", "application/json", VERSION),
    }
    manifest = make_manifest(project_id).model_copy(
        update={
            "text_root": TextRootRef(**refs["text_root"].model_dump(mode="python")),
            "plan_root": PlanRootRef(**refs["plan_root"].model_dump(mode="python")),
            "world_root": WorldRootRef(**refs["world_root"].model_dump(mode="python")),
            "project_profile_root": ProjectProfileRootRef(
                **refs["profile_root"].model_dump(mode="python")
            ),
            "reference_root": ReferenceRootRef(**refs["reference_root"].model_dump(mode="python")),
        }
    )
    base_commit = commits.initialize_project(manifest)
    receipt = _receipt(AgentType.PLANNER, mode, base_commit=base_commit)
    proposal = PlanProposal(
        proposal_id=StableId(f"proposal.{mode.value}.boundary"),
        project_id=project_id,
        mode=mode,
        base_commit=base_commit,
        items=items,
        coverage=1.0,
        receipt=receipt,
    )
    proposal_ref = artifacts.put(
        canonical_json_bytes(proposal.model_dump(mode="json")),
        "application/vnd.novel-agent.plan-proposal+json",
        VERSION,
    )
    reviewer_receipt = _receipt(
        AgentType.PLAN_REVIEWER,
        mode,
        base_commit=base_commit,
    ).model_copy(update={"input_artifacts": (proposal_ref,)})
    review = PlanReview(
        review_id=StableId(f"review.{mode.value}.boundary"),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_artifact_ref=proposal_ref,
        decision=ReviewDecision.ACCEPT,
        receipt=reviewer_receipt,
    )
    review_ref = artifacts.put(
        canonical_json_bytes(review.model_dump(mode="json")),
        "application/vnd.novel-agent.plan-review+json",
        VERSION,
    )
    execution = PlannerExecutionResult(
        mode=mode,
        plan_proposal=proposal,
        output_artifact=artifacts.put(b"planner-output", "application/json", VERSION),
        receipt=receipt,
    )
    execution_ref = artifacts.put(
        canonical_json_bytes(execution.model_dump(mode="json")),
        "application/vnd.novel-agent.planner-execution-result+json",
        VERSION,
    )
    event = PlanningLoopEventReceipt(
        event_id=StableId(f"event.{mode.value}.boundary"),
        request_id=StableId(f"request.{mode.value}.boundary"),
        phase=PlanningLoopPhase.PLAN_REVIEWED,
        event_kind="plan.review_settled",
        artifact_refs=(review_ref, execution_ref),
    )
    event_ref = artifacts.put(
        canonical_json_bytes(event.model_dump(mode="json")),
        "application/vnd.novel-agent.planning-loop-event+json",
        VERSION,
    )
    candidate = CandidateBinding(
        candidate_id=StableId(f"candidate.{mode.value}.boundary"),
        kind=CandidateKind.PLAN,
        artifact_ref=proposal_ref,
        candidate_hash=proposal_ref.artifact_id.root,
        basis_commit=base_commit,
        basis_snapshot=StableId("snapshot.t2-t6.materializer"),
        lineage_artifact_refs=(review_ref, event_ref),
        horizon_start=None if horizon is None else horizon[0],
        horizon_end=None if horizon is None else horizon[1],
    )
    accepted = AcceptedCandidateBinding(
        acceptance_id=StableId(f"acceptance.{mode.value}.boundary"),
        command_id=StableId(f"command.{mode.value}.boundary"),
        project_id=project_id,
        run_id=RunId("run.t2-t6.materializer"),
        task_id=TaskId(f"task.{mode.value}.boundary"),
        candidate=candidate,
        actor_kind=ActorKind.AUTHOR,
        actor_id="author",
        accepted_at=datetime(2026, 9, 7, tzinfo=UTC),
        expected_project_commit=base_commit,
    )
    return (
        PlanCandidateMaterializer(artifacts, commits, schema_version=VERSION),
        accepted,
        commits,
        project_id,
        artifacts,
    )


def _review() -> PlanReviewDraft:
    return PlanReviewDraft(
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        decision=ReviewDecision.ACCEPT,
    )


def _world() -> WorldRootDocument:
    return WorldRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        source_commit=COMMIT,
        entities=(Entity(entity_id=HERO, entity_type="character", internal_label="Hero"),),
    )


def _task(chapter: int = 21) -> BenchmarkTaskContract:
    return BenchmarkTaskContract(
        task_id=StableId("task.t2-t6.boundary"),
        task_text="prepare the bounded writer context",
        checkpoint_chapter=chapter - 1,
        target_chapter_start=chapter,
        target_chapter_end=chapter,
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        task_template_version="test.v1",
        output_contract_version="test.v1",
        task_intent="write the target chapter",
    )


def _writing_task(chapter: int = 21) -> WritingTaskContract:
    return WritingTaskContract(
        contract_id=StableId("writing-contract.t2-t6"),
        target_chapter=chapter,
        target_scenes=(StableId(f"scene.chapter.{chapter}.0"),),
        pov="third-person limited",
        narrative_person="third person limited",
        chapter_goal="advance the current goal",
        length_policy=WritingLengthPolicy(
            minimum_characters=3_000,
            target_characters=4_000,
            maximum_characters=5_000,
        ),
    )


def _candidate(text: str, candidate_id: str) -> EvidenceCandidate:
    return EvidenceCandidate(
        candidate_id=StableId(candidate_id),
        block_id=StableId("block.chapter.1"),
        chapter_index=1,
        scene_index=0,
        text=text,
        start=0,
        end=len(text),
        content_hash=sha256_id(text.encode("utf-8")),
    )


def _stage4_request(
    *,
    project_id: ProjectId,
    basis_commit: CommitId,
    plan_level: PlanLevel,
) -> PlanningLoopRequest:
    source = ArtifactRef(
        artifact_id=HASH,
        media_type="text/plain",
        byte_length=1,
        schema_version=VERSION,
    )
    return PlanningLoopRequest(
        run_id=RunId("run.t2-t6.stage4"),
        task_id=TaskId(f"task.t2-t6.stage4.{plan_level.value}"),
        project_id=project_id,
        basis_commit=basis_commit,
        basis_snapshot=StableId("snapshot.t2-t6.stage4"),
        input_artifact_refs=(source,),
        chapter_index=20,
        plan_level=plan_level,
    )


def test_stage4_story_public_invocation_loads_story_skill_and_rejects_bad_allowlist(
    tmp_path: Path,
) -> None:
    factory, base_commit, project_id = _stage4_factory(tmp_path)
    invocation = factory(
        _stage4_request(
            project_id=project_id,
            basis_commit=base_commit,
            plan_level=PlanLevel.STORY,
        )
    )
    assert StableId("skill.planning-inquiry") in invocation.request.allowed_skill_ids
    assert StableId("skill.planner.story") in invocation.request.allowed_skill_ids
    assert invocation.request.task.creative_scope == ("level:story", "purpose:normal")

    restricted, base_commit, project_id = _stage4_factory(
        tmp_path / "restricted",
        allowed_skill_ids=(StableId("skill.planning-inquiry"),),
    )
    with pytest.raises(ValueError, match="missing required skills"):
        restricted(
            _stage4_request(
                project_id=project_id,
                basis_commit=base_commit,
                plan_level=PlanLevel.STORY,
            )
        )


def test_stage4_arc_public_invocation_loads_arc_skill_without_initial_alternative(
    tmp_path: Path,
) -> None:
    factory, base_commit, project_id = _stage4_factory(tmp_path)
    invocation = factory(
        _stage4_request(
            project_id=project_id,
            basis_commit=base_commit,
            plan_level=PlanLevel.ARC_VOLUME,
        )
    )
    assert StableId("skill.planner.arc_volume") in invocation.request.allowed_skill_ids
    assert "alternative" not in " ".join(invocation.request.task.creative_scope)


def test_genesis_prepare_keeps_style_guide_in_profile_and_plan_payload_publicly(
    tmp_path: Path,
) -> None:
    plan_items = tuple(
        ProposedItem(
            item_id=StableId(f"plan.boundary.{index}"),
            kind="story_direction",
            payload={
                "title": f"阶段 {index}",
                "description": (f"这一阶段的完整叙事方向 {index} 保留作者约束并推进主要冲突。"),
            },
            provenance=ProposalProvenance.AUTHOR_SUPPLIED,
            source_ids=(StableId("source.style-guide.1"),),
        )
        for index in range(1, 6)
    )
    world_items = tuple(
        ProposedItem(
            item_id=StableId(f"world.boundary.{index}"),
            kind="location",
            payload={"label": f"地点 {index}", "description": f"稳定世界事实 {index}"},
            provenance=ProposalProvenance.AUTHOR_SUPPLIED,
            source_ids=(StableId("source.baseline-setting.1"),),
        )
        for index in range(1, 9)
    )
    service, project_id, engine = _bootstrap_service(
        tmp_path,
        brief_text=_composite_brief(),
        plan_items=plan_items,
        world_items=world_items,
    )
    try:
        prepared = asyncio.run(
            service.prepare(project_id=project_id, brief_text=_composite_brief())
        )
    finally:
        engine.dispose()

    style_sources = prepared.document.profile.style_profile.get("style_guide_sources")
    assert isinstance(style_sources, list)
    assert any("STYLE_GUIDE_SENTINEL" in str(item) for item in style_sources)
    assert all(
        item.source_ids == (StableId("source.author-initial-brief"),)
        for item in prepared.document.plan_proposal.items
    )
    node = next(
        node for node in prepared.document.plan.nodes if node.plan_node_id == plan_items[0].item_id
    )
    assert node.payload == plan_items[0].payload


def _volume_slots(index: int, *, slots: bool = True) -> dict[str, object]:
    """Build one volume payload with a readable responsibility table.

    ``obligation_plan`` is part of ``VOLUME_STRUCTURE_REQUIRED_KEYS``, but it is a
    contract-bearing slot rather than an opaque string: host review now rejects an
    unreadable responsibility table, so the fixture must declare a real entry.
    """

    payload: dict[str, object] = {
        key: f"{key}.{index}" for key in VOLUME_STRUCTURE_REQUIRED_KEYS
    }
    if slots:
        payload["obligation_plan"] = [
            {
                "kind": "objective",
                "summary": f"volume.{index} responsibility",
                "not_before_chapter": index * 100 + 1,
                "setup_window": f"{index * 100 + 1}-{index * 100 + 40}",
                "progress_windows": [f"{index * 100 + 41}-{index * 100 + 70}"],
                "payoff_window": f"{index * 100 + 71}-{(index + 1) * 100}",
            }
        ]
    else:
        payload.pop("obligation_plan", None)
    return payload


def test_plan_review_rejects_three_volumes_when_profile_requires_eight() -> None:
    def payload(count: int, *, slots: bool = True) -> str:
        return json.dumps(
            {
                "expected_volume_count": 8,
                "target_chapters": 800,
                "items": [
                    {
                        "item_id": f"volume.{index}",
                        "kind": "arc_volume",
                        "payload": {
                            "plan_level": "arc_volume",
                            "chapter_start": index * 800 // count + 1,
                            "chapter_end": (index + 1) * 800 // count,
                            **_volume_slots(index, slots=slots),
                        },
                    }
                    for index in range(count)
                ],
            }
        )

    incomplete = apply_host_plan_review_constraints(
        _review(),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=payload(3),
    )
    complete = apply_host_plan_review_constraints(
        _review(),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=payload(8),
    )
    missing_slots = apply_host_plan_review_constraints(
        _review(),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=payload(8, slots=False),
    )

    assert incomplete.decision is ReviewDecision.REVISE
    assert any(issue.kind is ReviewIssueKind.COVERAGE for issue in incomplete.issues)
    assert complete.decision is ReviewDecision.ACCEPT
    assert complete.issues == ()
    assert missing_slots.decision is ReviewDecision.REVISE
    assert any(
        issue.kind is ReviewIssueKind.VOLUME_STRUCTURE_INCOMPLETE
        for issue in missing_slots.issues
    )


def test_plan_review_blocks_missing_or_early_future_payoff() -> None:
    missing_window = apply_host_plan_review_constraints(
        _review(),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=json.dumps(
            {
                "items": [
                    {
                        "item_id": "obligation.future",
                        "kind": "promise",
                        "payload": {"obligation_kind": "promise", "description": "later"},
                    }
                ]
            }
        ),
    )
    early_payoff = apply_host_plan_review_constraints(
        _review(),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=json.dumps(
            {
                "items": [
                    {
                        "item_id": "obligation.future",
                        "kind": "promise",
                        "payload": {
                            "obligation_kind": "promise",
                            "description": "later",
                            "not_before_chapter": 20,
                            "chapter_index": 10,
                            "status": "resolved",
                        },
                    }
                ]
            }
        ),
    )

    assert missing_window.decision is ReviewDecision.HUMAN_REQUIRED
    assert any(
        issue.kind is ReviewIssueKind.LONG_RANGE_PAYOFF_WITHOUT_TIME_WINDOW
        for issue in missing_window.issues
    )
    assert any(
        issue.kind is ReviewIssueKind.EARLY_RESOLUTION_OF_FUTURE_LOCKED_OBLIGATION
        for issue in early_payoff.issues
    )


def test_chapter_set_public_materialization_creates_wrapper_and_five_children(
    tmp_path: Path,
) -> None:
    items = tuple(
        ProposedItem(
            item_id=StableId(f"plan.chapter.{index}"),
            kind="chapter",
            payload={"chapter_index": index, "summary": f"完成第 {index} 章目标。"},
            provenance=ProposalProvenance.PLANNER_PROPOSED,
        )
        for index in range(6, 11)
    )
    materializer, accepted, _commits, _project_id, artifacts = _plan_candidate_fixture(
        tmp_path,
        mode=AgentMode.CHAPTER_SET,
        items=items,
        horizon=(6, 10),
    )
    bundle, _report = materializer.materialize(accepted)
    plan = PlanRootDocument.model_validate_json(
        artifacts.read_verified(bundle.proposed_roots.plan_root),
        strict=True,
    )
    wrappers = tuple(node for node in plan.nodes if node.plan_level is PlanLevel.CHAPTER_SET)
    chapters = tuple(node for node in plan.nodes if node.plan_level is PlanLevel.CHAPTER)
    assert len(wrappers) == 1
    assert len(chapters) == 5
    assert all(node.parent_id == wrappers[0].plan_node_id for node in chapters)
    assert {goal.chapter_index for goal in plan.chapter_goals} == set(range(6, 11))
    for label, parent_id in (
        ("volume", "plan.volume.1"),
        ("proposal", "plan.chapter.7"),
    ):
        wrong_parent_items = tuple(
            item.model_copy(
                update={
                    "item_id": StableId(f"plan.chapter.wrong.{label}.{index}"),
                    "payload": {**item.payload, "parent_id": parent_id},
                }
            )
            if index == 6
            else item.model_copy(
                update={"item_id": StableId(f"plan.chapter.wrong.{label}.{index}")}
            )
            for index, item in enumerate(items, start=6)
        )
        with pytest.raises(CandidateMaterializationError, match="current CHAPTER_SET wrapper"):
            wrong_materializer, wrong_accepted, *_ = _plan_candidate_fixture(
                tmp_path / f"wrong-parent-{label}",
                mode=AgentMode.CHAPTER_SET,
                items=wrong_parent_items,
                horizon=(6, 10),
            )
            wrong_materializer.materialize(wrong_accepted)
    invalid_items = tuple(
        item.model_copy(
            update={
                "item_id": StableId(f"plan.chapter.invalid.{index}"),
                "payload": {"chapter_index": index, "summary": f"完成第 {index} 章目标。"},
            }
        )
        for item, index in zip(items, range(11, 16), strict=True)
    )
    with pytest.raises(CandidateMaterializationError, match="covering ARC_VOLUME"):
        invalid_materializer, invalid_accepted, *_ = _plan_candidate_fixture(
            tmp_path / "invalid-horizon",
            mode=AgentMode.CHAPTER_SET,
            items=invalid_items,
            horizon=(11, 15),
        )
        invalid_materializer.materialize(invalid_accepted)


def test_plan_public_materialization_commits_obligation_atomically_and_rejects_chapter_declaration(
    tmp_path: Path,
) -> None:
    declaration = ProposedItem(
        item_id=StableId("plan.volume.promise"),
        kind="arc_volume",
        payload={
            "description": "The sealed gate must be opened after the midpoint.",
            "chapter_start": 1,
            "chapter_end": 10,
            "obligation": {
                "kind": "promise",
                "description": "Open the sealed gate later.",
                "not_before_chapter": 30,
            },
        },
        provenance=ProposalProvenance.PLANNER_PROPOSED,
    )
    materializer, accepted, commits, project_id, artifacts = _plan_candidate_fixture(
        tmp_path,
        mode=AgentMode.ARC_VOLUME,
        items=(declaration,),
    )
    bundle, _report = materializer.materialize(accepted)
    world = WorldRootDocument.model_validate_json(
        artifacts.read_verified(bundle.proposed_roots.world_root),
        strict=True,
    )
    assert world.obligations[0].status is ObligationStatus.OPEN
    assert commits.current_commit(project_id) == accepted.expected_project_commit

    chapter_declaration = ProposedItem(
        item_id=StableId("plan.chapter.forbidden-promise"),
        kind="chapter",
        payload={
            "chapter_index": 6,
            "summary": "Chapter cannot declare a new obligation.",
            "obligation": {
                "kind": "promise",
                "description": "Forbidden chapter declaration.",
                "not_before_chapter": 30,
            },
        },
        provenance=ProposalProvenance.PLANNER_PROPOSED,
    )
    forbidden, forbidden_accepted, forbidden_commits, forbidden_project, _ = (
        _plan_candidate_fixture(
            tmp_path / "forbidden",
            mode=AgentMode.CHAPTER_SET,
            items=(chapter_declaration,),
            horizon=(6, 6),
        )
    )
    before = forbidden_commits.current_commit(forbidden_project)
    with pytest.raises(CandidateMaterializationError, match="may not declare"):
        forbidden.materialize(forbidden_accepted)
    assert forbidden_commits.current_commit(forbidden_project) == before


def test_event_evidence_positive_and_missing_result_are_distinct() -> None:
    gate = EvidenceSupportGate()
    operation = CuratedOperationDraftV2(
        operation=ChangeOperationType.CREATE,
        record_kind=WorldRecordKind.EVENT,
        target_id=StableId("event.awakening"),
        record=CuratorEventRecord(event_type="awakening", truth_class=TruthClass.ASSERTION),
        evidence_candidate_ids=(StableId("e.event"),),
    )
    positive = gate.evaluate_operation(
        operation_index=0,
        operation=operation,
        candidates=(_candidate("The awakening happened at the gate.", "e.event"),),
    )[0]
    negative = gate.evaluate_operation(
        operation_index=0,
        operation=operation,
        candidates=(_candidate("He walked home without incident.", "e.event"),),
    )[0]

    assert positive.disposition is EvidenceSupportDisposition.SUPPORTS
    assert negative.disposition is EvidenceSupportDisposition.PARTIAL
    assert negative.reason_code == "TYPE_AWARE_EVENT_OR_OBLIGATION_NEEDS_SEMANTIC_VERIFIER"


def test_obligation_evidence_positive_and_missing_promise_are_distinct() -> None:
    gate = EvidenceSupportGate()
    operation = CuratedOperationDraftV2(
        operation=ChangeOperationType.CREATE,
        record_kind=WorldRecordKind.OBLIGATION,
        target_id=StableId("obligation.gate"),
        record=CuratorObligationRecord(
            kind="objective",
            description="enter the tower",
            status="open",
        ),
        evidence_candidate_ids=(StableId("e.obligation"),),
    )
    positive = gate.evaluate_operation(
        operation_index=0,
        operation=operation,
        candidates=(_candidate("The objective is clear: enter the tower.", "e.obligation"),),
    )[0]
    negative = gate.evaluate_operation(
        operation_index=0,
        operation=operation,
        candidates=(_candidate("Silence.", "e.obligation"),),
    )[0]

    assert positive.disposition is EvidenceSupportDisposition.SUPPORTS
    assert negative.disposition is EvidenceSupportDisposition.PARTIAL
    assert negative.reason_code == "TYPE_AWARE_EVENT_OR_OBLIGATION_NEEDS_SEMANTIC_VERIFIER"


def test_missing_history_decision_is_invalid_and_does_not_expand() -> None:
    goal = ChapterGoal(
        goal_id=StableId("goal.chapter.21"),
        chapter_index=21,
        summary="write from canonical state",
    )
    plan = PlanRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        chapter_goals=(goal,),
    )
    result = TaskPlanConditionedNeedGenerator().generate_for_writing_task(
        _task(), _writing_task(), _world(), plan, None
    )

    assert result.status is NeedGenerationStatus.INVALID
    assert result.retrieval_requirement is HistoryRetrievalRequirement.UNDECIDED
    assert result.needs == ()
    assert len(MEMORY_CONTEXT_BUDGET_TIERS) == 3


def test_two_declared_history_needs_are_bounded_to_two() -> None:
    goal = ChapterGoal(
        goal_id=StableId("goal.chapter.21"),
        chapter_index=21,
        summary="write with two explicit history questions",
        payload={
            "history_needs": [
                {"kind": "causal_history", "query": "where did the key come from"},
                {"kind": "setup_evidence", "query": "when was the gate first shown"},
            ]
        },
    )
    plan = PlanRootDocument(root_hash=HASH, schema_version=VERSION, chapter_goals=(goal,))
    result = TaskPlanConditionedNeedGenerator().generate_for_writing_task(
        _task(), _writing_task(), _world(), plan, None
    )

    assert len(result.needs) == 2
    assert {need.need_type for need in result.needs} == {
        "causal_history",
        "setup_evidence",
    }
    assert all(need.requirement.value == "mandatory" for need in result.needs)
    assert tuple(item[1] for item in MEMORY_CONTEXT_BUDGET_TIERS) == (24_000, 48_000, 72_000)
    assert tuple(item[3] for item in MEMORY_CONTEXT_BUDGET_TIERS) == (12, 24, 36)


def test_profile_and_writer_skill_policy_are_project_configured() -> None:
    spec = load_production_assembly_spec()
    optional = {
        "skill.character-voice-writing",
        "skill.dialogue-subtext-writing",
        "skill.pov-epistemic-writing",
        "skill.pacing-transition-writing",
        "skill.hook-foreshadowing-writing",
    }

    assert {item.root for item in spec.skills_for_writer()} >= {
        "skill.scene-composition",
        "skill.style-genre-writing",
    }
    assert optional.issubset({item.root for item in spec.skills_for_writer()})
    assert {item.root for item in spec.skills_for_planner()} >= {
        "skill.planning-inquiry",
        "skill.planner.story",
        "skill.planner.arc_volume",
    }


def test_draft_surface_gate_is_public_and_rejects_internal_future_or_copy_text() -> None:
    from novel_agent.services.writer_cognition import draft_surface_error

    assert draft_surface_error("正文\nPLANNER_future chapter") is not None
    assert draft_surface_error("正文\n<EVALUATOR verdict=PASS>") is not None
    assert (
        draft_surface_error(
            "连续的新叙述。" * 120,
            recent_prose=(("连续的新叙述。" * 120, False),),
        )
        is not None
    )
    assert draft_surface_error("林澈推开门，风从塔内涌出。", target_language="zh-CN") is None
    assert draft_surface_error("钟声rhythmic地响起。", target_language="zh-CN") is not None
    assert draft_surface_error("researchers走进大厅。", target_language="zh-CN") is not None
    assert draft_surface_error("接口ER-07开启。", target_language="zh-CN") is not None
    assert (
        draft_surface_error(
            "接口ER-07开启。",
            target_language="zh-CN",
            allowed_language_tokens=("ER-07",),
        )
        is None
    )
    assert (
        draft_surface_error(
            "接口ER-07开启。",
            target_language="zh-CN",
            allowed_language_tokens=("ER",),
        )
        is not None
    )
    assert draft_surface_error("The tower opens.", target_language="en") is None


def test_canary_policy_is_candidate_only_through_public_genesis_commit(tmp_path: Path) -> None:
    item = ProposedItem(
        item_id=StableId("plan.candidate-only"),
        kind="story_direction",
        payload={
            "title": "Opening direction",
            "description": (
                "The opening direction preserves the author constraint and advances the conflict."
            ),
        },
        provenance=ProposalProvenance.AUTHOR_SUPPLIED,
        source_ids=(StableId("source.author-initial-brief"),),
    )
    world_item = ProposedItem(
        item_id=StableId("world.candidate-only"),
        kind="location",
        payload={"label": "North Tower", "description": "A named location."},
        provenance=ProposalProvenance.AUTHOR_SUPPLIED,
        source_ids=(StableId("source.author-initial-brief"),),
    )
    service, project_id, engine = _bootstrap_service(
        tmp_path,
        brief_text="A wounded heir enters the tower.",
        plan_items=(item,),
        world_items=(world_item,),
    )
    try:
        prepared = asyncio.run(
            service.prepare(project_id=project_id, brief_text="A wounded heir enters the tower.")
        )
        policy, request, descriptor = service.commit(
            prepared=prepared.document,
            author_id=StableId("author.boundary"),
            reason="boundary approval",
            target_chapters=2,
            run_id=RunId("run.candidate-only"),
            object_store_root=tmp_path / "runtime",
        )
    finally:
        engine.dispose()

    assert policy.auto_accept_plan is False
    assert policy.auto_accept_draft is False
    assert request.policy.policy_hash == policy.policy_hash
    assert descriptor.stop_after_chapter == 2


def test_host_review_blocks_structured_unresolved_conflict_affecting_window() -> None:
    payload = json.dumps(
        {
            "items": [
                {
                    "item_id": "ch7_plan_item",
                    "kind": "chapter",
                    "payload": {"chapter_index": 7, "plan_level": "chapter"},
                }
            ],
            "unresolved": [
                {
                    "issue_id": "plan-issue.ch7.inner-court",
                    "kind": "AUTHOR_INTENT_CONFLICT",
                    "summary": "第7章进入内府与第三卷进入内府冲突",
                    "affected_chapters": [7],
                    "blocking": False,
                    "forbidden_assumptions": ["第7章已正式取得内府身份"],
                }
            ],
        }
    )
    reviewed = apply_host_plan_review_constraints(
        _review(),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=payload,
    )

    assert reviewed.decision is ReviewDecision.REVISE
    assert any(
        issue.kind is ReviewIssueKind.BLOCKING_UNRESOLVED and issue.blocking
        for issue in reviewed.issues
    )


def test_materializer_propagates_advisories_and_rejects_blocking_unresolved() -> None:
    from novel_agent.domain.stage2 import (
        PlanProposal,
        PlanUnresolvedIssue,
        PlanUnresolvedKind,
    )

    issue = PlanUnresolvedIssue(
        issue_id=StableId("plan-issue.advisory.state"),
        kind=PlanUnresolvedKind.CURRENT_STATE_UNKNOWN,
        summary="伤情恢复程度未知",
        affected_chapters=(21,),
        blocking=False,
        forbidden_assumptions=("不得假设伤势已经完全康复",),
    )
    proposal = PlanProposal.model_construct(
        proposal_id=StableId("proposal.advisory"),
        unresolved=(issue,),
    )
    goal = ChapterGoal(
        goal_id=StableId("goal.chapter.21"),
        chapter_index=21,
        summary="Enter the tower.",
    )
    propagated = PlanCandidateMaterializer._propagate_unresolved_advisories((goal,), proposal)
    advisories = propagated[0].payload["unresolved_advisories"]
    assert isinstance(advisories, list)
    assert advisories[0]["summary"] == "伤情恢复程度未知"
    assert advisories[0]["forbidden_assumptions"] == ["不得假设伤势已经完全康复"]

    blocking = issue.model_copy(update={"blocking": True})
    with pytest.raises(CandidateMaterializationError, match="blocking unresolved"):
        PlanCandidateMaterializer._propagate_unresolved_advisories(
            (goal,),
            PlanProposal.model_construct(
                proposal_id=StableId("proposal.blocking"),
                unresolved=(blocking,),
            ),
        )


def test_host_review_blocks_volume_range_gap() -> None:
    ranges = [(index * 100 + 1, (index + 1) * 100) for index in range(8)]
    ranges[3] = (301, 450)
    ranges[4] = (452, 500)
    payload = json.dumps(
        {
            "expected_volume_count": 8,
            "target_chapters": 800,
            "items": [
                {
                    "item_id": f"volume.{index}",
                    "kind": "arc_volume",
                    "payload": {
                        "plan_level": "arc_volume",
                        "chapter_start": start,
                        "chapter_end": end,
                        **_volume_slots(index),
                    },
                }
                for index, (start, end) in enumerate(ranges)
            ],
        }
    )
    reviewed = apply_host_plan_review_constraints(
        _review(),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=payload,
    )

    assert reviewed.decision is ReviewDecision.REVISE
    assert any(
        issue.kind is ReviewIssueKind.COVERAGE and "VOLUME_RANGE" in issue.summary
        for issue in reviewed.issues
    )
