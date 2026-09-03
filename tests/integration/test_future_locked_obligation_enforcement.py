from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from novel_agent.adapters.runtime.materializers import PlanCandidateMaterializer
from novel_agent.adapters.runtime.stage3_writer import ProductionWritingRequestFactory
from novel_agent.domain.artifacts import ArtifactRef, RootKind, RootManifest
from novel_agent.domain.benchmark import ChapterGoal, PlanRootDocument, TextRootDocument
from novel_agent.domain.changes import (
    CandidateChangeBundle,
    ChangeOperation,
    ChangeOperationType,
    ObservedChangeSet,
    ValidationStatus,
    WorldRecordKind,
)
from novel_agent.domain.creative_runtime import CandidateBinding, CandidateKind
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
)
from novel_agent.domain.memory import (
    ObligationKind,
    ObligationStatus,
    PlanObligation,
    WorldRootDocument,
)
from novel_agent.domain.planning import PlannerContextSection
from novel_agent.domain.runtime import TaskKind, TaskRecord, TaskStatus
from novel_agent.domain.stage2 import AgentMode, PlanProposal, ProposalProvenance, ProposedItem
from novel_agent.domain.world import Entity, PlanLevel, PlanNode
from novel_agent.ports.creative_runtime import CandidateMaterializationError
from novel_agent.services.content_addressing import world_root_content_id
from novel_agent.services.planner_context_assembler import PlannerContextAssembler
from novel_agent.services.validation import Stage1Validator
from tests.factories import make_manifest
from tests.unit.test_stage4_planning_contracts import _inquiry, _put, _repo, _request

HASH = ArtifactId("sha256:" + "1" * 64)
COMMIT = CommitId("sha256:" + "a" * 64)
VERSION = SchemaVersion("1.0.0")
YINMING = StableId("obligation.yinming")


def _yinming(*, status: ObligationStatus = ObligationStatus.OPEN) -> PlanObligation:
    return PlanObligation(
        obligation_id=YINMING,
        kind=ObligationKind.PROMISE,
        description="银铭最终获得",
        status=status,
        not_before_chapter=85,
        target_chapter_start=90,
        target_chapter_end=100,
    )


def _world(obligation: PlanObligation) -> WorldRootDocument:
    entity = Entity(
        entity_id=StableId("entity.lin"),
        entity_type="character",
        internal_label="林澈",
    )
    world = WorldRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        source_commit=COMMIT,
        entities=(entity,),
        obligations=(obligation,),
    )
    return world.model_copy(update={"root_hash": world_root_content_id(world)})


def _text(*, last_chapter: int = 24) -> TextRootDocument:
    from novel_agent.domain.benchmark import ChapterDocument, SceneDocument
    from novel_agent.domain.text import TextBlock

    chapters = tuple(
        ChapterDocument(
            chapter_id=StableId(f"chapter.{index}"),
            chapter_index=index,
            title=f"Chapter {index}",
            scenes=(
                SceneDocument(
                    scene_id=StableId(f"scene.{index}"),
                    scene_index=0,
                    blocks=(
                        TextBlock(
                            block_id=StableId(f"block.{index}"),
                            chapter_id=StableId(f"chapter.{index}"),
                            scene_id=StableId(f"scene.{index}"),
                            narrative_index=0,
                            text="visible text",
                        ),
                    ),
                ),
            ),
        )
        for index in range(1, last_chapter + 1)
    )
    return TextRootDocument(root_hash=HASH, schema_version=VERSION, chapters=chapters)


def _plan() -> PlanRootDocument:
    parent = PlanNode(
        plan_node_id=StableId("plan.volume1"),
        node_type="arc_volume",
        title="Volume 1",
        summary="Opening volume.",
    )
    current = PlanNode(
        plan_node_id=StableId("plan.24-28"),
        node_type="chapter_set",
        title="Chapters 24-28",
        summary="Local investigation.",
        parent_id=parent.plan_node_id,
        obligation_ids=(YINMING,),
    )
    far = PlanNode(
        plan_node_id=StableId("plan.volume8"),
        node_type="arc_volume",
        title="Volume 8 银铭终局",
        summary="银铭最终获得与终局真相。",
    )
    return PlanRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        nodes=(parent, current, far),
        chapter_goals=(
            ChapterGoal(
                goal_id=StableId("goal.24"),
                chapter_index=24,
                summary="Investigate the wreck.",
                obligation_ids=(YINMING,),
            ),
        ),
    )


def test_chapter_set_projection_excludes_unrelated_story_root_nodes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    brief = _put(repo, "完整作者 brief: 卷六真相与第700章 payoff.")
    story = PlanNode(
        plan_node_id=StableId("plan.story.core"),
        node_type="story",
        title="Core conflict",
        summary="全书核心冲突",
        plan_level=PlanLevel.STORY,
    )
    volume6 = PlanNode(
        plan_node_id=StableId("plan.story.volume6"),
        node_type="story",
        title="Volume 6 truth",
        summary="卷六真相",
        plan_level=PlanLevel.STORY,
        chapter_start=501,
        chapter_end=600,
    )
    ending = PlanNode(
        plan_node_id=StableId("plan.story.ending"),
        node_type="story",
        title="Ending payoff",
        summary="第700章 payoff",
        plan_level=PlanLevel.STORY,
        chapter_start=700,
        chapter_end=800,
    )
    volume1 = PlanNode(
        plan_node_id=StableId("plan.volume1"),
        node_type="arc_volume",
        title="Volume 1",
        summary="First volume local arc.",
        parent_id=story.plan_node_id,
        plan_level=PlanLevel.ARC_VOLUME,
        chapter_start=1,
        chapter_end=100,
    )
    current = PlanNode(
        plan_node_id=StableId("plan.set.24-28"),
        node_type="chapter_set",
        title="Chapters 24-28",
        summary="Local investigation.",
        parent_id=volume1.plan_node_id,
        plan_level=PlanLevel.CHAPTER_SET,
        chapter_start=24,
        chapter_end=28,
        obligation_ids=(YINMING,),
    )
    plan = PlanRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        nodes=(story, volume6, ending, volume1, current),
        chapter_goals=(
            ChapterGoal(
                goal_id=StableId("goal.24"),
                chapter_index=24,
                summary="Investigate the wreck.",
                obligation_ids=(YINMING,),
            ),
        ),
    )
    world = _world(_yinming())
    plan_ref = _put(repo, plan.model_dump_json())
    world_ref = _put(repo, world.model_dump_json())
    text_ref = _put(repo, _text().model_dump_json())
    request = _request(
        AgentMode.CHAPTER_SET,
        brief,
        accepted=(plan_ref, world_ref, text_ref),
    ).model_copy(update={"horizon_start": 24, "horizon_end": 28})
    inquiry = _inquiry(AgentMode.CHAPTER_SET, brief)
    inquiry_ref = _put(repo, inquiry.model_dump_json())
    from novel_agent.domain.memory import ContextBudgetReport, Stage1ContextPackage

    memory = Stage1ContextPackage(
        context_id=StableId("context.hierarchy"),
        base_commit=request.task.base_commit,
        snapshot_id=request.snapshot_id or StableId("snapshot.stage4"),
        task_contract="stage4",
        budget_report=ContextBudgetReport(
            token_budget=8_000,
            mandatory_tokens=0,
            optional_tokens=0,
            full_chapter_read_count=0,
        ),
    )
    memory_ref = _put(repo, memory.model_dump_json())
    package, _ = PlannerContextAssembler(repo, schema_version=VERSION).assemble(
        request=request,
        inquiry=inquiry,
        inquiry_ref=inquiry_ref,
        stage1_context=memory,
        stage1_context_ref=memory_ref,
    )
    rendered = package.rendered_context
    assert "全书核心冲突" in rendered
    assert "First volume local arc." in rendered
    assert "Local investigation." in rendered
    assert "卷六真相" not in rendered
    assert "第700章 payoff" not in rendered


def _payoff_item(chapter: int) -> ProposedItem:
    return ProposedItem(
        item_id=StableId(f"goal.{chapter}"),
        kind="payoff",
        payload={
            "chapter_index": chapter,
            "summary": "银铭到手",
            "status": "resolved",
            "obligation_ids": [YINMING.root],
            "obligation_kind": "promise",
            "not_before_chapter": 85,
        },
        provenance=ProposalProvenance.PLANNER_PROPOSED,
    )


def _validate_payoff_at_chapter(chapter: int) -> None:
    materializer = PlanCandidateMaterializer(Mock(), Mock(), schema_version=VERSION)
    item = _payoff_item(chapter)
    proposal = PlanProposal.model_construct(
        proposal_id=StableId(f"proposal.yinming.{chapter}"),
        project_id=ProjectId("project.test"),
        mode=AgentMode.CHAPTER_SET,
        base_commit=COMMIT,
        items=(item,),
        coverage=1.0,
        receipt=object(),
    )
    materializer._read = Mock(  # type: ignore[method-assign]
        side_effect=[_world(_yinming()), _text(last_chapter=80)]
    )
    materializer._validate_temporal_obligation_use(
        current=_plan(),
        incoming_nodes=(),
        incoming_goals=(
            ChapterGoal(
                goal_id=item.item_id,
                chapter_index=chapter,
                summary="银铭到手",
                obligation_ids=(YINMING,),
            ),
        ),
        proposal=proposal,
        base=make_manifest(),
        candidate=CandidateBinding(
            candidate_id=StableId(f"candidate.yinming.{chapter}"),
            kind=CandidateKind.PLAN,
            artifact_ref=ArtifactRef(
                artifact_id=HASH,
                media_type="application/json",
                byte_length=1,
                schema_version=VERSION,
            ),
            candidate_hash=HASH.root,
            basis_commit=COMMIT,
            horizon_start=81,
            horizon_end=85,
        ),
    )


@pytest.mark.parametrize("chapter", [81, 84])
def test_not_before_rejects_resolve_before_boundary_inside_horizon(chapter: int) -> None:
    with pytest.raises(
        CandidateMaterializationError,
        match="future-locked obligation cannot be resolved",
    ):
        _validate_payoff_at_chapter(chapter)


def test_not_before_allows_resolve_on_boundary_chapter() -> None:
    _validate_payoff_at_chapter(85)


def test_future_lock_carries_through_hierarchy_without_raw_brief(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    brief_text = "完整作者 brief UNIQUE-LOCK: 第85章以后才能得到银铭, 第100章是第一卷终局."
    brief = _put(repo, brief_text)
    story = PlanNode(
        plan_node_id=StableId("plan.story.core"),
        node_type="story",
        title="Core conflict",
        summary="Silver-inscription payoff is forbidden before chapter 85.",
        plan_level=PlanLevel.STORY,
        obligation_ids=(YINMING,),
    )
    volume = PlanNode(
        plan_node_id=StableId("plan.volume1"),
        node_type="arc_volume",
        title="Volume 1",
        summary="第一卷终局约在第100章。",
        parent_id=story.plan_node_id,
        plan_level=PlanLevel.ARC_VOLUME,
        chapter_start=1,
        chapter_end=100,
        obligation_ids=(YINMING,),
    )
    plan = PlanRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        nodes=(story, volume),
        chapter_goals=(),
    )
    world = _world(_yinming())
    plan_ref = _put(repo, plan.model_dump_json())
    world_ref = _put(repo, world.model_dump_json())
    text_ref = _put(repo, _text(last_chapter=24).model_dump_json())
    accepted = (plan_ref, world_ref, text_ref)
    story_request = _request(AgentMode.STORY, brief, accepted=accepted).model_copy(
        update={"horizon_start": None, "horizon_end": None}
    )
    from novel_agent.domain.memory import ContextBudgetReport, Stage1ContextPackage

    memory = Stage1ContextPackage(
        context_id=StableId("context.carry-forward"),
        base_commit=story_request.task.base_commit,
        snapshot_id=story_request.snapshot_id or StableId("snapshot.stage4"),
        task_contract="stage4",
        budget_report=ContextBudgetReport(
            token_budget=8_000,
            mandatory_tokens=0,
            optional_tokens=0,
            full_chapter_read_count=0,
        ),
    )
    memory_ref = _put(repo, memory.model_dump_json())
    story_inquiry = _inquiry(AgentMode.STORY, brief).model_copy(
        update={"horizon_start": None, "horizon_end": None}
    )
    story_inquiry_ref = _put(repo, story_inquiry.model_dump_json())
    story_package, _ = PlannerContextAssembler(repo, schema_version=VERSION).assemble(
        request=story_request,
        inquiry=story_inquiry,
        inquiry_ref=story_inquiry_ref,
        stage1_context=memory,
        stage1_context_ref=memory_ref,
    )
    assert brief_text in story_package.rendered_context
    assert "银铭" in story_package.rendered_context

    set_request = _request(AgentMode.CHAPTER_SET, brief, accepted=accepted).model_copy(
        update={"horizon_start": 24, "horizon_end": 28}
    )
    set_inquiry = _inquiry(AgentMode.CHAPTER_SET, brief)
    set_inquiry_ref = _put(repo, set_inquiry.model_dump_json())
    set_package, _ = PlannerContextAssembler(repo, schema_version=VERSION).assemble(
        request=set_request,
        inquiry=set_inquiry,
        inquiry_ref=set_inquiry_ref,
        stage1_context=memory,
        stage1_context_ref=memory_ref,
    )
    rendered = set_package.rendered_context
    assert "UNIQUE-LOCK" not in rendered
    assert brief_text not in rendered
    assert "银铭" in rendered
    assert "85" in rendered
    assert "SETUP/PROGRESS" in rendered
    assert "第一卷终局约在第100章" in rendered


@pytest.mark.parametrize(
    "layer",
    [
        "brief_isolation",
        "writer_constraints",
        "writer_outline",
        "plan_materializer",
        "plan_reviewer_host",
        "curator_validator",
    ],
)
def test_yinming_cannot_payoff_at_chapter_24(layer: str, tmp_path: Path) -> None:
    obligation = _yinming()
    world = _world(obligation)
    if layer == "brief_isolation":
        repo = _repo(tmp_path)
        brief = _put(
            repo,
            "完整作者 brief: 第90-100章银铭最终获得, 卷终真相与内府终局.",
        )
        plan_ref = _put(repo, _plan().model_dump_json())
        world_ref = _put(repo, world.model_dump_json())
        text_ref = _put(repo, _text().model_dump_json())
        request = _request(
            AgentMode.CHAPTER_SET,
            brief,
            accepted=(plan_ref, world_ref, text_ref),
        )
        inquiry = _inquiry(AgentMode.CHAPTER_SET, brief)
        inquiry_ref = _put(repo, inquiry.model_dump_json())
        from novel_agent.domain.memory import ContextBudgetReport, Stage1ContextPackage

        memory = Stage1ContextPackage(
            context_id=StableId("context.yinming"),
            base_commit=request.task.base_commit,
            snapshot_id=request.snapshot_id or StableId("snapshot.stage4"),
            task_contract="stage4",
            budget_report=ContextBudgetReport(
                token_budget=8_000,
                mandatory_tokens=0,
                optional_tokens=0,
                full_chapter_read_count=0,
            ),
        )
        memory_ref = _put(repo, memory.model_dump_json())
        package, _ = PlannerContextAssembler(repo, schema_version=VERSION).assemble(
            request=request,
            inquiry=inquiry,
            inquiry_ref=inquiry_ref,
            stage1_context=memory,
            stage1_context_ref=memory_ref,
        )
        rendered = package.rendered_context
        assert "完整作者 brief" not in rendered
        assert "卷终真相" not in rendered
        assert any(item.section is PlannerContextSection.ACCEPTED_PLAN for item in package.items)
        assert "SETUP/PROGRESS" in rendered
        return

    if layer == "writer_constraints":
        constraints, forbids = ProductionWritingRequestFactory._future_lock_constraints(world, 24)
        assert any("不得 RESOLVE/PAYOFF" in item and "85" in item for item in constraints)
        assert any("不得在本章完成银铭最终获得" in item for item in forbids)
        return

    if layer == "writer_outline":
        from novel_agent.domain.runtime import TaskId

        context = ProductionWritingRequestFactory._planning_context(
            TaskRecord(
                task_id=TaskId("task.writer.24"),
                run_id=RunId("run.yinming"),
                project_id=ProjectId("project.test"),
                kind=TaskKind.DRAFT_CANDIDATE,
                task_revision=0,
                status=TaskStatus.READY,
                basis_commit=COMMIT,
                policy_hash="sha256:" + "1" * 64,
                permission_hash="sha256:" + "1" * 64,
                chapter_index=24,
                target_chapters=28,
                horizon_start=24,
                horizon_end=28,
            ),
            _plan(),
            "Investigate the wreck.",
        )
        titles = {node.title for node in context.visible_outline_nodes}
        assert "Volume 8 银铭终局" not in titles
        assert "Chapters 24-28" in titles
        assert "Volume 1" in titles
        return

    if layer == "plan_reviewer_host":
        from novel_agent.agents.plan_reviewer import apply_host_plan_review_constraints
        from novel_agent.domain.planning import PlanReviewDraft, ReviewDecision, ReviewTargetKind

        draft = PlanReviewDraft(
            target_kind=ReviewTargetKind.PLAN_PROPOSAL,
            decision=ReviewDecision.ACCEPT,
        )
        proposal = {
            "items": [
                {
                    "item_id": "goal.24",
                    "kind": "payoff",
                    "payload": {
                        "chapter_index": 24,
                        "summary": "银铭到手",
                        "status": "resolved",
                        "obligation_kind": "promise",
                        "not_before_chapter": 85,
                    },
                }
            ]
        }
        gated = apply_host_plan_review_constraints(
            draft,
            target_kind=ReviewTargetKind.PLAN_PROPOSAL,
            target_payload=json.dumps(proposal),
        )
        assert gated.decision is ReviewDecision.REVISE
        assert any(
            issue.kind.value == "early_resolution_of_future_locked_obligation"
            for issue in gated.issues
        )
        return

    if layer == "plan_materializer":
        materializer = PlanCandidateMaterializer(Mock(), Mock(), schema_version=VERSION)
        item = ProposedItem(
            item_id=StableId("goal.24"),
            kind="payoff",
            payload={
                "chapter_index": 24,
                "summary": "银铭到手",
                "status": "resolved",
                "obligation_ids": [YINMING.root],
                "obligation_kind": "promise",
                "not_before_chapter": 85,
            },
            provenance=ProposalProvenance.PLANNER_PROPOSED,
        )
        proposal = PlanProposal.model_construct(
            proposal_id=StableId("proposal.yinming.payoff"),
            project_id=ProjectId("project.test"),
            mode=AgentMode.CHAPTER_SET,
            base_commit=COMMIT,
            items=(item,),
            coverage=1.0,
            receipt=object(),
        )
        materializer._read = Mock(  # type: ignore[method-assign]
            side_effect=[world, _text(last_chapter=23)]
        )
        with pytest.raises(
            CandidateMaterializationError,
            match="future-locked obligation cannot be resolved",
        ):
            materializer._validate_temporal_obligation_use(
                current=_plan(),
                incoming_nodes=(),
                incoming_goals=(
                    ChapterGoal(
                        goal_id=StableId("goal.24"),
                        chapter_index=24,
                        summary="银铭到手",
                        obligation_ids=(YINMING,),
                    ),
                ),
                proposal=proposal,
                base=make_manifest(),
                candidate=CandidateBinding(
                    candidate_id=StableId("candidate.yinming"),
                    kind=CandidateKind.PLAN,
                    artifact_ref=ArtifactRef(
                        artifact_id=HASH,
                        media_type="application/json",
                        byte_length=1,
                        schema_version=VERSION,
                    ),
                    candidate_hash=HASH.root,
                    basis_commit=COMMIT,
                    horizon_start=24,
                    horizon_end=28,
                ),
            )
        return

    world = _world(_yinming())
    resolved = _yinming(status=ObligationStatus.RESOLVED)
    proposed = world.model_copy(
        update={
            "obligations": (resolved,),
        }
    )
    proposed = proposed.model_copy(update={"root_hash": world_root_content_id(proposed)})
    evidence = _text(last_chapter=24)
    operation = ChangeOperation(
        operation_id=StableId("op.resolve.yinming"),
        operation=ChangeOperationType.REPLACE,
        root_kind=RootKind.WORLD,
        target_id=YINMING,
        payload={
            "record_type": WorldRecordKind.OBLIGATION.value,
            "record": resolved.model_dump(mode="json"),
        },
        evidence_refs=(),
    )
    bundle = CandidateChangeBundle(
        bundle_id=StableId("bundle.yinming"),
        project_id=ProjectId("project.test"),
        run_id=RunId("run.yinming"),
        base_commit=COMMIT,
        observed_changes=ObservedChangeSet(
            change_set_id=StableId("changes.yinming"),
            base_commit=COMMIT,
            source_artifact=ArtifactRef(
                artifact_id=HASH,
                media_type="application/json",
                byte_length=1,
                schema_version=VERSION,
            ),
            operations=(operation,),
        ),
        proposed_roots=RootManifest.model_validate(make_manifest().model_dump(mode="python")),
        produced_artifacts=(),
    )
    report = Stage1Validator().validate(bundle, world, proposed, evidence)
    assert report.status is ValidationStatus.FAILED
    assert any(
        finding.code == "OBLIGATION_RESOLVED_BEFORE_NOT_BEFORE" for finding in report.findings
    )


def test_long_range_promise_without_not_before_is_rejected() -> None:
    with pytest.raises(ValidationError, match="target chapter window must be complete"):
        PlanObligation(
            obligation_id=StableId("obligation.broken-window"),
            kind=ObligationKind.PROMISE,
            description="A long-range promise",
            status=ObligationStatus.OPEN,
            target_chapter_start=90,
        )
    materializer = PlanCandidateMaterializer(Mock(), Mock(), schema_version=VERSION)
    item = ProposedItem(
        item_id=StableId("item.promise"),
        kind="promise",
        payload={"summary": "长期伏笔", "obligation_kind": "promise"},
        provenance=ProposalProvenance.PLANNER_PROPOSED,
    )
    with pytest.raises(
        CandidateMaterializationError,
        match="requires not_before_chapter",
    ):
        materializer._reject_item_without_required_window(item)
    item_ok = ProposedItem(
        item_id=StableId("item.foreshadowing"),
        kind="foreshadowing",
        payload={
            "summary": "长期伏笔",
            "obligation_kind": "foreshadowing",
            "not_before_chapter": 85,
        },
        provenance=ProposalProvenance.PLANNER_PROPOSED,
    )
    materializer._reject_item_without_required_window(item_ok)
    from novel_agent.agents.plan_reviewer import apply_host_plan_review_constraints
    from novel_agent.domain.planning import PlanReviewDraft, ReviewDecision, ReviewTargetKind

    missing = apply_host_plan_review_constraints(
        PlanReviewDraft(
            target_kind=ReviewTargetKind.PLAN_PROPOSAL,
            decision=ReviewDecision.ACCEPT,
        ),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        target_payload=json.dumps(
            {
                "items": [
                    {
                        "item_id": "item.promise",
                        "kind": "promise",
                        "payload": {"summary": "长期伏笔", "obligation_kind": "promise"},
                    }
                ]
            }
        ),
    )
    assert missing.decision is ReviewDecision.HUMAN_REQUIRED
    assert any(
        issue.kind.value == "long_range_payoff_without_time_window" for issue in missing.issues
    )
