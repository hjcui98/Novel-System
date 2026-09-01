"""Classify Writer/Planner/Editor/repair round progress from typed terminals.

Retry count, remaining-work text, and model self-praise are not progress.
Consecutive NO_PROGRESS on the same candidate+basis enters the existing
poison/budget gate; this module does not add a second stall platform.
"""

from __future__ import annotations

from novel_agent.domain.agent_context import LoopRoundProgress, LoopRoundProgressKind
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.editorial import EditorialVerdict
from novel_agent.domain.ids import ArtifactId, CommitId, StableId
from novel_agent.domain.memory_write import RepairAction
from novel_agent.domain.planning import PlanningLoopTerminal
from novel_agent.domain.writer_context import ContextAssemblyStatus
from novel_agent.domain.writing_loop import WritingLoopTerminalStatus

NO_PROGRESS_STALL_LIMIT = 2
_WRITER_WAITING = frozenset(
    {
        WritingLoopTerminalStatus.YIELDED,
        WritingLoopTerminalStatus.MEMORY_BUDGET_EXHAUSTED,
        WritingLoopTerminalStatus.INPUT_NOT_READY,
    }
)
_WRITER_NO_PROGRESS = frozenset({WritingLoopTerminalStatus.MEMORY_INSUFFICIENT})
_PLANNER_WAITING = frozenset(
    {
        PlanningLoopTerminal.YIELDED,
        PlanningLoopTerminal.HUMAN_REQUIRED,
        PlanningLoopTerminal.SUSPENDED,
    }
)
_PLANNER_NO_PROGRESS_MARKERS = (
    "INQUIRY_REVISION_NO_PROGRESS",
    "PLAN_REVISION_NO_PROGRESS",
    "PLANNER_MEMORY_NO_PROGRESS",
)
_REPAIR_WAITING = frozenset(
    {
        RepairAction.HUMAN,
        RepairAction.GUARDIAN_REVIEW,
        RepairAction.RETRY_AFTER_SOURCE_CONTEXT_REFRESH,
    }
)
_REPAIR_TERMINAL = frozenset(
    {
        RepairAction.STOP_BUDGET_EXHAUSTED,
        RepairAction.STOP_FATAL,
        RepairAction.QUARANTINE_OPERATION,
        RepairAction.REPLAN,
    }
)


def writer_package_precondition(package: object) -> WritingLoopTerminalStatus | None:
    """Map an expected not-ready Writer package to INPUT_NOT_READY."""

    assembly_status = getattr(package, "assembly_status", ContextAssemblyStatus.READY)
    budget = getattr(package, "budget_report", None)
    final_status = getattr(budget, "final_status", ContextAssemblyStatus.READY)
    if (
        assembly_status != ContextAssemblyStatus.READY
        or final_status is not ContextAssemblyStatus.READY
    ):
        return WritingLoopTerminalStatus.INPUT_NOT_READY
    return None


def writer_round_progress(
    status: WritingLoopTerminalStatus,
    *,
    basis_commit: CommitId | None,
    changed_ids: tuple[StableId, ...] = (),
    remaining_work: tuple[str, ...] = (),
    artifact_ref: ArtifactRef | None = None,
    input_candidate_ref: ArtifactRef | None = None,
) -> LoopRoundProgress:
    if status in _WRITER_WAITING:
        kind = LoopRoundProgressKind.WAITING
    elif status in _WRITER_NO_PROGRESS:
        kind = LoopRoundProgressKind.NO_PROGRESS
        changed_ids = ()
        artifact_ref = None
    else:
        kind = LoopRoundProgressKind.TERMINAL
    return LoopRoundProgress(
        kind=kind,
        basis_commit=basis_commit,
        input_candidate_ref=input_candidate_ref,
        changed_ids=changed_ids,
        remaining_work=remaining_work,
        artifact_ref=artifact_ref,
    )


def writer_checkpoint_progress(
    *,
    basis_commit: CommitId | None,
    changed_ids: tuple[StableId, ...] = (),
    remaining_work: tuple[str, ...] = (),
    artifact_ref: ArtifactRef | None = None,
    input_candidate_ref: ArtifactRef | None = None,
) -> LoopRoundProgress:
    kind = LoopRoundProgressKind.PROGRESSED if changed_ids else LoopRoundProgressKind.WAITING
    return LoopRoundProgress(
        kind=kind,
        basis_commit=basis_commit,
        input_candidate_ref=input_candidate_ref,
        changed_ids=changed_ids if kind is LoopRoundProgressKind.PROGRESSED else (),
        remaining_work=remaining_work,
        artifact_ref=artifact_ref,
    )


def planner_round_progress(
    terminal: PlanningLoopTerminal,
    *,
    basis_commit: CommitId | None,
    diagnostics: tuple[str, ...] = (),
    changed_ids: tuple[StableId, ...] = (),
    remaining_work: tuple[str, ...] = (),
    artifact_ref: ArtifactRef | None = None,
    input_candidate_ref: ArtifactRef | None = None,
) -> LoopRoundProgress:
    if any(marker in diagnostics for marker in _PLANNER_NO_PROGRESS_MARKERS):
        kind = LoopRoundProgressKind.NO_PROGRESS
        changed_ids = ()
        artifact_ref = None
    elif terminal in _PLANNER_WAITING:
        kind = LoopRoundProgressKind.WAITING
    else:
        kind = LoopRoundProgressKind.TERMINAL
    return LoopRoundProgress(
        kind=kind,
        basis_commit=basis_commit,
        input_candidate_ref=input_candidate_ref,
        changed_ids=changed_ids,
        remaining_work=remaining_work,
        artifact_ref=artifact_ref,
    )


def editor_round_progress(
    verdict: EditorialVerdict,
    *,
    basis_commit: CommitId | None,
    previous_issue_ids: tuple[StableId, ...] = (),
    current_issue_ids: tuple[StableId, ...] = (),
    remaining_work: tuple[str, ...] = (),
    artifact_ref: ArtifactRef | None = None,
    input_candidate_ref: ArtifactRef | None = None,
) -> LoopRoundProgress:
    previous = tuple(dict.fromkeys(previous_issue_ids))
    current = tuple(dict.fromkeys(current_issue_ids))
    if verdict is EditorialVerdict.PASS:
        kind = LoopRoundProgressKind.PROGRESSED
        changed_ids = previous if previous else current
        if not changed_ids and artifact_ref is None:
            kind = LoopRoundProgressKind.WAITING
        return LoopRoundProgress(
            kind=kind,
            basis_commit=basis_commit,
            input_candidate_ref=input_candidate_ref,
            changed_ids=changed_ids if kind is LoopRoundProgressKind.PROGRESSED else (),
            remaining_work=remaining_work,
            artifact_ref=artifact_ref,
        )
    if previous and current == previous:
        return LoopRoundProgress(
            kind=LoopRoundProgressKind.NO_PROGRESS,
            basis_commit=basis_commit,
            input_candidate_ref=input_candidate_ref,
            remaining_work=remaining_work,
        )
    return LoopRoundProgress(
        kind=LoopRoundProgressKind.PROGRESSED,
        basis_commit=basis_commit,
        input_candidate_ref=input_candidate_ref,
        changed_ids=current or previous,
        remaining_work=remaining_work,
        artifact_ref=artifact_ref,
    )


def repair_round_progress(
    action: RepairAction,
    *,
    basis_commit: CommitId | None,
    finding_ids: tuple[StableId, ...] = (),
    previous_finding_ids: tuple[StableId, ...] = (),
    remaining_work: tuple[str, ...] = (),
    artifact_ref: ArtifactRef | None = None,
    input_candidate_ref: ArtifactRef | None = None,
) -> LoopRoundProgress:
    if action in _REPAIR_WAITING:
        kind = LoopRoundProgressKind.WAITING
        changed_ids: tuple[StableId, ...] = ()
    elif action in _REPAIR_TERMINAL:
        kind = LoopRoundProgressKind.TERMINAL
        changed_ids = ()
    elif previous_finding_ids and finding_ids == previous_finding_ids:
        kind = LoopRoundProgressKind.NO_PROGRESS
        changed_ids = ()
        artifact_ref = None
    elif finding_ids or artifact_ref is not None:
        kind = LoopRoundProgressKind.PROGRESSED
        changed_ids = finding_ids
    else:
        kind = LoopRoundProgressKind.NO_PROGRESS
        artifact_ref = None
    return LoopRoundProgress(
        kind=kind,
        basis_commit=basis_commit,
        input_candidate_ref=input_candidate_ref,
        changed_ids=changed_ids if kind is LoopRoundProgressKind.PROGRESSED else (),
        remaining_work=remaining_work,
        artifact_ref=artifact_ref,
    )


def planner_failure_is_no_progress(failure_code: str | None) -> bool:
    if failure_code is None:
        return False
    return any(marker in failure_code for marker in _PLANNER_NO_PROGRESS_MARKERS)


def progress_stall_key(
    progress: LoopRoundProgress,
) -> tuple[CommitId | None, ArtifactId | None]:
    candidate = (
        None if progress.input_candidate_ref is None else progress.input_candidate_ref.artifact_id
    )
    return (progress.basis_commit, candidate)


def no_progress_exceeds_limit(
    history: tuple[LoopRoundProgress, ...],
    current: LoopRoundProgress,
    *,
    limit: int = NO_PROGRESS_STALL_LIMIT,
) -> bool:
    """True when the same candidate+basis has `limit` consecutive NO_PROGRESS rounds.

    Remaining-work text and retry labels are ignored so a model claiming
    improvement cannot reset the existing poison/budget gate.
    """

    if current.kind is not LoopRoundProgressKind.NO_PROGRESS or limit < 1:
        return False
    key = progress_stall_key(current)
    count = 1
    for prior in reversed(history):
        if prior.kind is not LoopRoundProgressKind.NO_PROGRESS or progress_stall_key(prior) != key:
            break
        count += 1
    return count >= limit


def should_stop_for_no_progress(
    progress: LoopRoundProgress | None,
    *,
    attempt_no: int,
    limit: int = NO_PROGRESS_STALL_LIMIT,
) -> bool:
    """Retry count is not progress; a later NO_PROGRESS attempt hits the stall gate."""

    return (
        progress is not None
        and progress.kind is LoopRoundProgressKind.NO_PROGRESS
        and attempt_no >= limit
    )
