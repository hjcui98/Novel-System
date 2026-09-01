#!/usr/bin/env python3
"""Run the isolated U6-B 20-chapter production baseline with one cold restart."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, select

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.adapters.postgres.model_call_ledger import SqlModelCallLedger
from novel_agent.adapters.postgres.models import (
    CommitRow,
    ModelCallLedgerRow,
    RunEventRow,
    RuntimeEffectProjectionRow,
    RuntimeTaskAttemptRow,
    RuntimeTaskProjectionRow,
)
from novel_agent.domain.creative_runtime import CreativeRunRequest
from novel_agent.domain.ids import StableId, TaskId
from novel_agent.domain.runtime import (
    FailureClass,
    TaskAttempt,
    TaskKind,
    TaskRecord,
    TaskStatus,
)
from novel_agent.domain.stage5_evaluation import Stage5VerticalRunReport, VerticalRunStatus
from novel_agent.domain.u6b_production import (
    U6BCompactionEvidence,
    U6BCompactionOutcome,
    U6BPhaseUsage,
    U6BProductionBaselineReport,
    U6BWorkerPhaseReport,
)
from novel_agent.domain.writing_loop import WritingLoopResult, WritingLoopTerminalStatus
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.event_log import RunEventLogRepository
from novel_agent.services.runtime_commands import RuntimeCommandService
from novel_agent.services.runtime_projection import assert_task_projection_matches

ROOT = Path(__file__).resolve().parents[1]
WRITING_LOOP_RESULT_MEDIA_TYPE = "application/vnd.novel-agent.writing-loop-result+json"
U6BPhase = Literal["plan", "memory", "writer", "editor", "settlement", "recovery"]
PHASES: tuple[U6BPhase, ...] = (
    "plan",
    "memory",
    "writer",
    "editor",
    "settlement",
    "recovery",
)


def _write_once(path: Path, payload: object) -> None:
    if path.exists():
        raise RuntimeError(f"U6-B refuses to overwrite output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_report(path: Path) -> Stage5VerticalRunReport:
    return Stage5VerticalRunReport.model_validate_json(path.read_bytes(), strict=True)


def _run_worker_phase(
    *,
    phase_index: int,
    request: Path,
    manifest: Path,
    database_url: str,
    object_store_root: Path,
    endpoint_profile: str,
    output_root: Path,
    max_tasks: int,
    max_slices: int,
    stop_after_chapter: int | None,
    before: tuple[int, ...],
    max_provider_retries: int = 0,
    settlement_timeout_seconds: float | None = None,
    settlement_output_tokens: int | None = None,
    max_major_rewrites: int | None = None,
    max_local_repairs: int | None = None,
) -> tuple[Stage5VerticalRunReport, U6BWorkerPhaseReport]:
    if max_provider_retries < 0:
        raise ValueError("max_provider_retries must be non-negative")
    recovery_index = 0
    while True:
        suffix = (
            f"worker-phase-{phase_index}"
            if recovery_index == 0
            else f"worker-phase-{phase_index}-provider-retry-{recovery_index}"
        )
        report_path = output_root / f"{suffix}.vertical-run-report.json"
        stdout_path = output_root / f"{suffix}.stdout.log"
        command = [
            sys.executable,
            str(ROOT / "scripts" / "run_stage5_runtime_evaluation.py"),
            "--request",
            str(request),
            "--manifest",
            str(manifest),
            "--database-url",
            database_url,
            "--object-store-root",
            str(object_store_root),
            "--endpoint-profile",
            endpoint_profile,
            "--assembly-factory",
            "novel_agent.runtime.creative_assembly:build_production_assembly",
            "--max-tasks",
            str(max_tasks),
            "--max-slices",
            str(max_slices),
            "--output",
            str(report_path),
        ]
        if stop_after_chapter is not None:
            command.extend(("--stop-after-chapter", str(stop_after_chapter)))
        if settlement_timeout_seconds is not None:
            command.extend(("--settlement-timeout-seconds", str(settlement_timeout_seconds)))
        if settlement_output_tokens is not None:
            command.extend(("--settlement-output-tokens", str(settlement_output_tokens)))
        if max_major_rewrites is not None:
            command.extend(("--max-major-rewrites", str(max_major_rewrites)))
        if max_local_repairs is not None:
            command.extend(("--max-local-repairs", str(max_local_repairs)))
        child_env = os.environ.copy()
        paths = (ROOT / "src", ROOT / "scripts", ROOT)
        existing_pythonpath = child_env.get("PYTHONPATH", "")
        child_env["PYTHONPATH"] = os.pathsep.join(str(path) for path in paths) + (
            os.pathsep + existing_pythonpath if existing_pythonpath else ""
        )
        with stdout_path.open("x", encoding="utf-8") as stream:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=child_env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if not report_path.exists():
            raise RuntimeError(
                f"U6-B worker phase {phase_index} produced no durable report; "
                f"exit={completed.returncode}, log={stdout_path}"
            )
        report = _load_report(report_path)
        if completed.returncode != 0:
            raise RuntimeError(
                f"U6-B worker phase {phase_index} failed with exit={completed.returncode}; "
                f"status={report.status.value}; log={stdout_path}"
            )
        retryable = _provider_transient_waiting_tasks(
            database_url=database_url,
            tasks=report.tasks,
        )
        if (
            report.status is VerticalRunStatus.WAITING
            and retryable
            and recovery_index < max_provider_retries
        ):
            if len(retryable) != 1:
                raise RuntimeError(
                    "U6-B provider retry requires exactly one foreground WAITING_RETRY task"
                )
            _retry_provider_transient_task(
                database_url=database_url,
                task=retryable[0],
                recovery_index=recovery_index + 1,
            )
            recovery_index += 1
            continue
        if stop_after_chapter is not None and report.status is not VerticalRunStatus.YIELDED:
            raise RuntimeError(
                "U6-B restart phase did not yield at the registered chapter boundary"
            )
        break
    after = tuple(report.completed_chapters)
    phase = U6BWorkerPhaseReport(
        phase_index=phase_index,
        report_path=str(report_path.resolve()),
        status=report.status.value,
        completed_chapters_before=before,
        completed_chapters_after=after,
        restarted_from_process=phase_index > 1,
    )
    return report, phase


def _database_counts(database_url: str, project_id: str, run_id: str) -> dict[str, int]:
    engine = build_engine(database_url)
    try:
        factory = build_session_factory(engine)
        with factory() as session:

            def count(model: type[Any], column: Any, value: str) -> int:
                return int(
                    session.scalar(select(func.count()).select_from(model).where(column == value))
                    or 0
                )

            return {
                "events": count(RunEventRow, RunEventRow.run_id, run_id),
                "tasks": count(RuntimeTaskProjectionRow, RuntimeTaskProjectionRow.run_id, run_id),
                "attempts": int(
                    session.scalar(
                        select(func.count())
                        .select_from(RuntimeTaskAttemptRow)
                        .join(
                            RuntimeTaskProjectionRow,
                            RuntimeTaskProjectionRow.task_id == RuntimeTaskAttemptRow.task_id,
                        )
                        .where(RuntimeTaskProjectionRow.run_id == run_id)
                    )
                    or 0
                ),
                "commits": count(CommitRow, CommitRow.project_id, project_id),
                "model_calls": count(ModelCallLedgerRow, ModelCallLedgerRow.run_id, run_id),
                "effects": count(
                    RuntimeEffectProjectionRow,
                    RuntimeEffectProjectionRow.run_id,
                    run_id,
                ),
            }
    finally:
        engine.dispose()


def _load_attempts(database_url: str, run_id: str) -> tuple[TaskAttempt, ...]:
    engine = build_engine(database_url)
    try:
        factory = build_session_factory(engine)
        with factory() as session:
            rows = session.scalars(
                select(RuntimeTaskAttemptRow)
                .join(
                    RuntimeTaskProjectionRow,
                    RuntimeTaskProjectionRow.task_id == RuntimeTaskAttemptRow.task_id,
                )
                .where(RuntimeTaskProjectionRow.run_id == run_id)
                .order_by(RuntimeTaskAttemptRow.attempt_id)
            )
            return tuple(
                TaskAttempt.model_validate_json(json.dumps(row.attempt_json), strict=True)
                for row in rows
            )
    finally:
        engine.dispose()


def _select_provider_transient_waiting_tasks(
    *,
    tasks: tuple[TaskRecord, ...],
    attempts: tuple[TaskAttempt, ...],
) -> tuple[TaskRecord, ...]:
    latest_by_task: dict[TaskId, TaskAttempt] = {}
    for attempt in attempts:
        prior = latest_by_task.get(attempt.task_id)
        if prior is None or attempt.attempt_no > prior.attempt_no:
            latest_by_task[attempt.task_id] = attempt
    return tuple(
        task
        for task in tasks
        if not task.superseded
        and task.status is TaskStatus.WAITING_RETRY
        and task.failure_budget > 0
        and latest_by_task.get(task.task_id) is not None
        and latest_by_task[task.task_id].failure_class is FailureClass.PROVIDER_TRANSIENT
    )


def _provider_transient_waiting_tasks(
    *,
    database_url: str,
    tasks: tuple[TaskRecord, ...],
) -> tuple[TaskRecord, ...]:
    """Return only durable WAITING_RETRY tasks whose latest Attempt was transient."""

    return _select_provider_transient_waiting_tasks(
        tasks=tasks,
        attempts=_load_attempts(database_url, tasks[0].run_id.root) if tasks else (),
    )


def _retry_provider_transient_task(
    *,
    database_url: str,
    task: TaskRecord,
    recovery_index: int,
) -> TaskRecord:
    """Retry one settled transient task through the existing command owner."""

    engine = build_engine(database_url)
    try:
        factory = build_session_factory(engine)
        commands = RuntimeCommandService(
            factory,
            RunEventLogRepository(factory),
            lambda _project_id: task.permission_hash,
        )
        current = commands.get_task(task.task_id)
        if current.project_id != task.project_id or current.run_id != task.run_id:
            raise RuntimeError("provider retry task identity does not match the phase run")
        if current.status is not TaskStatus.WAITING_RETRY:
            raise RuntimeError("provider retry task is no longer WAITING_RETRY")
        updated = commands.control(
            current.task_id,
            command_id=StableId(
                f"u6e-provider-retry.{current.task_id.root}."
                f"{current.task_revision}.{recovery_index}"[:128]
            ),
            action="retry",
            actor_id="u6e-endurance-recovery",
            reason="retry settled provider-transient effect through the typed command owner",
            observed_revision=current.task_revision,
        )
        if updated.status is not TaskStatus.READY:
            raise RuntimeError("provider transient retry did not return the task to READY")
        return updated
    finally:
        engine.dispose()


def _task_chapter(task: TaskRecord) -> int:
    if task.kind in {TaskKind.PLAN_CANDIDATE, TaskKind.PLAN_ACCEPTANCE, TaskKind.PLAN_COMMIT}:
        return int(task.chapter_index) + 1
    return int(task.chapter_index)


def _task_phase(task: TaskRecord) -> U6BPhase:
    if task.kind in {TaskKind.PLAN_CANDIDATE, TaskKind.PLAN_ACCEPTANCE, TaskKind.PLAN_COMMIT}:
        return "plan"
    if task.kind is TaskKind.DRAFT_COMMIT:
        return "settlement"
    if task.kind is TaskKind.DRAFT_CANDIDATE:
        return "writer"
    return "recovery"


def _phase_usage(
    *,
    tasks: tuple[TaskRecord, ...],
    attempts: tuple[TaskAttempt, ...],
    ledger: tuple[Any, ...],
    expected_chapters: tuple[int, ...],
) -> tuple[U6BPhaseUsage, ...]:
    values: dict[tuple[int, U6BPhase], dict[str, int]] = defaultdict(
        lambda: {
            "wall_clock_ms": 0,
            "model_call_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "attempt_count": 0,
        }
    )
    task_by_id = {task.task_id: task for task in tasks}

    def add(chapter: int, phase: U6BPhase, **increments: int) -> None:
        if chapter < 1 or chapter not in expected_chapters or phase not in PHASES:
            return
        target = values[(chapter, phase)]
        for key, amount in increments.items():
            target[key] += amount

    for attempt in attempts:
        task = task_by_id.get(attempt.task_id)
        if task is None:
            continue
        chapter = _task_chapter(task)
        phase = (
            "recovery"
            if attempt.attempt_no > 1 or attempt.failure_class is not None
            else _task_phase(task)
        )
        duration = 0
        if attempt.started_at is not None and attempt.ended_at is not None:
            duration = max(0, int((attempt.ended_at - attempt.started_at).total_seconds() * 1000))
        add(chapter, phase, wall_clock_ms=duration, attempt_count=1)

    for entry in ledger:
        task = task_by_id.get(entry.task_id)
        record = entry.call_record
        if task is None or record is None:
            continue
        chapter = _task_chapter(task)
        logical = entry.logical_phase.lower()
        if task.kind in {TaskKind.PLAN_CANDIDATE, TaskKind.PLAN_ACCEPTANCE, TaskKind.PLAN_COMMIT}:
            phase = "plan"
        elif "memory" in logical or "context" in logical or "reactive" in logical:
            phase = "memory"
        elif "editor" in logical or "review" in logical or "repair" in logical:
            phase = "editor"
        else:
            phase = "writer"
        add(
            chapter,
            phase,
            wall_clock_ms=record.latency_ms,
            model_call_count=1,
            input_tokens=record.usage.input_tokens,
            output_tokens=record.usage.output_tokens,
        )

    return tuple(
        U6BPhaseUsage(chapter_index=chapter, phase=phase, **values[(chapter, phase)])
        for chapter in expected_chapters
        for phase in PHASES
    )


def _load_writing_results(
    *,
    tasks: tuple[TaskRecord, ...],
    object_store_root: Path,
) -> tuple[tuple[TaskRecord, WritingLoopResult], ...]:
    artifacts = ArtifactRepository(FilesystemObjectStore(object_store_root))
    results: list[tuple[TaskRecord, WritingLoopResult]] = []
    for task in tasks:
        if task.kind is not TaskKind.DRAFT_CANDIDATE:
            continue
        ref = next(
            (
                item
                for item in task.terminal_artifact_refs
                if item.media_type == WRITING_LOOP_RESULT_MEDIA_TYPE
            ),
            None,
        )
        if ref is None:
            continue
        result = WritingLoopResult.model_validate_json(artifacts.read_verified(ref), strict=True)
        results.append((task, result))
    return tuple(results)


def _compaction_evidence(
    *,
    results: tuple[tuple[TaskRecord, WritingLoopResult], ...],
    events: tuple[Any, ...],
    min_reduction_ratio: float,
) -> tuple[U6BCompactionEvidence, ...]:
    output: list[U6BCompactionEvidence] = []
    for task, result in results:
        if result.context_view is None:
            raise RuntimeError("U6-B writing result lacks context view for compaction evidence")
        context_view = result.context_view
        pressure_events = tuple(
            event
            for event in events
            if event.task_id == task.task_id
            and event.event_type.value == "context.pressure_detected"
        )
        if result.compaction_receipts:
            for receipt in result.compaction_receipts:
                input_tokens = receipt.input_context_tokens
                output_tokens = receipt.output_context_tokens
                if input_tokens is None or output_tokens is None:
                    raise RuntimeError("U6-B compaction receipt lacks token measurements")
                ratio = (input_tokens - output_tokens) / input_tokens
                outcome = (
                    U6BCompactionOutcome.COMPACTED
                    if ratio >= min_reduction_ratio
                    else U6BCompactionOutcome.INEFFECTIVE
                )
                retained = all(
                    not item.mandatory and not item.pending_effect
                    for item in receipt.compacted_items
                )
                output.append(
                    U6BCompactionEvidence(
                        receipt_id=StableId(
                            f"u6b.compaction.{task.task_id.root}.{receipt.receipt_id.root}"[:128]
                        ),
                        run_id=task.run_id,
                        task_id=task.task_id,
                        chapter_index=task.chapter_index,
                        outcome=outcome,
                        input_context_tokens=input_tokens,
                        output_context_tokens=output_tokens,
                        reduction_ratio=ratio,
                        min_reduction_ratio=min_reduction_ratio,
                        covered_event_range=receipt.covered_event_range,
                        protected_items_retained=all(
                            item.mandatory for item in context_view.protected_items
                        ),
                        pending_effects_retained=retained,
                        safe_cut=receipt.safe_cut,
                        semantic_retention_passed=receipt.safe_cut and retained,
                        source_receipt_id=receipt.receipt_id,
                    )
                )
            continue
        if pressure_events:
            pressure = pressure_events[-1].payload["pressure"]
            input_tokens = int(pressure["rendered_input_tokens"])
            output_tokens = input_tokens
            output.append(
                U6BCompactionEvidence(
                    receipt_id=StableId(f"u6b.compaction.ineffective.{task.task_id.root}"[:128]),
                    run_id=task.run_id,
                    task_id=task.task_id,
                    chapter_index=task.chapter_index,
                    outcome=U6BCompactionOutcome.INEFFECTIVE,
                    input_context_tokens=input_tokens,
                    output_context_tokens=output_tokens,
                    reduction_ratio=0.0,
                    min_reduction_ratio=min_reduction_ratio,
                    covered_event_range=(
                        pressure_events[-1].sequence_no,
                        pressure_events[-1].sequence_no,
                    ),
                    protected_items_retained=all(
                        item.mandatory for item in context_view.protected_items
                    ),
                    pending_effects_retained=True,
                    safe_cut=True,
                    semantic_retention_passed=True,
                )
            )
            continue
        rendered = int(context_view.token_report.get("rendered", 0))
        position = max(1, context_view.basis_event_position)
        output.append(
            U6BCompactionEvidence(
                receipt_id=StableId(f"u6b.compaction.no-op.{task.task_id.root}"[:128]),
                run_id=task.run_id,
                task_id=task.task_id,
                chapter_index=task.chapter_index,
                outcome=U6BCompactionOutcome.NO_OP,
                input_context_tokens=rendered,
                output_context_tokens=rendered,
                reduction_ratio=0.0,
                min_reduction_ratio=min_reduction_ratio,
                covered_event_range=(position, position),
                protected_items_retained=all(
                    item.mandatory for item in context_view.protected_items
                ),
                pending_effects_retained=True,
                safe_cut=True,
                semantic_retention_passed=True,
            )
        )
    return tuple(output)


def _main_report(
    *,
    request: CreativeRunRequest,
    final: Stage5VerticalRunReport,
    phases: tuple[U6BWorkerPhaseReport, ...],
    database_url: str,
    object_store_root: Path,
    min_reduction_ratio: float,
) -> U6BProductionBaselineReport:
    expected = tuple(range(request.current_chapter + 1, request.target_chapters + 1))
    engine = build_engine(database_url)
    try:
        factory = build_session_factory(engine)
        events = RunEventLogRepository(factory).replay(request.run_id)
        ledger = SqlModelCallLedger(factory).list_for_run(request.run_id)
    finally:
        engine.dispose()
    tasks = tuple(final.tasks)
    attempts = _load_attempts(database_url, request.run_id.root)
    writing_results = _load_writing_results(tasks=tasks, object_store_root=object_store_root)
    compaction = _compaction_evidence(
        results=writing_results,
        events=events,
        min_reduction_ratio=min_reduction_ratio,
    )
    counts = _database_counts(database_url, request.project_id.root, request.run_id.root)
    phase_usage = _phase_usage(
        tasks=tasks,
        attempts=attempts,
        ledger=ledger,
        expected_chapters=expected,
    )
    try:
        assert_task_projection_matches(events, tasks)
        projection_rebuild_verified = all(
            any(
                task.kind is TaskKind.PROJECTION_FRESHNESS
                and task.chapter_index == chapter
                and task.projection_after == "draft"
                and task.status is TaskStatus.SUCCEEDED
                for task in tasks
            )
            for chapter in expected
        )
    except (AssertionError, RuntimeError, ValueError):
        projection_rebuild_verified = False
    future_leakage_count = sum(
        1
        for _task, result in writing_results
        if result.context_view is None
        or result.context_view.information_scope != "writer_safe"
        or any(
            item.information_scope == "planner_safe" for item in result.context_view.protected_items
        )
    )
    semantic_findings = tuple(
        sorted(
            {
                f"C{task.chapter_index}:{issue.issue_type.value}"
                for task, result in writing_results
                for report in result.editorial_reports
                for issue in report.issues
            }
        )
    )
    repair_count = sum(
        1
        for _task, result in writing_results
        for report in result.editorial_reports
        if report.verdict.value != "PASS"
    )
    request_ids = [entry.request_id.root for entry in ledger]
    duplicate_effect_count = len(request_ids) - len(set(request_ids))
    clean = (
        final.status is VerticalRunStatus.COMPLETED
        and tuple(final.completed_chapters) == expected
        and len(writing_results) == len(expected)
        and all(
            result.status is WritingLoopTerminalStatus.DRAFT_CANDIDATE_READY
            for _, result in writing_results
        )
        and all(item.outcome is not U6BCompactionOutcome.INEFFECTIVE for item in compaction)
        and future_leakage_count == 0
        and duplicate_effect_count == 0
        and projection_rebuild_verified
    )
    artifact_count = (
        sum(
            1
            for path in (object_store_root / "sha256").rglob("*")
            if path.is_file() and not path.name.endswith(".metadata.json")
        )
        if (object_store_root / "sha256").exists()
        else 0
    )
    return U6BProductionBaselineReport(
        status="PASS" if clean else "REVIEW_REQUIRED",
        run_id=request.run_id,
        project_id=request.project_id,
        basis_commit=request.basis_commit,
        final_commit=final.final_commit,
        expected_chapters=expected,
        completed_chapters=final.completed_chapters,
        restart_boundary_chapter=phases[0].completed_chapters_after[-1],
        worker_phases=phases,
        phase_usage=phase_usage,
        compaction=compaction,
        model_call_count=counts["model_calls"],
        input_tokens=sum(
            entry.call_record.usage.input_tokens
            for entry in ledger
            if entry.call_record is not None
        ),
        output_tokens=sum(
            entry.call_record.usage.output_tokens
            for entry in ledger
            if entry.call_record is not None
        ),
        event_count=counts["events"],
        task_count=counts["tasks"],
        attempt_count=counts["attempts"],
        commit_count=counts["commits"],
        artifact_count=artifact_count,
        future_leakage_count=future_leakage_count,
        duplicate_effect_count=duplicate_effect_count,
        projection_rebuild_verified=projection_rebuild_verified,
        semantic_findings=semantic_findings,
        repair_count=repair_count,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--object-store-root", type=Path, required=True)
    parser.add_argument("--endpoint-profile", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--restart-after-chapter", type=int, required=True)
    parser.add_argument("--max-tasks", type=int, default=1)
    parser.add_argument("--max-slices", type=int, default=2000)
    parser.add_argument("--min-reduction-ratio", type=float, required=True)
    args = parser.parse_args()

    if args.output_root.exists():
        raise RuntimeError(f"U6-B refuses to reuse output root: {args.output_root}")
    request = CreativeRunRequest.model_validate_json(args.request.read_bytes(), strict=True)
    expected = tuple(range(request.current_chapter + 1, request.target_chapters + 1))
    if len(expected) != 20:
        raise ValueError("U6-B requires exactly 20 target chapters")
    if not request.current_chapter < args.restart_after_chapter < request.target_chapters:
        raise ValueError("U6-B restart boundary must be strictly inside the target range")
    if not 0.0 <= args.min_reduction_ratio <= 1.0:
        raise ValueError("U6-B min reduction ratio must be between zero and one")
    args.output_root.mkdir(parents=True)
    manifest = {
        "schema": "u6b-production-baseline-manifest.v1",
        "run_id": request.run_id.root,
        "project_id": request.project_id.root,
        "basis_commit": request.basis_commit.root,
        "target_chapters": expected,
        "restart_after_chapter": args.restart_after_chapter,
        "endpoint_profile": args.endpoint_profile,
        "max_tasks": args.max_tasks,
        "max_slices": args.max_slices,
        "min_reduction_ratio": args.min_reduction_ratio,
        "database_descriptor": database_url_descriptor(args.database_url),
        "object_store_root": str(args.object_store_root.resolve()),
    }
    _write_once(args.output_root / "u6b-production-baseline-manifest.json", manifest)
    phase_reports: list[U6BWorkerPhaseReport] = []
    before: tuple[int, ...] = ()
    first, first_phase = _run_worker_phase(
        phase_index=1,
        request=args.request,
        manifest=args.manifest,
        database_url=args.database_url,
        object_store_root=args.object_store_root,
        endpoint_profile=args.endpoint_profile,
        output_root=args.output_root,
        max_tasks=args.max_tasks,
        max_slices=args.max_slices,
        stop_after_chapter=args.restart_after_chapter,
        before=before,
    )
    phase_reports.append(first_phase)
    second, second_phase = _run_worker_phase(
        phase_index=2,
        request=args.request,
        manifest=args.manifest,
        database_url=args.database_url,
        object_store_root=args.object_store_root,
        endpoint_profile=args.endpoint_profile,
        output_root=args.output_root,
        max_tasks=args.max_tasks,
        max_slices=args.max_slices,
        stop_after_chapter=None,
        before=tuple(first.completed_chapters),
    )
    phase_reports.append(second_phase)
    report = _main_report(
        request=request,
        final=second,
        phases=tuple(phase_reports),
        database_url=args.database_url,
        object_store_root=args.object_store_root,
        min_reduction_ratio=args.min_reduction_ratio,
    )
    report_path = args.output_root / "u6b-production-baseline-report.json"
    _write_once(report_path, report.model_dump(mode="json"))
    print(report.model_dump_json(indent=2))
    return 0 if report.status == "PASS" else 2


def database_url_descriptor(database_url: str) -> str:
    from urllib.parse import urlsplit

    parsed = urlsplit(database_url)
    if parsed.hostname is None:
        return database_url
    authority = parsed.hostname
    if parsed.port is not None:
        authority = f"{authority}:{parsed.port}"
    return f"{parsed.scheme}://{authority}{parsed.path}"


if __name__ == "__main__":
    raise SystemExit(main())
