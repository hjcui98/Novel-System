"""Regression coverage for the unified hierarchical next-step decision.

These cases reconstruct the reported调度 gap deterministically: after a chapter
set was accepted the runtime used to create a Draft immediately, and a finished
CHAPTER task could leak its level into the next rolling window.  They assert the
decision derived from the current accepted PlanRoot, not from the previous task
record.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from novel_agent.domain.benchmark import PlanRootDocument
from novel_agent.domain.creation_step import (
    CreationStep,
    TrustedPlanReadiness,
    chapter_execution_usable,
    derive_plan_readiness,
    parent_binding_is_current,
    select_next_creation_step,
)
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.plan_detail import plan_node_content_id
from novel_agent.domain.world import PlanLevel, PlanNode

VERSION = SchemaVersion("1.0.0")


def _story() -> PlanNode:
    return PlanNode(
        plan_node_id=StableId("plan.story"),
        node_type="story",
        title="Story",
        summary="Full-book conflict.",
        plan_level=PlanLevel.STORY,
        chapter_start=1,
        chapter_end=800,
    )


def _volume() -> PlanNode:
    return PlanNode(
        plan_node_id=StableId("plan.volume.1"),
        node_type="arc_volume",
        title="Volume 1",
        summary="First volume.",
        parent_id=StableId("plan.story"),
        plan_level=PlanLevel.ARC_VOLUME,
        chapter_start=1,
        chapter_end=100,
    )


def _chapter_set() -> PlanNode:
    return PlanNode(
        plan_node_id=StableId("plan.chapter-set.6-10"),
        node_type="chapter_set",
        title="ChapterSet 6-10",
        summary="The window's own course.",
        parent_id=StableId("plan.volume.1"),
        plan_level=PlanLevel.CHAPTER_SET,
        chapter_start=6,
        chapter_end=10,
        payload={"contract_version": "chapter-set.v2"},
    )


def _chapter(
    chapter_index: int,
    *,
    detail_level: str = "outline",
    parent: PlanNode | None = None,
    binding_hash: ArtifactId | None = None,
) -> PlanNode:
    payload: dict[str, object] = {
        "contract_version": "chapter.v2",
        "detail_level": detail_level,
        "narrative_function": "chapter duty",
    }
    if detail_level == "execution":
        payload["scenes"] = [
            {
                "scene_id": f"scene.{chapter_index}.1",
                "narrative_task": "t",
                "location": "l",
                "pov": "p",
                "entry_condition": "e",
                "exit_condition": "x",
                "budget_characters": 400,
                "beats": [
                    {
                        "beat_id": f"beat.{chapter_index}.1",
                        "parent_beat_refs": ["beat.parent.1"],
                        "action": "a",
                        "resistance": "r",
                        "choice": "c",
                        "outcome": "o",
                        "information_revealed": "i",
                        "prose_focus": "p",
                        "budget_characters": 400,
                        "close_point": "x",
                    }
                ],
            }
        ]
    if parent is not None:
        payload["parent_set_binding"] = {
            "parent_node_id": parent.plan_node_id.root,
            "parent_content_hash": (binding_hash or plan_node_content_id(parent)).root,
            "parent_plan_level": "chapter_set",
        }
    return PlanNode(
        plan_node_id=StableId(f"plan.chapter.{chapter_index}"),
        node_type="goal",
        title=f"Chapter {chapter_index}",
        summary=f"第 {chapter_index} 章的职责。",
        parent_id=StableId("plan.chapter-set.6-10"),
        plan_level=PlanLevel.CHAPTER,
        chapter_start=chapter_index,
        chapter_end=chapter_index,
        payload=payload,
    )


def _root(*nodes: PlanNode) -> PlanRootDocument:
    return PlanRootDocument(
        root_hash=ArtifactId("sha256:" + "0" * 64),
        schema_version=VERSION,
        nodes=nodes,
    )


def test_no_volume_means_plan_the_volume_first() -> None:
    state = TrustedPlanReadiness(
        volume_usable=False,
        chapter_set_usable=False,
        chapter_outline_usable=False,
        chapter_execution_usable=False,
    )

    assert (
        select_next_creation_step(committed_chapter=5, target_chapter=10, state=state)
        is CreationStep.ARC_VOLUME
    )


def test_accepted_chapter_set_without_execution_refines_the_chapter() -> None:
    """R01: an accepted window whose next chapter is outline-only is not writable."""

    state = TrustedPlanReadiness(
        volume_usable=True,
        chapter_set_usable=True,
        chapter_outline_usable=True,
        chapter_execution_usable=False,
    )

    assert (
        select_next_creation_step(committed_chapter=5, target_chapter=10, state=state)
        is CreationStep.CHAPTER
    )


def test_accepted_execution_allows_the_draft() -> None:
    """R02: a chapter with accepted execution is writable, and is not refined twice."""

    state = TrustedPlanReadiness(
        volume_usable=True,
        chapter_set_usable=True,
        chapter_outline_usable=True,
        chapter_execution_usable=True,
    )

    assert (
        select_next_creation_step(committed_chapter=5, target_chapter=10, state=state)
        is CreationStep.DRAFT
    )


def test_missing_outline_replans_the_window_instead_of_writing() -> None:
    state = TrustedPlanReadiness(
        volume_usable=True,
        chapter_set_usable=True,
        chapter_outline_usable=False,
        chapter_execution_usable=False,
    )

    assert (
        select_next_creation_step(committed_chapter=5, target_chapter=10, state=state)
        is CreationStep.CHAPTER_SET
    )


def test_committed_target_is_complete() -> None:
    state = TrustedPlanReadiness(
        volume_usable=True,
        chapter_set_usable=True,
        chapter_outline_usable=True,
        chapter_execution_usable=True,
    )

    assert (
        select_next_creation_step(committed_chapter=10, target_chapter=10, state=state)
        is CreationStep.COMPLETE
    )


def test_hard_conflict_blocks_instead_of_choosing_a_step() -> None:
    state = TrustedPlanReadiness(
        volume_usable=True,
        chapter_set_usable=True,
        chapter_outline_usable=True,
        chapter_execution_usable=True,
        hard_conflict=True,
    )

    assert (
        select_next_creation_step(committed_chapter=5, target_chapter=10, state=state)
        is CreationStep.BLOCKED
    )


def test_from_chapter_never_skips_the_chapter_being_refined() -> None:
    """A CHAPTER task's own basis root already holds the execution it produced."""

    ready = TrustedPlanReadiness(
        volume_usable=True,
        chapter_set_usable=True,
        chapter_outline_usable=True,
        chapter_execution_usable=True,
    )

    # Continuing chapter 6 after its own execution was accepted must draft 6.
    assert (
        select_next_creation_step(
            committed_chapter=5,
            target_chapter=11,
            from_chapter=6,
            state=ready,
        )
        is CreationStep.DRAFT
    )
    # Working on chapter 7 after chapter 6 was committed must refine 7.
    not_ready = TrustedPlanReadiness(
        volume_usable=True,
        chapter_set_usable=True,
        chapter_outline_usable=True,
        chapter_execution_usable=False,
    )
    assert (
        select_next_creation_step(committed_chapter=6, target_chapter=11, state=not_ready)
        is CreationStep.CHAPTER
    )


def test_decision_rejects_untyped_ranges() -> None:
    state = TrustedPlanReadiness(
        volume_usable=True,
        chapter_set_usable=True,
        chapter_outline_usable=True,
        chapter_execution_usable=True,
    )

    with pytest.raises(ValueError, match="committed chapter must be an integer"):
        select_next_creation_step(
            committed_chapter=True,
            target_chapter=10,
            state=state,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="invalid creation range"):
        select_next_creation_step(committed_chapter=-1, target_chapter=10, state=state)
    with pytest.raises(ValueError, match="from chapter must be an integer"):
        select_next_creation_step(
            committed_chapter=5,
            target_chapter=10,
            from_chapter="6",  # type: ignore[arg-type]
            state=state,
        )


def test_readiness_is_derived_from_the_accepted_root() -> None:
    """The decision reads the root, never the previous task's plan_level."""

    parent = _chapter_set()
    plan = _root(
        _story(),
        _volume(),
        parent,
        _chapter(6, parent=parent),
        _chapter(7, parent=parent),
    )

    readiness = derive_plan_readiness(plan, target_chapter=6)
    assert readiness.volume_usable is True
    assert readiness.chapter_set_usable is True
    assert readiness.chapter_outline_usable is True
    assert readiness.chapter_execution_usable is False

    # A chapter that has only outline content cannot be written.
    assert chapter_execution_usable(plan, chapter_index=7) is False


def test_stale_parent_binding_invalidates_the_execution_detail() -> None:
    """H11: a changed parent node re-opens the child's applicability check.

    Applicability is checked against the *current parent node content hash*, not
    against the whole root: refining an unrelated chapter must not invalidate a
    chapter whose parent has not moved.
    """

    parent = _chapter_set()
    current = _chapter(6, detail_level="execution", parent=parent)
    plan = _root(_story(), _volume(), parent, current)
    assert chapter_execution_usable(plan, chapter_index=6) is True

    # The parent was revised after the child was written: the child's detail no
    # longer applies, even though nothing about chapter 6 itself changed.
    changed_parent = parent.model_copy(update={"summary": "The parent was revised."})
    revised = _root(_story(), _volume(), changed_parent, current)
    assert parent_binding_is_current(revised, current) is False
    assert chapter_execution_usable(revised, chapter_index=6) is False

    # Rebinding against the revised parent restores usability, and an unrelated
    # later chapter elsewhere in the same root must not invalidate it again.
    rebound = _chapter(6, detail_level="execution", parent=changed_parent)
    other = _chapter(8, parent=changed_parent)
    with_other = _root(_story(), _volume(), changed_parent, rebound, other)
    assert parent_binding_is_current(with_other, rebound) is True
    assert chapter_execution_usable(with_other, chapter_index=6) is True


def test_legacy_root_without_volumes_is_not_treated_as_missing_volume() -> None:
    """A V1 hierarchy must not be pushed back to volume planning.

    Legacy roots carry no STORY, ARC_VOLUME or CHAPTER_SET node at all.
    Treating the absent volume as a missing volume would restart every
    historical project at volume planning.
    """

    legacy_chapter = _chapter(1).model_copy(
        update={
            "payload": {"chapter_index": 1, "summary": "旧形状的章目标。"},
            "parent_id": None,
        }
    )
    plan = _root(
        legacy_chapter,
        _chapter(2).model_copy(update={"parent_id": None}),
    )
    readiness = derive_plan_readiness(plan, target_chapter=1)

    assert readiness.volume_usable is True
    assert readiness.chapter_set_usable is True
    assert readiness.chapter_outline_usable is True
    # A V1 chapter node has no accepted scene/beat structure, so it is a plan
    # but not an accepted execution: the chapter must be refined first.
    assert readiness.chapter_execution_usable is False
    assert (
        select_next_creation_step(committed_chapter=0, target_chapter=2, state=readiness)
        is CreationStep.CHAPTER
    )

    # A chapter with no content at all still needs a plan, because nothing
    # describes it yet.  A node that declares no payload contract at all keeps
    # its historical "already described" reading instead.
    empty = _chapter(2).model_copy(
        update={
            "payload": {"contract_version": "chapter.v2", "detail_level": "outline"},
            "parent_id": None,
            "summary": " ",
        }
    )
    without_content = _root(
        _chapter(1).model_copy(update={"parent_id": None}),
        empty,
    )
    assert derive_plan_readiness(without_content, target_chapter=2).chapter_outline_usable is False


def test_legacy_chapter_node_is_a_plan_but_not_an_execution() -> None:
    """H10: a V1 chapter must be refined to execution detail before writing."""

    legacy = PlanNode(
        plan_node_id=StableId("plan.chapter.3"),
        node_type="goal",
        title="Chapter 3",
        summary="Legacy chapter goal.",
        plan_level=PlanLevel.CHAPTER,
        chapter_start=3,
        chapter_end=3,
        payload={"chapter_index": 3, "summary": "Legacy chapter goal."},
    )
    plan = _root(legacy)

    assert chapter_execution_usable(plan, chapter_index=3) is False
    readiness = derive_plan_readiness(plan, target_chapter=3)
    assert readiness.chapter_outline_usable is True
    assert readiness.chapter_execution_usable is False
    assert (
        select_next_creation_step(committed_chapter=2, target_chapter=4, state=readiness)
        is CreationStep.CHAPTER
    )


def test_ambiguous_accepted_roots_are_refused() -> None:
    """The root contract already forbids two owners; the reader stays explicit."""

    parent = _chapter_set()
    duplicate = parent.model_copy(update={"plan_node_id": StableId("plan.chapter-set.other")})
    with pytest.raises(ValidationError, match="cover the same chapter"):
        _root(_story(), _volume(), parent, duplicate, _chapter(6, parent=parent))

    overlapping_volumes = (
        _volume(),
        _volume().model_copy(
            update={"plan_node_id": StableId("plan.volume.1b"), "chapter_end": 50}
        ),
    )
    with pytest.raises(ValidationError, match="cover the same chapter"):
        _root(_story(), *overlapping_volumes, parent, _chapter(6, parent=parent))


def test_execution_detail_without_scenes_is_not_usable() -> None:
    parent = _chapter_set()
    node = _chapter(6, detail_level="execution", parent=parent).model_copy(
        update={
            "payload": {
                "contract_version": "chapter.v2",
                "detail_level": "execution",
                "scenes": [],
            }
        }
    )
    plan = _root(_story(), _volume(), parent, node)

    assert chapter_execution_usable(plan, chapter_index=6) is False
