"""Bounded Task/Plan/cutoff-frontier focus extraction for Stage 2M."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import Field

from novel_agent.domain.base import DomainModel
from novel_agent.domain.benchmark import PlanRootDocument
from novel_agent.domain.ids import StableId
from novel_agent.domain.memory import ObligationStatus, WorldRootDocument
from novel_agent.domain.world import Event
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
)


class TaskFocusType(StrEnum):
    ENTITY = "entity"
    RELATION = "relation"
    EVENT = "event"
    STATE = "state"
    OBLIGATION = "obligation"
    PLAN_INTENT = "plan_intent"


class TaskFocusSource(StrEnum):
    TASK = "task"
    OPEN_OBLIGATION = "open_obligation"
    CUTOFF_FRONTIER = "cutoff_frontier"
    PLAN_INTENT = "plan_intent"
    ALIAS_EXPANSION = "alias_expansion"
    ONE_HOP_RELATION = "one_hop_relation"


class TaskFocus(DomainModel):
    focus_id: StableId
    focus_type: TaskFocusType
    canonical_id: StableId
    source: TaskFocusSource
    reason: str = Field(min_length=1)


class FocusSet(DomainModel):
    task_id: StableId
    focuses: tuple[TaskFocus, ...]
    truncated_focus_ids: tuple[StableId, ...] = ()


class TaskFocusExtractor:
    """Extract a deterministic, non-recursive frontier instead of scanning the World."""

    version = "task_focus.v4"

    def __init__(
        self,
        *,
        max_focuses: int = 48,
        max_relation_expansions: int = 12,
        recent_event_limit: int = 12,
    ) -> None:
        if max_focuses < 1 or max_relation_expansions < 0 or recent_event_limit < 1:
            raise ValueError("focus limits are invalid")
        self._max_focuses = max_focuses
        self._max_relation_expansions = max_relation_expansions
        self._recent_event_limit = recent_event_limit

    def extract(
        self,
        task: BenchmarkTaskContract,
        world: WorldRootDocument,
        plan: PlanRootDocument | None = None,
    ) -> FocusSet:
        labels = {
            entity.entity_id: (entity.internal_label, *entity.aliases) for entity in world.entities
        }
        result: list[TaskFocus] = []
        source_priority = {
            TaskFocusSource.TASK: 0,
            TaskFocusSource.OPEN_OBLIGATION: 1,
            TaskFocusSource.PLAN_INTENT: 2,
            TaskFocusSource.CUTOFF_FRONTIER: 3,
            TaskFocusSource.ALIAS_EXPANSION: 4,
            TaskFocusSource.ONE_HOP_RELATION: 5,
        }

        def add(
            focus_type: TaskFocusType,
            canonical_id: StableId,
            source: TaskFocusSource,
            reason: str,
        ) -> None:
            replacement = TaskFocus(
                focus_id=StableId(
                    f"focus.{source.value}.{focus_type.value}.{canonical_id.root}"[:128]
                ),
                focus_type=focus_type,
                canonical_id=canonical_id,
                source=source,
                reason=reason,
            )
            for index, item in enumerate(result):
                if item.focus_type is not focus_type or item.canonical_id != canonical_id:
                    continue
                if source_priority[source] < source_priority[item.source]:
                    result[index] = replacement
                return
            result.append(replacement)

        # Explicit names in the safe task remain useful for synthetic/unseen tasks.
        folded_task = task.task_text.casefold()
        matched_task_entities: dict[str, list[tuple[int, StableId]]] = {}
        for index, (entity_id, aliases) in enumerate(labels.items()):
            if not any(alias and alias.casefold() in folded_task for alias in aliases):
                continue
            internal_label = aliases[0].strip().casefold()
            matched_task_entities.setdefault(internal_label, []).append((index, entity_id))
        for matches in matched_task_entities.values():
            _, entity_id = min(
                matches,
                key=lambda indexed: (
                    ".bootstrap." not in indexed[1].root,
                    indexed[0],
                ),
            )
            add(
                TaskFocusType.ENTITY,
                entity_id,
                TaskFocusSource.TASK,
                "entity is explicitly named by the public task",
            )

        # Open obligations are a bounded semantic frontier. They are not inferred
        # from target prose and therefore remain legal for both profiles.
        for obligation in world.obligations:
            if obligation.status not in {ObligationStatus.OPEN, ObligationStatus.PROGRESSED}:
                continue
            add(
                TaskFocusType.OBLIGATION,
                obligation.obligation_id,
                TaskFocusSource.OPEN_OBLIGATION,
                "open obligation intersects or follows the target horizon",
            )
            for owner_id in obligation.owner_ids:
                add(
                    TaskFocusType.ENTITY,
                    owner_id,
                    TaskFocusSource.OPEN_OBLIGATION,
                    "entity owns an open obligation",
                )

        # The most recently materialized events form the other deterministic
        # frontier. A fixed cap means adding unrelated historical records cannot
        # cause linear growth.
        recent_events = tuple(
            event
            for _, event in sorted(
                enumerate(world.events),
                key=lambda indexed: (
                    self._event_chapter(indexed[1]),
                    indexed[0],
                ),
                reverse=True,
            )[: self._recent_event_limit]
        )
        for event in recent_events:
            add(
                TaskFocusType.EVENT,
                event.event_id,
                TaskFocusSource.CUTOFF_FRONTIER,
                "event is on the recent cutoff frontier",
            )
            for participant_id in event.participant_ids:
                add(
                    TaskFocusType.ENTITY,
                    participant_id,
                    TaskFocusSource.CUTOFF_FRONTIER,
                    "entity participates in a recent frontier event",
                )

        # If canonical memory has no explicit events/obligations yet, use a small
        # stable prefix as a bootstrap frontier; never enumerate the whole World.
        if not result:
            for state in world.states[:8]:
                add(
                    TaskFocusType.STATE,
                    state.state_id,
                    TaskFocusSource.CUTOFF_FRONTIER,
                    "bounded bootstrap state at the cutoff frontier",
                )
                add(
                    TaskFocusType.ENTITY,
                    state.subject_id,
                    TaskFocusSource.CUTOFF_FRONTIER,
                    "subject of a bounded bootstrap frontier state",
                )

        if (
            task.information_profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
            and plan is not None
        ):
            target_range_node_records = tuple(
                node
                for node in plan.nodes
                if self._plan_node_intersects_target(node.plan_node_id, task)
            )
            target_range_nodes = tuple(node.plan_node_id for node in target_range_node_records)
            other_plan_nodes = tuple(
                node.plan_node_id
                for node in plan.nodes
                if node.plan_node_id not in target_range_nodes
            )
            visible_plan_ids = (
                *target_range_nodes,
                *other_plan_nodes[:8],
                *(
                    goal.goal_id
                    for goal in plan.chapter_goals
                    if task.target_chapter_start <= goal.chapter_index <= task.target_chapter_end
                ),
            )
            for plan_id in visible_plan_ids:
                add(
                    TaskFocusType.PLAN_INTENT,
                    plan_id,
                    TaskFocusSource.PLAN_INTENT,
                    "author-visible coarse plan intersects the target horizon",
                )
            target_goals = tuple(
                goal
                for goal in plan.chapter_goals
                if task.target_chapter_start <= goal.chapter_index <= task.target_chapter_end
            )
            # Entity expansion follows only the target-intersecting coarse intent.
            # Names elsewhere in a long author plan must not consume the need budget.
            folded_plan = " ".join(
                (
                    *(node.title + " " + node.summary for node in target_range_node_records),
                    *(goal.summary for goal in target_goals),
                )
            ).casefold()
            for entity_id, aliases in labels.items():
                if any(alias and alias.casefold() in folded_plan for alias in aliases):
                    add(
                        TaskFocusType.ENTITY,
                        entity_id,
                        TaskFocusSource.PLAN_INTENT,
                        "entity is named by author-visible plan intent",
                    )

        entity_focuses = {
            item.canonical_id for item in result if item.focus_type is TaskFocusType.ENTITY
        }
        expanded = 0
        for relation in world.relations:
            if expanded >= self._max_relation_expansions:
                break
            if (
                relation.subject_id not in entity_focuses
                and relation.object_id not in entity_focuses
            ):
                continue
            add(
                TaskFocusType.RELATION,
                relation.relation_id,
                TaskFocusSource.ONE_HOP_RELATION,
                "one-hop relation of a focused entity",
            )
            add(
                TaskFocusType.ENTITY,
                relation.subject_id,
                TaskFocusSource.ONE_HOP_RELATION,
                "subject of a focused one-hop relation",
            )
            add(
                TaskFocusType.ENTITY,
                relation.object_id,
                TaskFocusSource.ONE_HOP_RELATION,
                "object of a focused one-hop relation",
            )
            expanded += 1

        ordered = tuple(
            item
            for _, item in sorted(
                enumerate(result),
                key=lambda indexed: (
                    source_priority[indexed[1].source],
                    indexed[0],
                ),
            )
        )
        retained = ordered[: self._max_focuses]
        return FocusSet(
            task_id=task.task_id,
            focuses=retained,
            truncated_focus_ids=tuple(item.focus_id for item in ordered[self._max_focuses :]),
        )

    @staticmethod
    def _plan_node_intersects_target(
        plan_node_id: StableId,
        task: BenchmarkTaskContract,
    ) -> bool:
        match = re.search(r"\.range\.(\d+)-(\d+)$", plan_node_id.root)
        if match is None:
            return False
        start, end = (int(value) for value in match.groups())
        return start <= task.target_chapter_end and end >= task.target_chapter_start

    @staticmethod
    def _event_chapter(event: Event) -> int:
        narrative_order = event.narrative_order
        if narrative_order is not None:
            return narrative_order.chapter_index
        chapters: list[int] = []
        for evidence in event.evidence_refs:
            if evidence.chapter_id is None:
                continue
            match = re.search(r"(?:^|[._:-])(\d+)$", evidence.chapter_id.root)
            if match is not None:
                chapters.append(int(match.group(1)))
        return max(chapters, default=-1)
