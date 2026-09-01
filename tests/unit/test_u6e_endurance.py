"""Unit contracts for the U6-E endurance evidence."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from novel_agent.domain.ids import CommitId, ProjectId, RunId, StableId, TaskId
from novel_agent.domain.runtime import (
    AttemptOutcome,
    FailureClass,
    TaskAttempt,
    TaskKind,
    TaskRecord,
    TaskStatus,
)
from novel_agent.domain.u6b_production import (
    U6BCompactionEvidence,
    U6BCompactionOutcome,
    U6BPhaseUsage,
)
from novel_agent.domain.u6e_endurance import (
    U6EEnduranceReport,
    U6EHealthProbe,
    U6EHistoryGrowth,
    U6EWorkerPhaseReport,
)


def _growth(chapter: int, offset: int) -> U6EHistoryGrowth:
    return U6EHistoryGrowth(
        chapter_index=chapter,
        event_count=offset + 1,
        task_count=offset + 2,
        attempt_count=offset + 3,
        effect_count=offset + 4,
        model_call_count=offset + 5,
        artifact_count=offset + 6,
    )


def _report(**updates: object) -> U6EEnduranceReport:
    run_id = RunId("run.u6e.unit")
    phase_usage = tuple(
        U6BPhaseUsage(
            chapter_index=chapter,
            phase="plan",
            wall_clock_ms=1,
            model_call_count=1,
            input_tokens=2,
            output_tokens=1,
            attempt_count=1,
        )
        for chapter in (21, 70)
    )
    compaction = U6BCompactionEvidence(
        receipt_id=StableId("u6e.compaction.unit"),
        run_id=run_id,
        task_id=TaskId("run.u6e.unit.draft.21"),
        chapter_index=21,
        outcome=U6BCompactionOutcome.NO_OP,
        input_context_tokens=10,
        output_context_tokens=10,
        reduction_ratio=0.0,
        min_reduction_ratio=0.1,
        covered_event_range=(1, 1),
        protected_items_retained=True,
        pending_effects_retained=True,
        safe_cut=True,
        semantic_retention_passed=True,
    )
    payload: dict[str, object] = {
        "status": "PASS",
        "run_id": run_id,
        "project_id": ProjectId("project.u6e.unit"),
        "basis_commit": CommitId("sha256:" + "a" * 64),
        "final_commit": CommitId("sha256:" + "b" * 64),
        "expected_chapters": tuple(range(21, 71)),
        "completed_chapters": tuple(range(21, 71)),
        "restart_boundary_chapter": 45,
        "worker_phases": (
            U6EWorkerPhaseReport(
                phase_index=1,
                report_path="/tmp/u6e-phase-1.json",
                status="yielded",
                completed_chapters_after=tuple(range(21, 46)),
                restarted_from_process=False,
            ),
            U6EWorkerPhaseReport(
                phase_index=2,
                report_path="/tmp/u6e-phase-2.json",
                status="completed",
                completed_chapters_before=tuple(range(21, 46)),
                completed_chapters_after=tuple(range(21, 71)),
                restarted_from_process=True,
            ),
        ),
        "history_growth": (_growth(20, 0), _growth(45, 100), _growth(70, 200)),
        "health_probes": (
            U6EHealthProbe(
                probe_id="final",
                chapter_index=70,
                status="PASS",
                event_count=201,
                task_count=202,
                model_call_count=205,
                detail="complete",
            ),
        ),
        "phase_usage": phase_usage,
        "compaction": (compaction,),
        "model_call_count": 205,
        "input_tokens": 1000,
        "output_tokens": 100,
        "event_count": 201,
        "task_count": 202,
        "attempt_count": 203,
        "commit_count": 51,
        "artifact_count": 206,
        "future_leakage_count": 0,
        "duplicate_effect_count": 0,
        "duplicate_commit_count": 0,
        "unrecoverable_task_count": 0,
        "external_wait_count": 0,
        "repeated_failure_count": 2,
        "projection_rebuild_verified": True,
        "cold_restart_verified": True,
        "process_memory_dependency": False,
        "repair_count": 1,
    }
    payload.update(updates)
    return U6EEnduranceReport(**payload)


def test_u6e_pass_accepts_fifty_chapters_and_reports_growth() -> None:
    report = _report()
    assert len(report.expected_chapters) == 50
    assert report.history_growth[-1].event_count > report.history_growth[0].event_count
    assert report.cold_restart_verified is True


def test_u6e_pass_requires_contiguous_complete_history_and_clean_boundaries() -> None:
    with pytest.raises(ValidationError, match="fifty contiguous"):
        _report(expected_chapters=tuple(range(21, 70)))
    with pytest.raises(ValidationError, match="complete, clean"):
        _report(future_leakage_count=1)
    with pytest.raises(ValidationError, match="complete, clean"):
        _report(process_memory_dependency=True)


def test_u6e_growth_cannot_go_backward() -> None:
    with pytest.raises(ValidationError, match="growth cannot decrease"):
        _report(history_growth=(_growth(20, 0), _growth(45, 100), _growth(70, 1)))


def test_provider_retry_filter_excludes_manual_or_non_transient_waits() -> None:
    from scripts.run_u6b_production_baseline import (
        _select_provider_transient_waiting_tasks,
    )

    common = {
        "run_id": RunId("run.u6e.retry-filter"),
        "project_id": ProjectId("project.u6e.retry-filter"),
        "kind": TaskKind.DRAFT_COMMIT,
        "task_revision": 1,
        "basis_commit": CommitId("sha256:" + "c" * 64),
        "policy_hash": "sha256:" + "d" * 64,
        "permission_hash": "sha256:" + "e" * 64,
        "chapter_index": 46,
        "target_chapters": 90,
    }
    transient_task = TaskRecord(
        task_id=TaskId("run.u6e.retry-filter.transient"),
        status=TaskStatus.WAITING_RETRY,
        **common,
    )
    manual_wait = TaskRecord(
        task_id=TaskId("run.u6e.retry-filter.manual"),
        status=TaskStatus.WAITING_INPUT,
        **common,
    )
    review_wait = TaskRecord(
        task_id=TaskId("run.u6e.retry-filter.review"),
        status=TaskStatus.WAITING_RETRY,
        **common,
    )
    now = datetime(2026, 8, 25, tzinfo=UTC)
    attempts = (
        TaskAttempt(
            attempt_id=StableId("attempt.transient"),
            task_id=transient_task.task_id,
            attempt_no=1,
            worker_id="test",
            claim_token_digest="sha256:" + "f" * 64,
            fence_generation=1,
            claimed_at=now,
            outcome=AttemptOutcome.SUSPENDED,
            failure_class=FailureClass.PROVIDER_TRANSIENT,
        ),
        TaskAttempt(
            attempt_id=StableId("attempt.manual"),
            task_id=manual_wait.task_id,
            attempt_no=1,
            worker_id="test",
            claim_token_digest="sha256:" + "1" * 64,
            fence_generation=1,
            claimed_at=now,
            outcome=AttemptOutcome.SUSPENDED,
            failure_class=FailureClass.PROVIDER_TRANSIENT,
        ),
        TaskAttempt(
            attempt_id=StableId("attempt.review"),
            task_id=review_wait.task_id,
            attempt_no=1,
            worker_id="test",
            claim_token_digest="sha256:" + "2" * 64,
            fence_generation=1,
            claimed_at=now,
            outcome=AttemptOutcome.SUSPENDED,
            failure_class=FailureClass.LEAF_REVIEW_REQUIRED,
        ),
    )

    assert _select_provider_transient_waiting_tasks(
        tasks=(transient_task, manual_wait, review_wait),
        attempts=attempts,
    ) == (transient_task,)
