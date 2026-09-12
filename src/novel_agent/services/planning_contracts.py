"""Shared projections of accepted narrative intent, separate from observed World facts."""

from __future__ import annotations

import hashlib
import json

from novel_agent.domain.benchmark import PlanRootDocument
from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import (
    ObligationKind,
    ObligationStatus,
    PlanObligation,
    WorldRootDocument,
    require_not_before_for_kind,
)
from novel_agent.domain.stage2 import ProposedItem
from novel_agent.domain.world import NarrativeRequirements, PlanLevel, PlanNode


def narrative_requirements(item: ProposedItem) -> NarrativeRequirements:
    return NarrativeRequirements.model_validate_json(
        json.dumps(
            {
                key: item.payload[key]
                for key in NarrativeRequirements.model_fields
                if key in item.payload
            }
        )
    )


def declared_obligations(items: tuple[ProposedItem, ...]) -> tuple[PlanObligation, ...]:
    declared: dict[StableId, PlanObligation] = {}
    for item in items:
        entries = item.payload.get("obligations", [])
        if not isinstance(entries, list):
            raise ValueError("planned obligations must be an array")
        entries = list(entries)
        kind = item.payload.get("obligation_kind", item.kind)
        if isinstance(kind, str) and kind in {value.value for value in ObligationKind}:
            entries.append(
                {
                    key: item.payload[key]
                    for key in PlanObligation.model_fields
                    if key in item.payload and key not in {"status", "evidence_refs"}
                }
                | {
                    "obligation_id": item.payload.get("obligation_id", item.item_id.root),
                    "kind": kind,
                    "description": item.payload.get("description", item.payload.get("summary")),
                }
            )
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("planned obligation must be an object")
            # A proposal declares intent; it cannot claim an event has already happened.
            obligation = PlanObligation.model_validate_json(
                json.dumps({**entry, "status": ObligationStatus.OPEN.value, "evidence_refs": []})
            )
            require_not_before_for_kind(obligation.kind, obligation.not_before_chapter)
            if (
                obligation.obligation_id in declared
                and declared[obligation.obligation_id] != obligation
            ):
                raise ValueError("conflicting declarations for one planned obligation")
            declared[obligation.obligation_id] = obligation
    return tuple(declared.values())


def effective_obligations(
    plan: PlanRootDocument,
    world: WorldRootDocument,
) -> tuple[PlanObligation, ...]:
    observed = {item.obligation_id: item for item in world.obligations}
    for intent in plan.obligations:
        actual = observed.get(intent.obligation_id)
        observed[intent.obligation_id] = (
            intent
            if actual is None
            else intent.model_copy(
                update={
                    "status": actual.status,
                    "evidence_refs": actual.evidence_refs,
                }
            )
        )
    return tuple(observed.values())


def chapter_plan_nodes(plan: PlanRootDocument, chapter: int) -> tuple[PlanNode, ...]:
    """Select the current scope and its ancestors, never future siblings sharing a promise."""
    goals = {goal.goal_id for goal in plan.chapter_goals if goal.chapter_index == chapter}
    by_id = {node.plan_node_id: node for node in plan.nodes}
    selected = {
        node.plan_node_id
        for node in plan.nodes
        if node.plan_node_id in goals
        or (
            node.chapter_start is not None
            and node.chapter_end is not None
            and node.chapter_start <= chapter <= node.chapter_end
        )
        or (node.plan_level is PlanLevel.STORY)
    }
    frontier = list(selected)
    while frontier:
        node = by_id[frontier.pop()]
        if (
            node.parent_id is not None
            and node.parent_id in by_id
            and node.parent_id not in selected
        ):
            selected.add(node.parent_id)
            frontier.append(node.parent_id)
    return tuple(node for node in plan.nodes if node.plan_node_id in selected)


def milestone_obligations(nodes: tuple[PlanNode, ...]) -> tuple[PlanObligation, ...]:
    """Stable objective identities; actual fulfillment remains evidence-bound in World."""
    return tuple(
        PlanObligation(
            obligation_id=StableId(
                "milestone."
                + hashlib.sha256(f"{node.plan_node_id.root}\0{outcome}".encode()).hexdigest()[:40]
            ),
            kind=ObligationKind.OBJECTIVE,
            description=f"{node.title}: {outcome}",
            status=ObligationStatus.OPEN,
            target_chapter_start=node.chapter_start,
            target_chapter_end=node.chapter_end,
            due_chapter=node.chapter_end,
        )
        for node in nodes
        for outcome in node.required_outcomes
    )


def dependency_constraints(
    plan: PlanRootDocument, world: WorldRootDocument, dependency_ids: tuple[StableId, ...]
) -> tuple[str, ...]:
    nodes = {item.plan_node_id: item for item in plan.nodes}
    obligations = {item.obligation_id: item for item in effective_obligations(plan, world)}
    result = []
    for identity in dict.fromkeys(dependency_ids):
        node = nodes.get(identity)
        targets = milestone_obligations((node,)) if node is not None else ()
        observed = tuple(obligations.get(item.obligation_id, item) for item in targets)
        if identity in obligations:
            observed = (obligations[identity],)
        description = (
            node.summary
            if node is not None
            else (obligations[identity].description if identity in obligations else identity.root)
        )
        fulfilled = bool(observed) and all(
            item.status is ObligationStatus.RESOLVED and item.evidence_refs for item in observed
        )
        evidence = (
            ", ".join(ref.model_dump_json() for item in observed for ref in item.evidence_refs)
            if fulfilled
            else "no verified fulfillment; obtain evidence or replan the dependency"
        )
        result.append(
            f"Dependency {identity.root}: {description}; fulfilled={fulfilled}; evidence={evidence}"
        )
    return tuple(result)
