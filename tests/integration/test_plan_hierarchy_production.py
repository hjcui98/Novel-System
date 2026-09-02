from __future__ import annotations

import pytest

from novel_agent.adapters.runtime.materializers import PlanCandidateMaterializer
from novel_agent.adapters.runtime.stage4_planner import ProductionStage4InvocationFactory
from novel_agent.domain.benchmark import ChapterGoal, PlanRootDocument
from novel_agent.domain.creative_runtime import PlanningLoopRequest
from novel_agent.domain.ids import (
    ArtifactId,
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.planning import PlanReview, ReviewDecision, ReviewTargetKind
from novel_agent.domain.stage2 import (
    AgentMode,
    PlanDeviationRecordCandidate,
    PlannerExecutionResult,
    PlanProposal,
    ProposalProvenance,
    ProposedItem,
)
from novel_agent.domain.world import PlanLevel, PlanNode
from novel_agent.ports.creative_runtime import CandidateMaterializationError

HASH = ArtifactId("sha256:" + "1" * 64)
COMMIT = CommitId("sha256:" + "a" * 64)
VERSION = SchemaVersion("1.0.0")


def _request(**updates: object) -> PlanningLoopRequest:
    values: dict[str, object] = {
        "run_id": RunId("run.hierarchy"),
        "task_id": TaskId("task.hierarchy"),
        "project_id": ProjectId("project.test"),
        "basis_commit": COMMIT,
        "basis_snapshot": StableId("snapshot.hierarchy"),
        "chapter_index": 24,
    }
    values.update(updates)
    return PlanningLoopRequest.model_validate(values)


@pytest.mark.parametrize(
    ("plan_level", "mode", "horizon"),
    [
        (PlanLevel.STORY, AgentMode.STORY, None),
        (PlanLevel.ARC_VOLUME, AgentMode.ARC_VOLUME, None),
        (PlanLevel.CHAPTER_SET, AgentMode.CHAPTER_SET, (25, 29)),
    ],
)
def test_stage4_mode_basis_rules(
    plan_level: PlanLevel,
    mode: AgentMode,
    horizon: tuple[int, int] | None,
) -> None:
    request = _request(
        plan_level=plan_level,
        horizon_start=None if horizon is None else horizon[0],
        horizon_end=None if horizon is None else horizon[1],
    )
    assert ProductionStage4InvocationFactory._mode(request) is mode
    if plan_level is PlanLevel.CHAPTER_SET:
        missing = _request(plan_level=plan_level)
        assert missing.horizon_start is None
        assert ProductionStage4InvocationFactory._mode(missing) is AgentMode.CHAPTER_SET
    else:
        assert request.horizon_start is None
        bad = _request(plan_level=plan_level, horizon_start=25, horizon_end=29)
        assert ProductionStage4InvocationFactory._mode(bad) is mode
        assert bad.horizon_start is not None


def test_single_level_plan_commit_respects_parent_scope() -> None:
    story = PlanNode(
        plan_node_id=StableId("plan.story"),
        node_type="story",
        title="Story",
        summary="Global conflict.",
        plan_level=PlanLevel.STORY,
    )
    volume = PlanNode(
        plan_node_id=StableId("plan.volume1"),
        node_type="arc_volume",
        title="Volume 1",
        summary="First volume.",
        parent_id=story.plan_node_id,
        plan_level=PlanLevel.ARC_VOLUME,
        chapter_start=1,
        chapter_end=100,
    )
    PlanCandidateMaterializer._validate_parent_scope((story, volume))
    overflow = PlanNode(
        plan_node_id=StableId("plan.set.overflow"),
        node_type="chapter_set",
        title="Overflow",
        summary="Exceeds volume.",
        parent_id=volume.plan_node_id,
        plan_level=PlanLevel.CHAPTER_SET,
        chapter_start=90,
        chapter_end=120,
    )
    with pytest.raises(CandidateMaterializationError, match="exceeds parent scope"):
        PlanCandidateMaterializer._validate_parent_scope((story, volume, overflow))
    mixed = PlanCandidateMaterializer._node(
        ProposedItem(
            item_id=StableId("plan.volume-from-story"),
            kind="arc_volume",
            payload={"summary": "should not appear in a STORY candidate", "title": "Vol"},
            provenance=ProposalProvenance.PLANNER_PROPOSED,
        ),
        plan_level=PlanLevel.STORY,
    )
    assert mixed.plan_level is PlanLevel.STORY
    assert PlanCandidateMaterializer._trusted_plan_level(AgentMode.STORY) is PlanLevel.STORY
    bootstrap = PlanNode(
        plan_node_id=StableId("plan.bootstrap"),
        node_type="seed",
        title="Genesis seed",
        summary="Opening seed.",
    )
    current = PlanRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        nodes=(bootstrap,),
        chapter_goals=(ChapterGoal(goal_id=StableId("goal.1"), chapter_index=1, summary="Open."),),
    )
    assert bootstrap.plan_level is None
    kept = tuple(
        item
        for item in current.nodes
        if not (
            PlanLevel.STORY is PlanLevel.STORY
            and item.plan_level is None
            and item.parent_id is None
        )
    )
    assert kept == ()


def test_replan_invalidates_future_descendants_and_keeps_committed_prefix() -> None:
    parent = PlanNode(
        plan_node_id=StableId("plan.set.21-25"),
        node_type="chapter_set",
        title="21-25",
        summary="Old window.",
        plan_level=PlanLevel.CHAPTER_SET,
        chapter_start=21,
        chapter_end=25,
    )
    committed = PlanNode(
        plan_node_id=StableId("plan.chapter.21"),
        node_type="chapter",
        title="Chapter 21",
        summary="Already written.",
        parent_id=parent.plan_node_id,
        plan_level=PlanLevel.CHAPTER,
        chapter_start=21,
        chapter_end=21,
    )
    future = PlanNode(
        plan_node_id=StableId("plan.chapter.25"),
        node_type="chapter",
        title="Chapter 25",
        summary="Not yet written.",
        parent_id=parent.plan_node_id,
        plan_level=PlanLevel.CHAPTER,
        chapter_start=25,
        chapter_end=25,
    )
    current = PlanRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        nodes=(parent, committed, future),
        chapter_goals=(
            ChapterGoal(goal_id=StableId("goal.21"), chapter_index=21, summary="done"),
            ChapterGoal(goal_id=StableId("goal.25"), chapter_index=25, summary="future"),
        ),
    )
    execution = PlannerExecutionResult.model_construct(
        mode=AgentMode.REPLAN,
        plan_proposal=PlanProposal.model_construct(
            proposal_id=StableId("proposal.replan"),
            project_id=ProjectId("project.test"),
            mode=AgentMode.CHAPTER_SET,
            items=(),
            coverage=1.0,
            receipt=object(),
        ),
        deviations=(
            PlanDeviationRecordCandidate(
                deviation_id=StableId("deviation.replan"),
                summary="Replace the unused window.",
                affected_plan_item_ids=(parent.plan_node_id,),
            ),
        ),
    )
    review = PlanReview.model_construct(
        review_id=StableId("review.replan"),
        target_kind=ReviewTargetKind.PLAN_PROPOSAL,
        decision=ReviewDecision.ACCEPT,
        preserve_item_ids=(),
    )
    invalidated = PlanCandidateMaterializer._effective_invalidated_ids(
        current,
        execution,
        review,
        current_chapter=24,
    )
    assert parent.plan_node_id in invalidated
    assert future.plan_node_id in invalidated
    assert committed.plan_node_id not in invalidated
