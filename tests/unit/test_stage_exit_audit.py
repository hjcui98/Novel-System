"""The stage driver consumes complete evidence, not root counters."""

from __future__ import annotations

from datetime import UTC, datetime

from novel_agent.domain.benchmark import ChapterGoal, PlanRootDocument, TextRootDocument
from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    DerivedBuildStatus,
    DerivedSnapshotLite,
    ObligationKind,
    ObligationStatus,
    PlanObligation,
    WorldRootDocument,
)
from novel_agent.domain.world import Entity, PlanLevel, PlanNode
from novel_agent.services.stage_exit_audit import StageRuntimeEvidence, audit_stage_roots

VERSION = SchemaVersion("1.0.0")
HASH = ArtifactId("sha256:" + "a" * 64)
COMMIT = CommitId("sha256:" + "b" * 64)
OWNER = StableId("entity.protagonist")
OBLIGATION = StableId("obligation.story.0.objective")


def _node(
    node_id: str,
    level: PlanLevel,
    start: int,
    end: int,
    *,
    parent: str | None = None,
    source: bool = False,
    obligation: bool = False,
) -> PlanNode:
    return PlanNode(
        plan_node_id=StableId(node_id),
        node_type=level.value,
        title=node_id,
        summary="bounded plan node",
        parent_id=None if parent is None else StableId(parent),
        source_ids=(StableId("source.author"),) if source else (),
        obligation_ids=(OBLIGATION,) if obligation else (),
        plan_level=level,
        chapter_start=start,
        chapter_end=end,
    )


def _roots() -> tuple[PlanRootDocument, WorldRootDocument, TextRootDocument]:
    story = _node("story", PlanLevel.STORY, 1, 800, source=True)
    volumes = tuple(
        _node(
            f"volume.{index}",
            PlanLevel.ARC_VOLUME,
            start,
            start + 99,
            parent="story",
            source=True,
            obligation=index == 1,
        )
        for index, start in enumerate(range(1, 801, 100), start=1)
    )
    wrapper = _node(
        "chapter-set.1-5",
        PlanLevel.CHAPTER_SET,
        1,
        5,
        parent="volume.1",
        source=True,
    )
    chapters = tuple(
        _node(
            f"chapter.{index}",
            PlanLevel.CHAPTER,
            index,
            index,
            parent="chapter-set.1-5",
            source=True,
        )
        for index in range(1, 6)
    )
    goals = tuple(
        ChapterGoal(
            goal_id=StableId(f"chapter.{index}"),
            chapter_index=index,
            summary=f"advance chapter {index}",
            obligation_ids=(OBLIGATION,) if index == 1 else (),
            source_ids=(StableId("source.author"),),
            payload=(
                {}
                if index == 1
                else {
                    "history_retrieval": {
                        "requirement": "REQUIRED",
                        "reason": "prior causal state is needed",
                        "needs": [{"kind": "causal_history", "query": "prior state"}],
                    },
                    "obligation_actions": [],
                }
            ),
        )
        for index in range(1, 6)
    )
    plan = PlanRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        nodes=(story, *volumes, wrapper, *chapters),
        chapter_goals=goals,
    )
    world = WorldRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        source_commit=COMMIT,
        entities=(Entity(entity_id=OWNER, entity_type="person", internal_label="protagonist"),),
        obligations=(
            PlanObligation(
                obligation_id=OBLIGATION,
                kind=ObligationKind.OBJECTIVE,
                description="the protagonist reaches the first threshold",
                status=ObligationStatus.OPEN,
                owner_ids=(OWNER,),
                due_chapter=5,
                evidence_refs=(),
            ),
        ),
    )
    text = TextRootDocument(root_hash=HASH, schema_version=VERSION, chapters=())
    return plan, world, text


def _runtime() -> StageRuntimeEvidence:
    return StageRuntimeEvidence(
        plan_reviewed=True,
        plan_accepted=True,
        plan_committed=True,
        plan_projected=True,
    )


def test_g0_requires_complete_volume_obligation_chapter_and_projection_evidence() -> None:
    plan, world, text = _roots()
    snapshot = DerivedSnapshotLite(
        snapshot_id=StableId("snapshot.current"),
        source_commit=COMMIT,
        anchor_build_id=StableId("anchor.current"),
        anchor_index_version="anchor-v1",
        grounded_index_version="grounded-v1",
        embedding_profile="embedding-v1",
        fusion_profile="fusion-v1",
        build_status=DerivedBuildStatus.EXACT,
        published_at=datetime.now(UTC),
    )

    evidence = audit_stage_roots(
        commit_id=COMMIT,
        plan=plan,
        world=world,
        text=text,
        snapshot=snapshot,
        runtime=_runtime(),
    )

    assert evidence["volume_coverage_complete"] is True
    assert evidence["formal_obligations_complete"] is True
    assert evidence["chapter_set_contract_complete"] is True
    assert evidence["g0_evidence_complete"] is True


def test_eight_volume_count_with_a_gap_does_not_pass_g0() -> None:
    plan, world, text = _roots()
    gap = plan.nodes[2].model_copy(update={"chapter_start": 102, "chapter_end": 200})
    plan = plan.model_copy(update={"nodes": (*plan.nodes[:2], gap, *plan.nodes[3:])})

    evidence = audit_stage_roots(
        commit_id=COMMIT,
        plan=plan,
        world=world,
        text=text,
        runtime=_runtime(),
    )

    assert evidence["committed_volumes"] == 8
    assert evidence["volume_coverage_complete"] is False
    assert evidence["g0_evidence_complete"] is False


def test_g1_needs_history_consumption_and_recovery_proof() -> None:
    plan, world, text = _roots()
    evidence = audit_stage_roots(
        commit_id=COMMIT,
        plan=plan,
        world=world,
        text=text,
        runtime=StageRuntimeEvidence(
            history_consumed_pairs=frozenset({(1, 2)}),
            recovery_without_memory_rerun=True,
        ),
    )

    assert evidence["g1_evidence_complete"] is False
