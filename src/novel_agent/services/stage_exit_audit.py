"""Fail-closed evidence checks for the formal G0--G3 stop points.

The stage driver may use the counts in this module for progress reporting, but a
stage is complete only when its evidence predicate is true.  In particular, an
eight-volume PlanRoot is not by itself a G0 acceptance, and committed chapter
text is not by itself a G1--G3 acceptance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from novel_agent.domain.benchmark import (
    PlanRootDocument,
    TextRootDocument,
    chapter_goal_history_retrieval_decision,
)
from novel_agent.domain.creative_runtime import CandidateBinding
from novel_agent.domain.ids import CommitId, StableId
from novel_agent.domain.memory import DerivedBuildStatus, WorldRootDocument
from novel_agent.domain.obligation_contract import compile_obligation_actions
from novel_agent.domain.runtime import (
    RunEvent,
    RunEventType,
    TaskKind,
    TaskRecord,
    TaskStatus,
)
from novel_agent.domain.world import PlanLevel


@dataclass(frozen=True, slots=True)
class StageRuntimeEvidence:
    """Durable runtime facts that cannot be inferred from root counts."""

    plan_reviewed: bool = False
    plan_accepted: bool = False
    plan_committed: bool = False
    plan_projected: bool = False
    history_consumed_pairs: frozenset[tuple[int, int]] = field(default_factory=frozenset)
    recovery_without_memory_rerun: bool = False
    curator_observed_chapters: frozenset[int] = field(default_factory=frozenset)
    obligation_observation_ids: frozenset[str] = field(default_factory=frozenset)
    atomic_chapter_writes: frozenset[int] = field(default_factory=frozenset)
    content_reviewed_chapters: frozenset[int] = field(default_factory=frozenset)
    rolling_window_verified: bool = False
    recovery_verified: bool = False
    budget_continuity_verified: bool = False


def _succeeded(tasks: tuple[TaskRecord, ...], kind: TaskKind) -> tuple[TaskRecord, ...]:
    return tuple(
        task for task in tasks if task.kind is kind and task.status is TaskStatus.SUCCEEDED
    )


def _coverage_complete(plan: PlanRootDocument) -> tuple[bool, tuple[tuple[int, int], ...]]:
    volumes = tuple(
        node
        for node in plan.nodes
        if node.plan_level is PlanLevel.ARC_VOLUME
        and node.chapter_start is not None
        and node.chapter_end is not None
    )
    range_values: list[tuple[int, int]] = []
    for node in volumes:
        start = node.chapter_start
        end = node.chapter_end
        if start is not None and end is not None:
            range_values.append((start, end))
    ranges = tuple(sorted(range_values))
    if len(ranges) != 8 or not ranges:
        return False, ranges
    if ranges[0][0] != 1 or ranges[-1][1] != 800:
        return False, ranges
    return all(
        end + 1 == next_start for (_start, end), (next_start, _end) in pairwise(ranges)
    ), ranges


def _obligation_checks(
    plan: PlanRootDocument, world: WorldRootDocument
) -> tuple[bool, tuple[dict[str, Any], ...]]:
    known = {obligation.obligation_id for obligation in world.obligations}
    bindings: dict[StableId, list[tuple[bool, bool]]] = {item: [] for item in known}
    for node in plan.nodes:
        for obligation_id in node.obligation_ids:
            bindings.setdefault(obligation_id, []).append((bool(node.source_ids), True))
    for goal in plan.chapter_goals:
        for obligation_id in goal.obligation_ids:
            bindings.setdefault(obligation_id, []).append((bool(goal.source_ids), True))

    details: list[dict[str, Any]] = []
    complete = bool(world.obligations)
    for obligation in world.obligations:
        source_bound = bool(obligation.evidence_refs) or any(
            source and bound for source, bound in bindings.get(obligation.obligation_id, ())
        )
        timing_bound = any(
            value is not None
            for value in (
                obligation.not_before_chapter,
                obligation.target_chapter_start,
                obligation.target_chapter_end,
                obligation.due_chapter,
            )
        )
        plan_bound = bool(bindings.get(obligation.obligation_id))
        item_complete = bool(obligation.obligation_id.root) and bool(obligation.owner_ids)
        item_complete = item_complete and timing_bound and source_bound and plan_bound
        details.append(
            {
                "obligation_id": obligation.obligation_id.root,
                "owner_bound": bool(obligation.owner_ids),
                "time_bound": timing_bound,
                "source_bound": source_bound,
                "plan_bound": plan_bound,
                "complete": item_complete,
            }
        )
        complete = complete and item_complete

    referenced = {
        *(obligation_id for node in plan.nodes for obligation_id in node.obligation_ids),
        *(obligation_id for goal in plan.chapter_goals for obligation_id in goal.obligation_ids),
    }
    complete = complete and referenced.issubset(known)
    return complete, tuple(details)


def _chapter_set_checks(
    plan: PlanRootDocument, world: WorldRootDocument
) -> tuple[bool, bool, bool, tuple[int, ...]]:
    goals = tuple(
        goal for goal in plan.chapter_goals if not goal.goal_id.root.startswith("plan.bootstrap")
    )
    expected = set(range(1, 6))
    goal_indexes = tuple(sorted(goal.chapter_index for goal in goals))
    goal_shape = len(goals) == 5 and set(goal_indexes) == expected
    by_id = {node.plan_node_id: node for node in plan.nodes}
    chapter_nodes = {
        node.chapter_start: node
        for node in plan.nodes
        if node.plan_level is PlanLevel.CHAPTER
        and node.chapter_start is not None
        and node.chapter_end == node.chapter_start
        and node.chapter_start in expected
    }
    wrappers = tuple(
        node
        for node in plan.nodes
        if node.plan_level is PlanLevel.CHAPTER_SET
        and node.chapter_start is not None
        and node.chapter_end is not None
        and node.chapter_start <= 1 <= 5 <= node.chapter_end
    )
    wrapper = wrappers[0] if len(wrappers) == 1 else None
    parent_ok = wrapper is not None
    if wrapper is not None:
        parent = by_id.get(wrapper.parent_id) if wrapper.parent_id is not None else None
        parent_ok = parent is not None and parent.plan_level is PlanLevel.ARC_VOLUME
        if (
            parent is not None
            and parent.chapter_start is not None
            and parent.chapter_end is not None
        ):
            parent_ok = parent_ok and parent.chapter_start <= 1 and parent.chapter_end >= 5
    for goal in goals:
        node = by_id.get(goal.goal_id)
        parent_ok = (
            parent_ok
            and node is not None
            and wrapper is not None
            and node.parent_id == wrapper.plan_node_id
        )
        if node is not None:
            parent_ok = parent_ok and node.chapter_start == goal.chapter_index

    contract_ok = goal_shape and parent_ok and set(chapter_nodes) == expected
    known = {item.obligation_id for item in world.obligations}
    for goal in goals:
        try:
            decision = chapter_goal_history_retrieval_decision(goal)
        except ValueError:
            contract_ok = False
            continue
        # The first chapter has one host-issued waiver path.  Later chapters
        # must make an explicit decision; an omitted field is not a waiver.
        if goal.chapter_index > 1 and decision.requirement.value == "UNDECIDED":
            contract_ok = False
        if decision.requirement.value == "NOT_REQUIRED" and not decision.waiver_is_host_issued:
            contract_ok = False
        actions = compile_obligation_actions(goal.payload.get("obligation_actions"))
        contract_ok = contract_ok and actions.complete
        contract_ok = contract_ok and all(
            StableId(action.obligation_id) in known for action in actions.actions
        )
        contract_ok = contract_ok and all(
            StableId(action.obligation_id) in set(goal.obligation_ids) for action in actions.actions
        )
    return goal_shape, parent_ok, contract_ok, goal_indexes


def audit_stage_roots(
    *,
    commit_id: CommitId,
    plan: PlanRootDocument,
    world: WorldRootDocument,
    text: TextRootDocument,
    snapshot: object | None = None,
    tasks: tuple[TaskRecord, ...] = (),
    events: tuple[RunEvent, ...] = (),
    runtime: StageRuntimeEvidence | None = None,
) -> dict[str, object]:
    """Return a serializable, fail-closed audit of canonical stage evidence."""

    runtime = runtime or StageRuntimeEvidence()
    chapter_indexes = tuple(chapter.chapter_index for chapter in text.chapters)
    volume_complete, volume_ranges = _coverage_complete(plan)
    obligation_complete, obligation_details = _obligation_checks(plan, world)
    goal_shape, parent_bindings, chapter_contract, goal_indexes = _chapter_set_checks(plan, world)
    exact_snapshot = (
        snapshot is not None
        and getattr(snapshot, "source_commit", None) == commit_id
        and getattr(snapshot, "build_status", None) is DerivedBuildStatus.EXACT
        and getattr(snapshot, "published_at", None) is not None
    )
    plan_lifecycle = (
        runtime.plan_reviewed
        and runtime.plan_accepted
        and runtime.plan_committed
        and runtime.plan_projected
    )
    g0 = (
        volume_complete
        and obligation_complete
        and goal_shape
        and parent_bindings
        and chapter_contract
        and exact_snapshot
        and plan_lifecycle
    )

    committed_drafts = {
        task.chapter_index
        for task in _succeeded(tasks, TaskKind.DRAFT_COMMIT)
        if task.chapter_index > 0
    }
    projected_drafts = {
        task.chapter_index
        for task in _succeeded(tasks, TaskKind.PROJECTION_FRESHNESS)
        if task.projection_after == "draft" and task.chapter_index > 0
    }
    g1_target = {1, 2}
    g1 = (
        g1_target.issubset(set(chapter_indexes))
        and g1_target.issubset(committed_drafts)
        and g1_target.issubset(projected_drafts)
        and (1, 2) in runtime.history_consumed_pairs
        and runtime.recovery_without_memory_rerun
    )
    g2_target = set(range(1, 6))
    g2 = (
        g2_target.issubset(set(chapter_indexes))
        and g2_target.issubset(committed_drafts)
        and g2_target.issubset(projected_drafts)
        and g2_target.issubset(runtime.curator_observed_chapters)
        and runtime.obligation_observation_ids.issubset(
            {item.obligation_id.root for item in world.obligations}
        )
        and g2_target.issubset(runtime.atomic_chapter_writes)
        and g2_target.issubset(runtime.content_reviewed_chapters)
    )
    g3_target = set(range(1, 21))
    g3 = (
        g3_target.issubset(set(chapter_indexes))
        and g3_target.issubset(committed_drafts)
        and g3_target.issubset(projected_drafts)
        and runtime.rolling_window_verified
        and runtime.recovery_verified
        and runtime.budget_continuity_verified
    )
    event_types = {event.event_type for event in events}
    return {
        "commit": commit_id.root,
        "committed_chapters": len(text.chapters),
        "committed_chapter_indexes": list(chapter_indexes),
        "committed_volumes": len(volume_ranges),
        "volume_ranges": [list(item) for item in volume_ranges],
        "volume_coverage_complete": volume_complete,
        "formal_obligation_count": len(world.obligations),
        "formal_obligation_ids": sorted(item.obligation_id.root for item in world.obligations),
        "formal_obligation_details": list(obligation_details),
        "formal_obligations_complete": obligation_complete,
        "chapter_set_goal_count": len(goal_indexes),
        "chapter_set_chapters": list(goal_indexes),
        "chapter_set_goal_shape_complete": goal_shape,
        "chapter_set_parent_bindings_complete": parent_bindings,
        "chapter_set_contract_complete": chapter_contract,
        "projection_exact": exact_snapshot,
        "projection_source_commit": (
            None
            if snapshot is None
            else getattr(getattr(snapshot, "source_commit", None), "root", None)
        ),
        "projection_published": getattr(snapshot, "published_at", None) is not None,
        "plan_reviewed": runtime.plan_reviewed,
        "plan_accepted": runtime.plan_accepted,
        "plan_committed": runtime.plan_committed,
        "plan_projected": runtime.plan_projected,
        "runtime_event_types": sorted(event_type.value for event_type in event_types),
        "g0_evidence_complete": g0,
        "g1_evidence_complete": g1,
        "g2_evidence_complete": g2,
        "g3_evidence_complete": g3,
        "stage_evidence_complete": {"g0": g0, "g1": g1, "g2": g2, "g3": g3},
        "audit_note": (
            "counts are diagnostic only; each stage flag requires its complete evidence predicate"
        ),
    }


def runtime_evidence_from_tasks(
    tasks: tuple[TaskRecord, ...],
    events: tuple[RunEvent, ...],
    *,
    plan_reviewed: bool,
) -> StageRuntimeEvidence:
    """Project the durable task/event stream into the stage predicates.

    This deliberately leaves semantic proofs false unless a typed artifact
    carries them.  A successful task is enough to establish its own mechanical
    step, but not a history-consumption, Curator, recovery, or budget claim.
    """

    plan_acceptances = _succeeded(tasks, TaskKind.PLAN_ACCEPTANCE)
    plan_commits = {task.task_id for task in _succeeded(tasks, TaskKind.PLAN_COMMIT)}
    plan_committed = any(
        event.event_type is RunEventType.COMMIT_ACCEPTED and event.task_id in plan_commits
        for event in events
    )
    plan_projected = any(
        task.projection_after == "plan" for task in _succeeded(tasks, TaskKind.PROJECTION_FRESHNESS)
    )
    atomic_writes = frozenset(
        task.chapter_index
        for task in _succeeded(tasks, TaskKind.DRAFT_COMMIT)
        if task.chapter_index > 0
    )
    return StageRuntimeEvidence(
        plan_reviewed=plan_reviewed,
        plan_accepted=bool(plan_acceptances),
        plan_committed=plan_committed,
        plan_projected=plan_projected,
        atomic_chapter_writes=atomic_writes,
    )


def plan_review_is_reachable(binding: CandidateBinding, *, review_media_type: str) -> bool:
    """Check the acceptance candidate has a review artifact in its lineage."""

    return any(ref.media_type == review_media_type for ref in binding.lineage_artifact_refs)


__all__ = [
    "StageRuntimeEvidence",
    "audit_stage_roots",
    "plan_review_is_reachable",
    "runtime_evidence_from_tasks",
]
