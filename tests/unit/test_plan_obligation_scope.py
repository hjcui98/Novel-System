"""Regression tests for the PlanRoot-to-Writer obligation scope bridge."""

from novel_agent.domain.benchmark import ChapterGoal, PlanRootDocument
from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.domain.memory import (
    ObligationKind,
    ObligationStatus,
    PlanObligation,
    WorldRootDocument,
)
from novel_agent.domain.plan_obligation_scope import scoped_plan_obligation_ids
from novel_agent.domain.world import PlanLevel, PlanNode

HASH = ArtifactId("sha256:" + "1" * 64)
VERSION = SchemaVersion("1.0.0")
COMMIT = CommitId("sha256:" + "2" * 64)


def _obligation(value: str) -> PlanObligation:
    return PlanObligation(
        obligation_id=StableId(value),
        kind=ObligationKind.OBJECTIVE,
        description=value,
        status=ObligationStatus.OPEN,
    )


def test_bounded_plan_nodes_prevent_future_volume_obligations_leaking_into_chapter_one() -> None:
    current = _obligation("obligation.volume.1")
    future = _obligation("obligation.volume.8")
    legacy = _obligation("obligation.legacy.unbounded")
    plan = PlanRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        nodes=(
            PlanNode(
                plan_node_id=StableId("plan.volume.1"),
                node_type="volume",
                title="volume 1",
                summary="current volume",
                obligation_ids=(current.obligation_id,),
                plan_level=PlanLevel.ARC_VOLUME,
                chapter_start=1,
                chapter_end=100,
            ),
            PlanNode(
                plan_node_id=StableId("plan.volume.8"),
                node_type="volume",
                title="volume 8",
                summary="future volume",
                obligation_ids=(future.obligation_id,),
                plan_level=PlanLevel.ARC_VOLUME,
                chapter_start=701,
                chapter_end=800,
            ),
        ),
        chapter_goals=(
            ChapterGoal(
                goal_id=StableId("plan.chapter.1"),
                chapter_index=1,
                summary="chapter one",
                obligation_ids=(current.obligation_id,),
            ),
        ),
    )
    world = WorldRootDocument(
        root_hash=HASH,
        schema_version=VERSION,
        source_commit=COMMIT,
        obligations=(current, future, legacy),
    )

    assert scoped_plan_obligation_ids(plan=plan, world=world, chapter_index=1) == (
        current.obligation_id,
        legacy.obligation_id,
    )


def test_legacy_display_entity_prefix_only_resolves_to_an_exact_world_id() -> None:
    from novel_agent.services.task_conditioned_need_generation import (
        TaskPlanConditionedNeedGenerator,
    )

    canonical = StableId("entity.bootstrap.1")
    ids = {canonical}

    assert (
        TaskPlanConditionedNeedGenerator._canonical_history_entity_id(
            StableId("planner-context.unit.anchor.entity.bootstrap.1"), ids
        )
        == canonical
    )
    unknown = StableId("planner-context.unit.anchor.entity.bootstrap.99")
    assert TaskPlanConditionedNeedGenerator._canonical_history_entity_id(unknown, ids) == unknown
