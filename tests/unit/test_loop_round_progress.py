"""U3-F: typed round progress and expected-precondition mapping."""

from __future__ import annotations

from types import SimpleNamespace

from novel_agent.domain.agent_context import LoopRoundProgressKind
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.creative_runtime import CreativeRunTerminal, PlanningTerminalStatus
from novel_agent.domain.editorial import EditorialVerdict
from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.domain.memory_write import RepairAction
from novel_agent.domain.planning import PlanningLoopTerminal
from novel_agent.domain.runtime import FailureClass, TaskStatus
from novel_agent.domain.writer_context import ContextAssemblyStatus
from novel_agent.domain.writing_loop import WritingLoopTerminalStatus
from novel_agent.services.creative_runtime import CreativeRuntimeService
from novel_agent.services.loop_round_progress import (
    editor_round_progress,
    no_progress_exceeds_limit,
    planner_failure_is_no_progress,
    planner_round_progress,
    repair_round_progress,
    should_stop_for_no_progress,
    writer_checkpoint_progress,
    writer_package_precondition,
    writer_round_progress,
)

COMMIT = CommitId("sha256:" + "b" * 64)


def test_writer_yield_is_waiting_and_duplicate_memory_is_no_progress() -> None:
    waiting = writer_round_progress(
        WritingLoopTerminalStatus.YIELDED,
        basis_commit=COMMIT,
        remaining_work=("reactive Memory",),
    )
    assert waiting.kind is LoopRoundProgressKind.WAITING
    no_progress = writer_round_progress(
        WritingLoopTerminalStatus.MEMORY_INSUFFICIENT,
        basis_commit=COMMIT,
    )
    assert no_progress.kind is LoopRoundProgressKind.NO_PROGRESS
    ready = writer_round_progress(
        WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY,
        basis_commit=COMMIT,
    )
    assert ready.kind is LoopRoundProgressKind.TERMINAL
    waiting_input = writer_round_progress(
        WritingLoopTerminalStatus.INPUT_NOT_READY,
        basis_commit=COMMIT,
        remaining_work=("candidate not ready",),
    )
    assert waiting_input.kind is LoopRoundProgressKind.WAITING


def test_planner_no_progress_diagnostics_are_not_progress() -> None:
    stalled = planner_round_progress(
        PlanningLoopTerminal.REVIEW_REQUIRED,
        basis_commit=COMMIT,
        diagnostics=("PLANNER_MEMORY_NO_PROGRESS",),
    )
    assert stalled.kind is LoopRoundProgressKind.NO_PROGRESS
    waiting = planner_round_progress(
        PlanningLoopTerminal.HUMAN_REQUIRED,
        basis_commit=COMMIT,
    )
    assert waiting.kind is LoopRoundProgressKind.WAITING
    ready = planner_round_progress(
        PlanningLoopTerminal.PLAN_CANDIDATE_READY,
        basis_commit=COMMIT,
    )
    assert ready.kind is LoopRoundProgressKind.TERMINAL


def test_writer_input_not_ready_maps_to_waiting_input() -> None:
    failure, status, terminal = CreativeRuntimeService._writer_failure(
        WritingLoopTerminalStatus.INPUT_NOT_READY
    )
    assert failure is FailureClass.LEAF_REVIEW_REQUIRED
    assert status is TaskStatus.WAITING_INPUT
    assert terminal is CreativeRunTerminal.WAITING_DRAFT_ACCEPTANCE
    insufficient_failure, insufficient_status, insufficient_terminal = (
        CreativeRuntimeService._writer_failure(WritingLoopTerminalStatus.MEMORY_INSUFFICIENT)
    )
    assert insufficient_failure is FailureClass.LEAF_REVIEW_REQUIRED
    assert insufficient_status is TaskStatus.WAITING_RETRY
    assert insufficient_terminal is CreativeRunTerminal.WAITING_RETRY
    editor_failure, editor_status, editor_terminal = CreativeRuntimeService._writer_failure(
        WritingLoopTerminalStatus.EDITOR_FAILED
    )
    assert editor_failure is FailureClass.PROVIDER_TRANSIENT
    assert editor_status is TaskStatus.WAITING_RETRY
    assert editor_terminal is CreativeRunTerminal.WAITING_RETRY


def test_planner_human_required_and_not_promotable_are_typed() -> None:
    failure, status, terminal = CreativeRuntimeService._planner_failure(
        PlanningTerminalStatus.WAITING_INPUT
    )
    assert failure is FailureClass.LEAF_REVIEW_REQUIRED
    assert status is TaskStatus.WAITING_INPUT
    assert terminal is CreativeRunTerminal.WAITING_PLAN_ACCEPTANCE
    blocked_failure, blocked_status, blocked_terminal = CreativeRuntimeService._planner_failure(
        PlanningTerminalStatus.BLOCKED
    )
    assert blocked_failure is FailureClass.BASIS_CHANGED
    assert blocked_status is TaskStatus.BLOCKED
    assert blocked_terminal is CreativeRunTerminal.BLOCKED


def test_not_ready_package_is_input_not_ready_not_an_exception() -> None:
    ready = SimpleNamespace(
        assembly_status=ContextAssemblyStatus.READY,
        budget_report=SimpleNamespace(final_status=ContextAssemblyStatus.READY),
    )
    assert writer_package_precondition(ready) is None
    not_ready = SimpleNamespace(
        assembly_status=ContextAssemblyStatus.EVIDENCE_INSUFFICIENT,
        budget_report=SimpleNamespace(final_status=ContextAssemblyStatus.READY),
    )
    assert writer_package_precondition(not_ready) is WritingLoopTerminalStatus.INPUT_NOT_READY


def test_editor_and_repair_progress_ignore_self_praise_text() -> None:
    issue = StableId("issue.continuity")
    first = editor_round_progress(
        EditorialVerdict.LOCAL_REPAIR,
        basis_commit=COMMIT,
        current_issue_ids=(issue,),
        remaining_work=(issue.root,),
        artifact_ref=_ref(),
        input_candidate_ref=_ref(),
    )
    assert first.kind is LoopRoundProgressKind.PROGRESSED
    stalled = editor_round_progress(
        EditorialVerdict.LOCAL_REPAIR,
        basis_commit=COMMIT,
        previous_issue_ids=(issue,),
        current_issue_ids=(issue,),
        remaining_work=("model improved the prose",),
        input_candidate_ref=_ref(),
    )
    assert stalled.kind is LoopRoundProgressKind.NO_PROGRESS
    passed = editor_round_progress(
        EditorialVerdict.PASS,
        basis_commit=COMMIT,
        previous_issue_ids=(issue,),
        artifact_ref=_ref(),
        input_candidate_ref=_ref(),
    )
    assert passed.kind is LoopRoundProgressKind.PROGRESSED
    waiting = repair_round_progress(
        RepairAction.HUMAN,
        basis_commit=COMMIT,
        finding_ids=(issue,),
        remaining_work=("await author",),
    )
    assert waiting.kind is LoopRoundProgressKind.WAITING
    repeated = repair_round_progress(
        RepairAction.DETERMINISTIC_REPAIR,
        basis_commit=COMMIT,
        finding_ids=(issue,),
        previous_finding_ids=(issue,),
        remaining_work=("retry 3",),
    )
    assert repeated.kind is LoopRoundProgressKind.NO_PROGRESS
    stopped = repair_round_progress(
        RepairAction.STOP_BUDGET_EXHAUSTED,
        basis_commit=COMMIT,
        remaining_work=("budget",),
    )
    assert stopped.kind is LoopRoundProgressKind.TERMINAL


def test_consecutive_no_progress_hits_existing_stall_gate() -> None:
    first = writer_round_progress(
        WritingLoopTerminalStatus.MEMORY_INSUFFICIENT,
        basis_commit=COMMIT,
        remaining_work=("retry 1",),
        input_candidate_ref=_ref(),
    )
    praised = writer_round_progress(
        WritingLoopTerminalStatus.MEMORY_INSUFFICIENT,
        basis_commit=COMMIT,
        remaining_work=("model claims improvement",),
        input_candidate_ref=_ref(),
    )
    assert no_progress_exceeds_limit((), first) is False
    assert no_progress_exceeds_limit((first,), praised) is True
    assert should_stop_for_no_progress(first, attempt_no=1) is False
    assert should_stop_for_no_progress(praised, attempt_no=2) is True
    assert planner_failure_is_no_progress("PLANNER_MEMORY_NO_PROGRESS") is True
    assert planner_failure_is_no_progress("PLAN_CANDIDATE_READY") is False
    assert planner_failure_is_no_progress(None) is False
    assert no_progress_exceeds_limit((), first, limit=0) is False
    waiting_checkpoint = writer_checkpoint_progress(basis_commit=COMMIT)
    assert waiting_checkpoint.kind is LoopRoundProgressKind.WAITING
    progressed_checkpoint = writer_checkpoint_progress(
        basis_commit=COMMIT,
        changed_ids=(StableId("change.writer"),),
        artifact_ref=_ref(),
    )
    assert progressed_checkpoint.kind is LoopRoundProgressKind.PROGRESSED
    empty_pass = editor_round_progress(EditorialVerdict.PASS, basis_commit=COMMIT)
    assert empty_pass.kind is LoopRoundProgressKind.WAITING
    empty_repair = repair_round_progress(RepairAction.DETERMINISTIC_REPAIR, basis_commit=COMMIT)
    assert empty_repair.kind is LoopRoundProgressKind.NO_PROGRESS
    artifact_repair = repair_round_progress(
        RepairAction.DETERMINISTIC_REPAIR,
        basis_commit=COMMIT,
        artifact_ref=_ref(),
    )
    assert artifact_repair.kind is LoopRoundProgressKind.PROGRESSED
    waiting = writer_round_progress(WritingLoopTerminalStatus.YIELDED, basis_commit=COMMIT)
    assert no_progress_exceeds_limit((waiting,), first) is False


def _ref() -> ArtifactRef:
    return ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "c" * 64),
        media_type="application/json",
        byte_length=1,
        schema_version=SchemaVersion("1.0.0"),
    )
