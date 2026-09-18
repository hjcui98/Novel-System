"""One deterministic decision for the next hierarchical creation step.

Stage 5 used to derive its next task from the *previous task record*: after a
chapter-set projection it always created a Draft, and ``_rolling_plan_task``
inherited the previous task's ``plan_level``.  With chapter execution added,
both shortcuts are wrong: an accepted chapter set must first refine its next
chapter, and a finished CHAPTER task must not leak ``CHAPTER`` into the next
rolling window.

This module derives the decision from the *current accepted PlanRoot* plus the
committed text cursor.  It selects one operation class only; the existing
Runtime still owns permission, acceptance, idempotency, commit, projection and
budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from novel_agent.domain.benchmark import PlanRootDocument
from novel_agent.domain.plan_detail import ParentPlanBinding, plan_node_content_id
from novel_agent.domain.world import PlanLevel, PlanNode


class CreationStep(StrEnum):
    """The operation class the next creation turn must perform."""

    COMPLETE = "complete"
    BLOCKED = "blocked"
    ARC_VOLUME = "arc_volume"
    CHAPTER_SET = "chapter_set"
    CHAPTER = "chapter"
    DRAFT = "draft"


@dataclass(frozen=True, slots=True)
class TrustedPlanReadiness:
    """Readiness derived from the accepted root, never from a model's claim.

    ``chapter_outline_usable`` means one chapter of the covering window carries
    at least outline-level content.  ``chapter_execution_usable`` means the
    target chapter carries accepted execution detail whose parent binding still
    matches the current parent node content.
    """

    volume_usable: bool
    chapter_set_usable: bool
    chapter_outline_usable: bool
    chapter_execution_usable: bool
    hard_conflict: bool = False


def select_next_creation_step(
    *,
    committed_chapter: int,
    target_chapter: int,
    state: TrustedPlanReadiness,
    from_chapter: int | None = None,
) -> CreationStep:
    """Return the next operation class toward one window target.

    ``committed_chapter`` is the committed text cursor.  ``target_chapter`` is
    the exclusive end of the window.  ``from_chapter`` is the first chapter that
    still needs work and defaults to the cursor plus one; a continuation call
    passes the chapter the current turn was already working on, so a CHAPTER
    refinement followed by its Draft never skips a chapter.
    """

    for name, value in (
        ("committed chapter", committed_chapter),
        ("target chapter", target_chapter),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
    if from_chapter is not None and (
        isinstance(from_chapter, bool) or not isinstance(from_chapter, int)
    ):
        raise ValueError("from chapter must be an integer")
    if committed_chapter < 0 or target_chapter < 1:
        raise ValueError("invalid creation range")
    work_chapter = committed_chapter + 1 if from_chapter is None else from_chapter
    if work_chapter < 1:
        raise ValueError("invalid creation range")
    if work_chapter >= target_chapter:
        return CreationStep.COMPLETE
    if state.hard_conflict:
        return CreationStep.BLOCKED
    if not state.volume_usable:
        return CreationStep.ARC_VOLUME
    if not state.chapter_set_usable or not state.chapter_outline_usable:
        return CreationStep.CHAPTER_SET
    if not state.chapter_execution_usable:
        return CreationStep.CHAPTER
    return CreationStep.DRAFT


def declared_parent_binding(node: PlanNode) -> ParentPlanBinding | None:
    """Read one node's host-written parent binding, if it has one."""

    raw = node.payload.get("parent_set_binding")
    if raw is None:
        return None
    return ParentPlanBinding.model_validate(raw)


def parent_binding_is_current(plan: PlanRootDocument, node: PlanNode) -> bool:
    """Return whether a node's stored parent binding still matches its parent.

    This is the applicability check the document calls for: a chapter's
    execution detail stops being usable when the *parent node it was written
    against* changed, but not merely because some unrelated chapter elsewhere in
    the root was refined.  A node with no stored binding (V1 or pre-binding
    data) keeps its historical reading and is treated as current.
    """

    binding = declared_parent_binding(node)
    if binding is None:
        return True
    if node.parent_id is None or node.parent_id != binding.parent_node_id:
        return False
    parent = next(
        (item for item in plan.nodes if item.plan_node_id == binding.parent_node_id), None
    )
    if parent is None:
        return False
    return plan_node_content_id(parent) == binding.parent_content_hash


def chapter_execution_usable(plan: PlanRootDocument, *, chapter_index: int) -> bool:
    """Return whether one chapter carries current, accepted execution detail.

    A V1 chapter node carries only string beats, so it has no accepted scene and
    beat structure for the Writer to follow.  It is a valid *plan*, but it is
    not an accepted single-chapter execution: the chapter must first be refined
    to ``chapter.v2`` execution detail before正文 generation.  The Writer's
    legacy compilation path stays available for historical replay of already
    committed chapters, which no longer pass through this decision.
    """

    node = chapter_node(plan, chapter_index=chapter_index)
    if node is None:
        return False
    payload = node.payload
    if payload.get("contract_version") != "chapter.v2":
        return False
    if payload.get("detail_level") != "execution":
        return False
    if not payload.get("scenes"):
        return False
    return parent_binding_is_current(plan, node)


def chapter_outline_usable(plan: PlanRootDocument, *, chapter_index: int) -> bool:
    """Return whether one chapter carries at least outline-level content."""

    node = chapter_node(plan, chapter_index=chapter_index)
    if node is None:
        return False
    if node.payload.get("contract_version") != "chapter.v2":
        return True
    if node.payload.get("detail_level") not in {"outline", "execution"}:
        return False
    return bool(str(node.summary).strip())


def _levels_present(plan: PlanRootDocument, levels: set[PlanLevel]) -> bool:
    return any(node.plan_level in levels for node in plan.nodes)


def hierarchy_is_enabled(plan: PlanRootDocument) -> bool:
    """Return whether the accepted root uses the hierarchical plan levels.

    A root that carries a STORY, ARC_VOLUME or CHAPTER_SET node has opted into
    that level, so a missing node is a missing plan rather than a legacy root.
    A root with none of them is a V1 hierarchy and must never be pushed back to
    volume or chapter-set planning.
    """

    return _levels_present(plan, {PlanLevel.STORY, PlanLevel.ARC_VOLUME, PlanLevel.CHAPTER_SET})


def derive_plan_readiness(plan: PlanRootDocument, *, target_chapter: int) -> TrustedPlanReadiness:
    """Derive one chapter's readiness from the accepted root alone."""

    volume, _volumes_present = covering_volume(plan, target_chapter=target_chapter)
    accepted_set = covering_chapter_set(plan, target_chapter=target_chapter)
    # Each level is only required when the accepted root already uses it.  A
    # project that selected STORY/ARC_VOLUME but plans its chapters directly
    # must not be forced to insert a chapter-set level it never adopted, while a
    # root that does carry a chapter set for the window must have one covering
    # the target chapter.
    volumes_present = _levels_present(plan, {PlanLevel.STORY, PlanLevel.ARC_VOLUME})
    sets_present = _levels_present(plan, {PlanLevel.CHAPTER_SET})
    return TrustedPlanReadiness(
        volume_usable=volume is not None or not volumes_present,
        chapter_set_usable=accepted_set is not None or not sets_present,
        chapter_outline_usable=chapter_outline_usable(plan, chapter_index=target_chapter),
        chapter_execution_usable=chapter_execution_usable(plan, chapter_index=target_chapter),
    )


def covering_volume(plan: PlanRootDocument, *, target_chapter: int) -> tuple[PlanNode | None, bool]:
    """Return the ARC_VOLUME covering one chapter and whether any volume exists.

    A plan with no ARC_VOLUME at all is a V1/legacy hierarchy: it must not be
    treated as a missing volume, or every legacy run would restart from volume
    planning.  A volume that declares no chapter range is the whole-book volume
    imported plans use; it covers every chapter.
    """

    volumes = tuple(node for node in plan.nodes if node.plan_level is PlanLevel.ARC_VOLUME)
    covering = tuple(
        node
        for node in volumes
        if node.chapter_start is None
        or node.chapter_end is None
        or node.chapter_start <= target_chapter <= node.chapter_end
    )
    if len(covering) > 1:
        raise ValueError("more than one accepted volume covers the target chapter")
    return (covering[0] if covering else None), bool(volumes)


def covering_chapter_set(plan: PlanRootDocument, *, target_chapter: int) -> PlanNode | None:
    """Return the one accepted CHAPTER_SET covering a chapter, if exactly one."""

    sets = tuple(
        node
        for node in plan.nodes
        if node.plan_level is PlanLevel.CHAPTER_SET
        and node.chapter_start is not None
        and node.chapter_end is not None
        and node.chapter_start <= target_chapter <= node.chapter_end
    )
    if len(sets) > 1:
        raise ValueError("more than one accepted chapter set covers the target chapter")
    return sets[0] if sets else None


def chapter_node(plan: PlanRootDocument, *, chapter_index: int) -> PlanNode | None:
    """Return the one accepted CHAPTER node for a chapter, if exactly one."""

    nodes = tuple(
        node
        for node in plan.nodes
        if node.plan_level is PlanLevel.CHAPTER and node.chapter_start == chapter_index
    )
    if len(nodes) > 1:
        raise ValueError("more than one accepted chapter node owns one chapter")
    return nodes[0] if nodes else None


__all__ = [
    "CreationStep",
    "TrustedPlanReadiness",
    "chapter_execution_usable",
    "chapter_node",
    "chapter_outline_usable",
    "covering_chapter_set",
    "covering_volume",
    "declared_parent_binding",
    "derive_plan_readiness",
    "hierarchy_is_enabled",
    "parent_binding_is_current",
    "select_next_creation_step",
]
