"""Fail-closed evidence checks for the formal G0--G3 stop points.

The stage driver may use the counts in this module for progress reporting, but a
stage is complete only when its evidence predicate is true.  In particular, an
eight-volume PlanRoot is not by itself a G0 acceptance, and committed chapter
text is not by itself a G1--G3 acceptance.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from novel_agent.domain.agent_context import ContextItemKind
from novel_agent.domain.artifacts import ArtifactRef, RootKind
from novel_agent.domain.benchmark import (
    PlanRootDocument,
    TextRootDocument,
    chapter_goal_history_retrieval_decision,
)
from novel_agent.domain.changes import ChangeOperationType
from novel_agent.domain.creative_runtime import CandidateBinding
from novel_agent.domain.generation import WritingTaskContract
from novel_agent.domain.ids import CommitId, StableId
from novel_agent.domain.memory import DerivedBuildStatus, WorldRootDocument
from novel_agent.domain.memory_write import (
    MemoryWriteCheckpoint,
    MemoryWriteWorkflowPhase,
    MemoryWriteWorkflowResult,
    MemoryWriteWorkflowStatus,
)
from novel_agent.domain.model_calls import ModelCallLedgerEntry, ModelCallLedgerStatus
from novel_agent.domain.obligation_contract import compile_obligation_actions
from novel_agent.domain.runtime import (
    RunEvent,
    RunEventType,
    TaskAttemptSettledPayload,
    TaskKind,
    TaskRecord,
    TaskStatus,
)
from novel_agent.domain.world import PlanLevel
from novel_agent.domain.writing_loop import (
    WRITING_LOOP_CHECKPOINT_MEDIA_TYPE,
    WritingLoopCheckpoint,
    WritingLoopPhase,
    WritingLoopResult,
    WritingLoopTerminalStatus,
)
from novel_agent.ports.memory_write import DurableMemoryWriteCommitRequest
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes

ArtifactReader = Callable[[ArtifactRef], bytes]

_WRITING_LOOP_RESULT_MEDIA_TYPE = "application/vnd.novel-agent.writing-loop-result+json"
_PLAN_REVIEW_MEDIA_TYPE = "application/vnd.novel-agent.plan-review+json"
_PLAN_ROOT_MEDIA_TYPE = "application/vnd.novel-agent.plan-root+json"
_WRITING_TASK_MEDIA_TYPE = "application/vnd.novel-agent.writing-task+json"
_CANDIDATE_OBSERVATION_MEDIA_TYPE = "application/vnd.novel-agent.candidate-observation+json"
_MEMORY_WRITE_CHECKPOINT_MEDIA_TYPE = "application/vnd.novel-agent.memory-write-checkpoint+json"
_MEMORY_WRITE_RESULT_MEDIA_TYPES = frozenset(
    {
        "application/vnd.novel-agent.terminal-result+json",
        "application/vnd.novel-agent.memory-write-workflow-result+json",
    }
)
_COMMIT_REQUEST_MEDIA_TYPE = "application/vnd.novel-agent.commit-request+json"


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
    expected_obligation_observation_chapters: frozenset[tuple[int, str]] = field(
        default_factory=frozenset
    )
    obligation_observation_chapters: frozenset[tuple[int, str]] = field(default_factory=frozenset)
    atomic_chapter_writes: frozenset[int] = field(default_factory=frozenset)
    content_reviewed_chapters: frozenset[int] = field(default_factory=frozenset)
    rolling_window_verified: bool = False
    recovery_verified: bool = False
    budget_continuity_verified: bool = False


@dataclass(frozen=True, slots=True)
class _SettlementEvidence:
    """One chapter settlement proven by its durable workflow/checkpoint chain."""

    task: TaskRecord
    result: MemoryWriteWorkflowResult
    commit_request: DurableMemoryWriteCommitRequest


@dataclass(frozen=True, slots=True)
class _ChapterTaskChain:
    """One complete chapter task topology with all dependency identities bound."""

    candidate: TaskRecord
    acceptance: TaskRecord
    commit: TaskRecord
    projection: TaskRecord


def _ref_key(ref: ArtifactRef) -> tuple[str, str, int, str]:
    return (
        ref.artifact_id.root,
        ref.media_type,
        ref.byte_length,
        ref.schema_version.root,
    )


def _dedupe_refs(refs: tuple[ArtifactRef, ...] | list[ArtifactRef]) -> tuple[ArtifactRef, ...]:
    values: list[ArtifactRef] = []
    seen: set[tuple[str, str, int, str]] = set()
    for ref in refs:
        key = _ref_key(ref)
        if key not in seen:
            seen.add(key)
            values.append(ref)
    return tuple(values)


def _read_model(
    ref: ArtifactRef,
    model_type: type[Any],
    artifact_reader: ArtifactReader | None,
) -> Any | None:
    """Read one typed artifact; an unreadable artifact never becomes evidence."""

    if artifact_reader is None:
        return None
    try:
        return model_type.model_validate_json(artifact_reader(ref), strict=False)
    except (OSError, TypeError, UnicodeError, ValueError, RuntimeError):
        return None


def _task_artifact_refs(
    task: TaskRecord,
    events: tuple[RunEvent, ...],
) -> tuple[ArtifactRef, ...]:
    """Replay task terminal refs, including refs replaced by a later attempt."""

    refs: list[ArtifactRef] = list(task.terminal_artifact_refs)
    for event in sorted(events, key=lambda item: item.sequence_no):
        if event.task_id != task.task_id:
            continue
        refs.extend(event.artifact_refs)
        if event.event_type is not RunEventType.RUNTIME_ATTEMPT_SETTLED:
            continue
        try:
            payload = TaskAttemptSettledPayload.model_validate(event.payload, strict=False)
        except (TypeError, ValueError):
            continue
        refs.extend(payload.terminal_artifact_refs)
    return _dedupe_refs(refs)


def _writing_results(
    task: TaskRecord,
    refs: tuple[ArtifactRef, ...],
    artifact_reader: ArtifactReader | None,
) -> tuple[WritingLoopResult, ...]:
    results: list[WritingLoopResult] = []
    seen: set[str] = set()
    for ref in refs:
        if ref.media_type != _WRITING_LOOP_RESULT_MEDIA_TYPE:
            continue
        if ref.artifact_id.root in seen:
            continue
        result = _read_model(ref, WritingLoopResult, artifact_reader)
        if (
            isinstance(result, WritingLoopResult)
            and result.run_id == task.run_id
            and result.task_id == task.task_id
        ):
            seen.add(ref.artifact_id.root)
            results.append(result)
    return tuple(results)


def _writing_checkpoint(
    result: WritingLoopResult,
    task: TaskRecord,
    refs: tuple[ArtifactRef, ...],
    artifact_reader: ArtifactReader | None,
) -> tuple[ArtifactRef, WritingLoopCheckpoint] | None:
    candidates = list(result.artifacts)
    if result.checkpoint_ref is not None:
        candidates.append(result.checkpoint_ref)
    candidates.extend(ref for ref in refs if ref.media_type == WRITING_LOOP_CHECKPOINT_MEDIA_TYPE)
    for ref in _dedupe_refs(candidates):
        if ref.media_type != WRITING_LOOP_CHECKPOINT_MEDIA_TYPE:
            continue
        checkpoint = _read_model(ref, WritingLoopCheckpoint, artifact_reader)
        if not isinstance(checkpoint, WritingLoopCheckpoint):
            continue
        if (
            checkpoint.run_id == task.run_id
            and checkpoint.task_id == task.task_id
            and checkpoint.base_commit == task.basis_commit
            and (task.basis_snapshot is None or checkpoint.snapshot_id == task.basis_snapshot)
        ):
            return ref, checkpoint
    return None


def _ready_result_has_durable_chain(
    result: WritingLoopResult,
    artifact_reader: ArtifactReader | None,
    *,
    task: TaskRecord | None = None,
) -> bool:
    if (
        artifact_reader is None
        or result.status is not WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY
    ):
        return False
    if result.final_text_artifact is None or result.observation_artifact is None:
        return False
    if task is not None and (
        result.run_id != task.run_id
        or result.task_id != task.task_id
        or result.context_view is None
        or result.context_view.base_commit != task.basis_commit
        or result.context_view.snapshot_id != task.basis_snapshot
    ):
        return False
    try:
        text = artifact_reader(result.final_text_artifact)
    except (OSError, TypeError, UnicodeError, ValueError, RuntimeError):
        return False
    if (
        not text.strip()
        or result.observation_artifact.media_type != _CANDIDATE_OBSERVATION_MEDIA_TYPE
    ):
        return False
    from novel_agent.domain.editorial import CuratorObservation

    observation = _read_model(result.observation_artifact, CuratorObservation, artifact_reader)
    return observation == result.observation


def _context_items(result: WritingLoopResult) -> tuple[Any, ...]:
    view = result.context_view
    if view is None:
        return ()
    return (
        *view.protected_items,
        *view.active_memory_items,
        *view.working_items,
        *view.recent_settled_tail,
        *view.compacted_prefix_items,
    )


def _history_pair_consumed(
    result: WritingLoopResult,
    *,
    target_chapter: int,
    text: TextRootDocument | None,
) -> bool:
    if text is None or target_chapter < 2:
        return False
    previous = next(
        (chapter for chapter in text.chapters if chapter.chapter_index == target_chapter - 1),
        None,
    )
    if previous is None:
        return False
    previous_text = "\n\n".join(
        block.text for scene in previous.scenes for block in scene.blocks if block.text.strip()
    )
    if not previous_text.strip():
        return False
    expected_artifact_id = sha256_id(previous_text.encode("utf-8"))
    expected_item_id = StableId(f"recent-prose.full.chapter.{target_chapter - 1}")
    return any(
        item.item_id == expected_item_id
        and item.kind is ContextItemKind.RECENT_PROSE
        and item.mandatory
        and expected_artifact_id in {ref.artifact_id for ref in item.source_artifact_refs}
        and previous_text in item.content
        for item in _context_items(result)
    )


def _writing_task(
    result: WritingLoopResult,
    task: TaskRecord,
    artifact_reader: ArtifactReader | None,
) -> WritingTaskContract | None:
    if result.work_plan is None:
        return None
    ref = result.work_plan.work_plan.writing_task_ref
    if ref.media_type != _WRITING_TASK_MEDIA_TYPE:
        return None
    writing_task = _read_model(ref, WritingTaskContract, artifact_reader)
    if (
        isinstance(writing_task, WritingTaskContract)
        and writing_task.target_chapter == task.chapter_index
    ):
        return writing_task
    return None


def _committed_workflow(
    task: TaskRecord,
    refs: tuple[ArtifactRef, ...],
    artifact_reader: ArtifactReader | None,
) -> tuple[MemoryWriteWorkflowResult, DurableMemoryWriteCommitRequest] | None:
    if artifact_reader is None or task.kind is not TaskKind.DRAFT_COMMIT:
        return None
    for ref in refs:
        if ref.media_type not in _MEMORY_WRITE_RESULT_MEDIA_TYPES:
            continue
        result = _read_model(ref, MemoryWriteWorkflowResult, artifact_reader)
        if not isinstance(result, MemoryWriteWorkflowResult):
            continue
        if (
            result.status is not MemoryWriteWorkflowStatus.COMMITTED
            or result.workflow_phase is not MemoryWriteWorkflowPhase.COMPLETE
            or not result.canonical_commit_accepted
            or result.resulting_commit is None
            or result.accepted_candidate_id is None
            or result.effect_uncertain
            or result.degraded
        ):
            continue
        checkpoint_candidates = list(refs)
        if result.checkpoint_ref is not None:
            checkpoint_candidates.append(result.checkpoint_ref)
        for checkpoint_ref in _dedupe_refs(checkpoint_candidates):
            if checkpoint_ref.media_type != _MEMORY_WRITE_CHECKPOINT_MEDIA_TYPE:
                continue
            checkpoint = _read_model(checkpoint_ref, MemoryWriteCheckpoint, artifact_reader)
            if not isinstance(checkpoint, MemoryWriteCheckpoint):
                continue
            if (
                checkpoint.run_id != task.run_id
                or checkpoint.task_id != task.task_id
                or checkpoint.project_id != task.project_id
                or checkpoint.base_commit != task.basis_commit
                or checkpoint.workflow_phase is not MemoryWriteWorkflowPhase.COMPLETE
                or checkpoint.accepted_commit_id != result.resulting_commit
                or checkpoint.commit_receipt_ref != result.commit_receipt
                or checkpoint.projection_receipt_ref != result.projection_receipt_ref
                or checkpoint.freshness_receipt_ref != result.freshness_receipt_ref
                # The terminal artifact cannot contain a self-reference: the
                # workflow writes it before returning the ref.  The checkpoint
                # must point to the exact artifact currently being parsed.
                or checkpoint.terminal_result_ref != ref
                or checkpoint.commit_request_ref is None
            ):
                continue
            request_ref = checkpoint.commit_request_ref
            if request_ref.media_type != _COMMIT_REQUEST_MEDIA_TYPE:
                continue
            commit_request = _read_model(
                request_ref, DurableMemoryWriteCommitRequest, artifact_reader
            )
            if not isinstance(commit_request, DurableMemoryWriteCommitRequest):
                continue
            bundle = commit_request.bundle
            operations = bundle.observed_changes.operations
            operation_ids = {operation.operation_id for operation in operations}
            if (
                commit_request.project_id != task.project_id
                or commit_request.base_commit != task.basis_commit
                or result.base_commit != task.basis_commit
                or bundle.run_id != task.run_id
                or bundle.base_commit != task.basis_commit
                or bundle.observed_changes.base_commit != task.basis_commit
                or commit_request.candidate.candidate_id != result.accepted_candidate_id
                or commit_request.validation.candidate_id != result.accepted_candidate_id
                or commit_request.request_id != result.request_id
                or set(result.committed_operation_ids) != operation_ids
            ):
                continue
            return result, commit_request
    return None


def _chapter_task_chain_records(tasks: tuple[TaskRecord, ...]) -> tuple[_ChapterTaskChain, ...]:
    """Return every complete candidate -> acceptance -> commit -> projection chain."""

    candidates = _succeeded(tasks, TaskKind.DRAFT_CANDIDATE)
    acceptances = _succeeded(tasks, TaskKind.DRAFT_ACCEPTANCE)
    commits = _succeeded(tasks, TaskKind.DRAFT_COMMIT)
    projections = tuple(
        task
        for task in _succeeded(tasks, TaskKind.PROJECTION_FRESHNESS)
        if task.projection_after == "draft"
    )
    complete: list[_ChapterTaskChain] = []
    for candidate in candidates:
        for acceptance in acceptances:
            if (
                acceptance.chapter_index != candidate.chapter_index
                or acceptance.run_id != candidate.run_id
                or acceptance.project_id != candidate.project_id
                or acceptance.basis_commit != candidate.basis_commit
                or acceptance.basis_snapshot != candidate.basis_snapshot
                or candidate.task_id not in acceptance.dependency_task_ids
            ):
                continue
            for commit in commits:
                if (
                    commit.chapter_index != candidate.chapter_index
                    or commit.run_id != candidate.run_id
                    or commit.project_id != candidate.project_id
                    or commit.basis_commit != acceptance.basis_commit
                    or commit.basis_snapshot != acceptance.basis_snapshot
                    or acceptance.task_id not in commit.dependency_task_ids
                ):
                    continue
                for projection in projections:
                    if (
                        projection.chapter_index == candidate.chapter_index
                        and projection.run_id == candidate.run_id
                        and projection.project_id == candidate.project_id
                        and commit.task_id in projection.dependency_task_ids
                    ):
                        complete.append(
                            _ChapterTaskChain(
                                candidate=candidate,
                                acceptance=acceptance,
                                commit=commit,
                                projection=projection,
                            )
                        )
    return tuple(complete)


def _chapter_task_chains(tasks: tuple[TaskRecord, ...]) -> frozenset[int]:
    """Require the fixed candidate -> acceptance -> commit -> projection topology."""

    return frozenset(chain.candidate.chapter_index for chain in _chapter_task_chain_records(tasks))


def _current_plan_lifecycle_reaches_root(
    tasks: tuple[TaskRecord, ...],
    events: tuple[RunEvent, ...],
    plan: PlanRootDocument,
    commit_id: CommitId,
    artifact_reader: ArtifactReader | None,
) -> bool:
    """Require the current PlanRoot to come from one reviewed plan task chain."""

    # Unit callers can provide an already-derived runtime fact without a task
    # stream.  Production callers always pass the durable stream and therefore
    # take the strict branch below.
    if not tasks:
        return True
    if artifact_reader is None:
        return False
    candidates = _succeeded(tasks, TaskKind.PLAN_CANDIDATE)
    acceptances = _succeeded(tasks, TaskKind.PLAN_ACCEPTANCE)
    commits = _succeeded(tasks, TaskKind.PLAN_COMMIT)
    projections = tuple(
        task
        for task in _succeeded(tasks, TaskKind.PROJECTION_FRESHNESS)
        if task.projection_after == "plan" and task.basis_commit == commit_id
    )
    plan_artifact_id = sha256_id(canonical_json_bytes(plan.model_dump(mode="json")))
    accepted_commit_task_ids = {
        event.task_id
        for event in events
        if event.event_type is RunEventType.COMMIT_ACCEPTED and event.task_id is not None
    }
    for projection in projections:
        for commit in commits:
            if (
                commit.task_id not in projection.dependency_task_ids
                or commit.run_id != projection.run_id
                or commit.project_id != projection.project_id
                or commit.task_id not in accepted_commit_task_ids
                or not any(
                    ref.media_type == _PLAN_ROOT_MEDIA_TYPE and ref.artifact_id == plan_artifact_id
                    for ref in commit.terminal_artifact_refs
                )
            ):
                continue
            for acceptance in acceptances:
                if (
                    acceptance.task_id not in commit.dependency_task_ids
                    or acceptance.run_id != commit.run_id
                    or acceptance.project_id != commit.project_id
                    or acceptance.basis_commit != commit.basis_commit
                    or acceptance.basis_snapshot != commit.basis_snapshot
                    or acceptance.candidate_binding_ref is None
                ):
                    continue
                for candidate in candidates:
                    if (
                        candidate.task_id not in acceptance.dependency_task_ids
                        or candidate.run_id != acceptance.run_id
                        or candidate.project_id != acceptance.project_id
                        or candidate.basis_commit != acceptance.basis_commit
                        or candidate.basis_snapshot != acceptance.basis_snapshot
                    ):
                        continue
                    binding = _read_model(
                        acceptance.candidate_binding_ref,
                        CandidateBinding,
                        artifact_reader,
                    )
                    if (
                        isinstance(binding, CandidateBinding)
                        and binding.kind.value == "plan"
                        and binding.basis_commit == acceptance.basis_commit
                        and binding.basis_snapshot == acceptance.basis_snapshot
                        and any(
                            ref.media_type == _PLAN_REVIEW_MEDIA_TYPE
                            for ref in binding.lineage_artifact_refs
                        )
                    ):
                        return True
    return False


def _recovery_without_memory_rerun(
    task: TaskRecord,
    results: tuple[WritingLoopResult, ...],
    refs: tuple[ArtifactRef, ...],
    artifact_reader: ArtifactReader | None,
) -> bool:
    resumable: list[tuple[WritingLoopResult, ArtifactRef, WritingLoopCheckpoint]] = []
    for result in results:
        if result.status not in {
            WritingLoopTerminalStatus.YIELDED,
            WritingLoopTerminalStatus.MEMORY_BUDGET_EXHAUSTED,
        }:
            continue
        checkpoint = _writing_checkpoint(result, task, refs, artifact_reader)
        if checkpoint is not None:
            resumable.append((result, checkpoint[0], checkpoint[1]))
    if not resumable:
        return False
    for result in results:
        if not _ready_result_has_durable_chain(result, artifact_reader, task=task):
            continue
        result_refs = {_ref_key(ref) for ref in result.artifacts}
        for _prior, checkpoint_ref, resumable_checkpoint in resumable:
            if (
                resumable_checkpoint.phase is not WritingLoopPhase.REACTIVE_MEMORY_PENDING
                and _ref_key(checkpoint_ref) in result_refs
            ):
                return True
    return False


def _budget_continuity_verified(
    ready_results: tuple[tuple[TaskRecord, WritingLoopResult], ...],
    model_calls: tuple[ModelCallLedgerEntry, ...],
) -> bool:
    if not ready_results or not model_calls:
        return False
    by_request_id = {entry.request_id: entry for entry in model_calls}
    relevant_tasks = {task.task_id for task, _result in ready_results}
    if any(
        entry.task_id in relevant_tasks
        and entry.status in {ModelCallLedgerStatus.REQUESTED, ModelCallLedgerStatus.UNCERTAIN}
        for entry in model_calls
    ):
        return False
    for task, result in ready_results:
        if result.run_id != task.run_id or result.task_id != task.task_id:
            return False
        if not result.model_call_records:
            return False
        for record in result.model_call_records:
            entry = by_request_id.get(record.request_id)
            if (
                entry is None
                or entry.run_id != task.run_id
                or entry.task_id != task.task_id
                or entry.status is not ModelCallLedgerStatus.COMPLETED
                or entry.call_record != record
            ):
                return False
    return True


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
        goal_obligation_ids = set(goal.obligation_ids)
        action_obligation_ids = {StableId(action.obligation_id) for action in actions.actions}
        contract_ok = contract_ok and action_obligation_ids.issubset(goal_obligation_ids)
        contract_ok = contract_ok and goal_obligation_ids.issubset(action_obligation_ids)
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
    artifact_reader: ArtifactReader | None = None,
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
    current_plan_lifecycle = _current_plan_lifecycle_reaches_root(
        tasks,
        events,
        plan,
        commit_id,
        artifact_reader,
    )
    plan_lifecycle = (
        runtime.plan_reviewed
        and runtime.plan_accepted
        and runtime.plan_committed
        and runtime.plan_projected
        and current_plan_lifecycle
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

    complete_chapter_chains = _chapter_task_chains(tasks)
    plan_obligation_pairs = {
        (goal.chapter_index, obligation_id.root)
        for goal in plan.chapter_goals
        if 1 <= goal.chapter_index <= 5
        for obligation_id in goal.obligation_ids
    }
    expected_obligation_pairs = plan_obligation_pairs | set(
        runtime.expected_obligation_observation_chapters
    )
    obligation_observation_complete = bool(expected_obligation_pairs) and (
        expected_obligation_pairs.issubset(runtime.obligation_observation_chapters)
    )
    g1_target = {1, 2}
    g1 = (
        g1_target.issubset(set(chapter_indexes))
        and g1_target.issubset(complete_chapter_chains)
        and g1_target.issubset(runtime.atomic_chapter_writes)
        and g1_target.issubset(runtime.content_reviewed_chapters)
        and (1, 2) in runtime.history_consumed_pairs
        and runtime.recovery_without_memory_rerun
    )
    g2_target = set(range(1, 6))
    g2 = (
        g2_target.issubset(set(chapter_indexes))
        and g2_target.issubset(complete_chapter_chains)
        and g2_target.issubset(runtime.curator_observed_chapters)
        and runtime.obligation_observation_ids.issubset(
            {item.obligation_id.root for item in world.obligations}
        )
        and obligation_observation_complete
        and g2_target.issubset(runtime.atomic_chapter_writes)
        and g2_target.issubset(runtime.content_reviewed_chapters)
    )
    g3_target = set(range(1, 21))
    g3 = (
        g3_target.issubset(set(chapter_indexes))
        and g3_target.issubset(complete_chapter_chains)
        and g3_target.issubset(runtime.atomic_chapter_writes)
        and g3_target.issubset(runtime.content_reviewed_chapters)
        and g3_target.issubset(runtime.curator_observed_chapters)
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
        "plan_lifecycle_current_root": current_plan_lifecycle,
        "complete_chapter_chain_indexes": sorted(complete_chapter_chains),
        "runtime_history_consumed_pairs": [
            list(item) for item in sorted(runtime.history_consumed_pairs)
        ],
        "runtime_recovery_without_memory_rerun": runtime.recovery_without_memory_rerun,
        "runtime_curator_observed_chapters": sorted(runtime.curator_observed_chapters),
        "runtime_obligation_observation_ids": sorted(runtime.obligation_observation_ids),
        "runtime_expected_obligation_observation_chapters": [
            [chapter, obligation_id]
            for chapter, obligation_id in sorted(runtime.expected_obligation_observation_chapters)
        ],
        "runtime_obligation_observation_chapters": [
            [chapter, obligation_id]
            for chapter, obligation_id in sorted(runtime.obligation_observation_chapters)
        ],
        "runtime_atomic_chapter_writes": sorted(runtime.atomic_chapter_writes),
        "runtime_content_reviewed_chapters": sorted(runtime.content_reviewed_chapters),
        "runtime_rolling_window_verified": runtime.rolling_window_verified,
        "runtime_recovery_verified": runtime.recovery_verified,
        "runtime_budget_continuity_verified": runtime.budget_continuity_verified,
        "obligation_observation_complete": obligation_observation_complete,
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
    artifact_reader: ArtifactReader | None = None,
    plan: PlanRootDocument | None = None,
    text: TextRootDocument | None = None,
    world: WorldRootDocument | None = None,
    model_calls: tuple[ModelCallLedgerEntry, ...] = (),
) -> StageRuntimeEvidence:
    """Project the durable task/event stream into the stage predicates.

    Semantic facts are derived only from exact typed artifacts and their
    persisted lineage.  A successful task is enough to establish its own
    mechanical step, but not a history-consumption, Curator, recovery, write,
    or budget claim.
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

    chain_records = _chapter_task_chain_records(tasks)
    chain_candidate_task_ids = {chain.candidate.task_id for chain in chain_records}
    chain_commit_task_ids = {chain.commit.task_id for chain in chain_records}

    ready_results: list[tuple[TaskRecord, WritingLoopResult]] = []
    history_pairs: set[tuple[int, int]] = set()
    recovery_without_memory_rerun = False
    curator_observed_chapters: set[int] = set()
    expected_obligation_pairs: set[tuple[int, str]] = set()
    observed_obligation_pairs: set[tuple[int, str]] = set()
    for task in _succeeded(tasks, TaskKind.DRAFT_CANDIDATE):
        if task.task_id not in chain_candidate_task_ids:
            continue
        refs = _task_artifact_refs(task, events)
        results = _writing_results(task, refs, artifact_reader)
        if _recovery_without_memory_rerun(task, results, refs, artifact_reader):
            recovery_without_memory_rerun = True
        for result in results:
            if not _ready_result_has_durable_chain(result, artifact_reader, task=task):
                continue
            ready_results.append((task, result))
            curator_observed_chapters.add(task.chapter_index)
            writing_task = _writing_task(result, task, artifact_reader)
            if writing_task is not None:
                expected_obligation_pairs.update(
                    (task.chapter_index, obligation_id.root)
                    for obligation_id in writing_task.active_plan_obligations
                )
            if task.chapter_index > 1 and _history_pair_consumed(
                result,
                target_chapter=task.chapter_index,
                text=text,
            ):
                history_pairs.add((task.chapter_index - 1, task.chapter_index))
            if result.observation is not None:
                observed_obligation_pairs.update(
                    (task.chapter_index, change.target_id.root)
                    for change in result.observation.changes
                    if change.target_id is not None
                )

    if plan is not None:
        expected_obligation_pairs.update(
            (goal.chapter_index, obligation_id.root)
            for goal in plan.chapter_goals
            if 1 <= goal.chapter_index <= 5
            for obligation_id in goal.obligation_ids
        )

    settlement_evidence: list[_SettlementEvidence] = []
    for task in _succeeded(tasks, TaskKind.DRAFT_COMMIT):
        if task.task_id not in chain_commit_task_ids:
            continue
        settled = _committed_workflow(
            task,
            _task_artifact_refs(task, events),
            artifact_reader,
        )
        if settled is not None:
            settled_result, commit_request = settled
            settlement_evidence.append(_SettlementEvidence(task, settled_result, commit_request))

    atomic_writes: set[int] = set()
    known_obligations = (
        {item.obligation_id for item in world.obligations} if world is not None else set()
    )
    for settlement in settlement_evidence:
        projection_bound = any(
            chain.commit.task_id == settlement.task.task_id
            and chain.projection.basis_commit == settlement.result.resulting_commit
            for chain in chain_records
        )
        if projection_bound:
            atomic_writes.add(settlement.task.chapter_index)
        if world is None:
            continue
        operations = settlement.commit_request.bundle.observed_changes.operations
        observed_obligation_pairs.update(
            (settlement.task.chapter_index, operation.target_id.root)
            for operation in operations
            if (
                operation.root_kind is RootKind.WORLD
                and operation.operation
                in {
                    ChangeOperationType.CREATE,
                    ChangeOperationType.REPLACE,
                    ChangeOperationType.RETIRE,
                }
                and operation.target_id in known_obligations
            )
        )

    obligation_observation_ids = frozenset(
        obligation_id for _chapter, obligation_id in observed_obligation_pairs
    )
    budget_continuity_verified = _budget_continuity_verified(
        tuple(ready_results),
        model_calls,
    )
    return StageRuntimeEvidence(
        plan_reviewed=plan_reviewed,
        plan_accepted=bool(plan_acceptances),
        plan_committed=plan_committed,
        plan_projected=plan_projected,
        history_consumed_pairs=frozenset(history_pairs),
        recovery_without_memory_rerun=recovery_without_memory_rerun,
        curator_observed_chapters=frozenset(curator_observed_chapters),
        obligation_observation_ids=obligation_observation_ids,
        expected_obligation_observation_chapters=frozenset(expected_obligation_pairs),
        obligation_observation_chapters=frozenset(observed_obligation_pairs),
        atomic_chapter_writes=frozenset(atomic_writes),
        content_reviewed_chapters=frozenset(task.chapter_index for task, _ in ready_results),
        rolling_window_verified=all((index, index + 1) in history_pairs for index in range(1, 20)),
        recovery_verified=recovery_without_memory_rerun,
        budget_continuity_verified=budget_continuity_verified,
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
