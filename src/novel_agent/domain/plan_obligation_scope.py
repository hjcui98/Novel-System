"""Plan-aware obligation scope shared by Writer construction and readiness."""

from __future__ import annotations

from novel_agent.domain.benchmark import PlanRootDocument
from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import (
    ObligationStatus,
    WorldRootDocument,
    obligation_in_scope_for_chapter,
)


def scoped_plan_obligation_ids(
    *,
    plan: PlanRootDocument | None,
    world: WorldRootDocument,
    chapter_index: int,
) -> tuple[StableId, ...]:
    """Return obligations the accepted plan assigns to ``chapter_index``.

    A bounded plan node is the durable owner of an obligation's chapter range.
    This prevents an unbounded World record from making every future volume's
    responsibility active in chapter one.  Explicit chapter-goal bindings remain
    authoritative.  Legacy obligations without a bounded plan owner retain the
    World timing fallback instead of disappearing silently.
    """

    explicit = {
        obligation_id
        for goal in (plan.chapter_goals if plan is not None else ())
        if goal.chapter_index == chapter_index
        for obligation_id in goal.obligation_ids
    }
    bounded_nodes_by_obligation: dict[StableId, list[tuple[int, int]]] = {}
    if plan is not None:
        for node in plan.nodes:
            if node.chapter_start is None or node.chapter_end is None:
                continue
            for obligation_id in node.obligation_ids:
                bounded_nodes_by_obligation.setdefault(obligation_id, []).append(
                    (node.chapter_start, node.chapter_end)
                )

    scoped: list[StableId] = []
    for obligation in world.obligations:
        if obligation.status in {ObligationStatus.RESOLVED, ObligationStatus.ABANDONED}:
            continue
        obligation_id = obligation.obligation_id
        ranges = bounded_nodes_by_obligation.get(obligation_id)
        if (
            obligation_id in explicit
            or (ranges is not None and any(start <= chapter_index <= end for start, end in ranges))
            or (ranges is None and obligation_in_scope_for_chapter(obligation, chapter_index))
        ):
            scoped.append(obligation_id)
    return tuple(dict.fromkeys(scoped))
