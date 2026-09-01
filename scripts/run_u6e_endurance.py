#!/usr/bin/env python3
"""Run the U6-E fifty-chapter endurance baseline with one cold restart."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal

try:
    from scripts.run_u6b_production_baseline import (
        _database_counts,
        _load_attempts,
        _main_report,
        _run_worker_phase,
    )
except ModuleNotFoundError as error:
    if error.name != "scripts":
        raise
    from run_u6b_production_baseline import (
        _database_counts,
        _load_attempts,
        _main_report,
        _run_worker_phase,
    )
from sqlalchemy import func, select

from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.adapters.postgres.models import (
    CommitReceiptRow,
    CommitRow,
    ModelCallLedgerRow,
    ProjectRow,
    RuntimeEffectProjectionRow,
    RuntimeTaskAttemptRow,
    RuntimeTaskProjectionRow,
)
from novel_agent.domain.creative_runtime import CreativeRunRequest
from novel_agent.domain.runtime import TaskAttempt, TaskRecord, TaskStatus
from novel_agent.domain.stage5_evaluation import Stage5VerticalRunReport, VerticalRunStatus
from novel_agent.domain.u6b_production import U6BWorkerPhaseReport
from novel_agent.domain.u6e_endurance import (
    U6EEnduranceReport,
    U6EHealthProbe,
    U6EHistoryGrowth,
    U6EWorkerPhaseReport,
)


def _write_once(path: Path, payload: object) -> None:
    if path.exists():
        raise RuntimeError(f"U6-E refuses to overwrite output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _artifact_count(object_store_root: Path) -> int:
    root = object_store_root / "sha256"
    if not root.exists():
        return 0
    return sum(
        1 for path in root.rglob("*") if path.is_file() and not path.name.endswith(".metadata.json")
    )


def _growth(
    *,
    database_url: str,
    request: CreativeRunRequest,
    object_store_root: Path,
    chapter_index: int,
) -> U6EHistoryGrowth:
    counts = _database_counts(database_url, request.project_id.root, request.run_id.root)
    return U6EHistoryGrowth(
        chapter_index=chapter_index,
        event_count=counts["events"],
        task_count=counts["tasks"],
        attempt_count=counts["attempts"],
        effect_count=counts["effects"],
        model_call_count=counts["model_calls"],
        artifact_count=_artifact_count(object_store_root),
    )


def _health_probe(
    *,
    probe_id: str,
    chapter_index: int,
    report: Stage5VerticalRunReport,
    growth: U6EHistoryGrowth,
) -> U6EHealthProbe:
    status: Literal["PASS", "REVIEW_REQUIRED"] = (
        "PASS"
        if report.status in {VerticalRunStatus.YIELDED, VerticalRunStatus.COMPLETED}
        else "REVIEW_REQUIRED"
    )
    return U6EHealthProbe(
        probe_id=probe_id,
        chapter_index=chapter_index,
        status=status,
        event_count=growth.event_count,
        task_count=growth.task_count,
        model_call_count=growth.model_call_count,
        detail=f"vertical_status={report.status.value};completed={report.completed_chapters}",
    )


def _duplicate_commit_count(database_url: str, project_id: str) -> int:
    engine = build_engine(database_url)
    try:
        factory = build_session_factory(engine)
        with factory() as session:
            rows = session.execute(
                select(CommitReceiptRow.idempotency_key, func.count())
                .where(CommitReceiptRow.project_id == project_id)
                .group_by(CommitReceiptRow.idempotency_key)
                .having(func.count() > 1)
            )
            return sum(max(0, int(count) - 1) for _key, count in rows)
    finally:
        engine.dispose()


def _unrecoverable_task_count(tasks: tuple[TaskRecord, ...]) -> int:
    terminal_bad = {
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.RECOVERY_PENDING,
        TaskStatus.BUDGET_REVIEW,
    }
    return sum(1 for task in tasks if not task.superseded and task.status in terminal_bad)


def _external_wait_count(tasks: tuple[TaskRecord, ...]) -> int:
    return sum(
        1 for task in tasks if not task.superseded and task.status is TaskStatus.WAITING_INPUT
    )


def _repeated_failure_count(attempts: tuple[TaskAttempt, ...]) -> int:
    return sum(1 for attempt in attempts if attempt.attempt_no > 1)


def _phase_report(phase: U6BWorkerPhaseReport) -> U6EWorkerPhaseReport:
    return U6EWorkerPhaseReport(
        phase_index=phase.phase_index,
        report_path=phase.report_path,
        status=phase.status,
        completed_chapters_before=phase.completed_chapters_before,
        completed_chapters_after=phase.completed_chapters_after,
        restarted_from_process=phase.restarted_from_process,
    )


def _canary_durable_evidence(
    *,
    database_url: str,
    report: Stage5VerticalRunReport,
    request: CreativeRunRequest,
) -> dict[str, object]:
    """Rebuild the one-chapter acceptance chain from PostgreSQL and the report refs."""

    engine = build_engine(database_url)
    try:
        factory = build_session_factory(engine)
        with factory() as session:
            current_commit = session.scalar(
                select(ProjectRow.current_commit_id).where(
                    ProjectRow.project_id == request.project_id.root
                )
            )
            commit_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(CommitRow)
                    .where(CommitRow.project_id == request.project_id.root)
                )
                or 0
            )
            attempt_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(RuntimeTaskAttemptRow)
                    .join(
                        RuntimeTaskProjectionRow,
                        RuntimeTaskProjectionRow.task_id == RuntimeTaskAttemptRow.task_id,
                    )
                    .where(RuntimeTaskProjectionRow.run_id == request.run_id.root)
                )
                or 0
            )
            effect_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(RuntimeEffectProjectionRow)
                    .where(RuntimeEffectProjectionRow.run_id == request.run_id.root)
                )
                or 0
            )
            ledger_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(ModelCallLedgerRow)
                    .where(ModelCallLedgerRow.run_id == request.run_id.root)
                )
                or 0
            )
            raw_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(ModelCallLedgerRow)
                    .where(
                        ModelCallLedgerRow.run_id == request.run_id.root,
                        ModelCallLedgerRow.raw_response_hash.is_not(None),
                    )
                )
                or 0
            )
            provider_id_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(ModelCallLedgerRow)
                    .where(
                        ModelCallLedgerRow.run_id == request.run_id.root,
                        ModelCallLedgerRow.provider_request_id.is_not(None),
                    )
                )
                or 0
            )
            provider_id_distinct_count = int(
                session.scalar(
                    select(func.count(func.distinct(ModelCallLedgerRow.provider_request_id))).where(
                        ModelCallLedgerRow.run_id == request.run_id.root,
                        ModelCallLedgerRow.provider_request_id.is_not(None),
                    )
                )
                or 0
            )
        terminal_ref_count = sum(len(task.terminal_artifact_refs) for task in report.tasks)
        duplicate_commits = _duplicate_commit_count(database_url, request.project_id.root)
        durable_commit = current_commit == report.final_commit.root and (
            report.final_commit != request.basis_commit
        )
        no_duplicate_provider_calls = provider_id_count == provider_id_distinct_count
        return {
            "current_commit": current_commit,
            "commit_count": commit_count,
            "attempt_count": attempt_count,
            "effect_count": effect_count,
            "model_call_count": ledger_count,
            "raw_response_count": raw_count,
            "provider_request_id_count": provider_id_count,
            "provider_request_id_distinct_count": provider_id_distinct_count,
            "terminal_artifact_ref_count": terminal_ref_count,
            "duplicate_commit_count": duplicate_commits,
            "durable_commit": durable_commit,
            "raw_chain_rebuilt": ledger_count > 0 and raw_count == ledger_count,
            "terminal_chain_rebuilt": terminal_ref_count > 0,
            "no_duplicate_provider_calls": no_duplicate_provider_calls,
            "no_duplicate_commits": duplicate_commits == 0,
        }
    finally:
        engine.dispose()


def _is_typed_content_outcome(report: Stage5VerticalRunReport) -> bool:
    markers = ("REJECTED", "ADVISORY", "OUTPUT_INCOMPLETE", "UNSUPPORTED")
    return any(
        any(marker in result.reason_code.upper() for marker in markers)
        and "FATAL" not in result.reason_code.upper()
        for result in report.runtime_results
    )


def _run_canary(
    *,
    args: argparse.Namespace,
    request: CreativeRunRequest,
) -> int:
    args.output_root.mkdir(parents=True)
    _write_once(
        args.output_root / "u6e-canary-manifest.json",
        {
            "schema": "u6e-settlement-canary-manifest.v1",
            "run_id": request.run_id.root,
            "project_id": request.project_id.root,
            "basis_commit": request.basis_commit.root,
            "target_chapter": request.target_chapters,
            "endpoint_profile": args.endpoint_profile,
            "max_tasks": args.max_tasks,
            "max_slices": args.max_slices,
            "max_provider_retries": args.max_provider_retries,
            "settlement_timeout_seconds": args.settlement_timeout_seconds,
            "settlement_output_tokens": args.settlement_output_tokens,
            "max_major_rewrites": args.max_major_rewrites,
            "max_local_repairs": args.max_local_repairs,
            "fresh_process_required": True,
            "formal_endurance_admission": False,
        },
    )
    try:
        report, phase = _run_worker_phase(
            phase_index=1,
            request=args.request,
            manifest=args.manifest,
            database_url=args.database_url,
            object_store_root=args.object_store_root,
            endpoint_profile=args.endpoint_profile,
            output_root=args.output_root,
            max_tasks=args.max_tasks,
            max_slices=args.max_slices,
            stop_after_chapter=None,
            before=(),
            max_provider_retries=args.max_provider_retries,
            settlement_timeout_seconds=args.settlement_timeout_seconds,
            settlement_output_tokens=args.settlement_output_tokens,
            max_major_rewrites=args.max_major_rewrites,
            max_local_repairs=args.max_local_repairs,
        )
        worker_error = None
    except RuntimeError as error:
        report_path = args.output_root / "worker-phase-1.vertical-run-report.json"
        if not report_path.exists():
            raise
        report = Stage5VerticalRunReport.model_validate_json(report_path.read_bytes(), strict=True)
        phase = U6EWorkerPhaseReport(
            phase_index=1,
            report_path=str(report_path.resolve()),
            status=report.status.value,
            completed_chapters_after=report.completed_chapters,
            restarted_from_process=False,
        )
        worker_error = str(error)
    evidence = _canary_durable_evidence(
        database_url=args.database_url,
        report=report,
        request=request,
    )
    committed = (
        report.status is VerticalRunStatus.COMPLETED
        and report.completed_chapters == (request.target_chapters,)
        and bool(evidence["durable_commit"])
        and bool(evidence["raw_chain_rebuilt"])
        and bool(evidence["terminal_chain_rebuilt"])
        and bool(evidence["no_duplicate_provider_calls"])
        and bool(evidence["no_duplicate_commits"])
    )
    typed_content = _is_typed_content_outcome(report)
    status = (
        "PASS" if committed else "TYPED_CONTENT_REJECTION" if typed_content else "REVIEW_REQUIRED"
    )
    payload = {
        "schema": "u6e-settlement-canary.v1",
        "status": status,
        "run_id": request.run_id.root,
        "project_id": request.project_id.root,
        "basis_commit": request.basis_commit.root,
        "target_chapter": request.target_chapters,
        "completed_chapters": list(report.completed_chapters),
        "final_commit": report.final_commit.root,
        "worker_phase": phase.model_dump(mode="json"),
        "fresh_process": True,
        "durable_evidence": evidence,
        "typed_content_outcome": typed_content,
        "worker_error": worker_error,
        "formal_endurance_admission": False,
    }
    _write_once(args.output_root / "u6e-settlement-canary.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if status in {"PASS", "TYPED_CONTENT_REJECTION"} else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--object-store-root", type=Path, required=True)
    parser.add_argument("--endpoint-profile", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--canary", action="store_true")
    parser.add_argument("--restart-after-chapter", type=int, default=45)
    parser.add_argument("--max-tasks", type=int, default=1)
    parser.add_argument("--max-slices", type=int, default=5000)
    parser.add_argument("--max-provider-retries", type=int, default=2)
    parser.add_argument("--min-reduction-ratio", type=float, default=0.1)
    parser.add_argument(
        "--settlement-timeout-seconds",
        type=float,
        default=120.0,
        help="U6-E-only Curator transport timeout; production default remains 60 seconds",
    )
    parser.add_argument(
        "--settlement-output-tokens",
        type=int,
        default=12_000,
        help="U6-E-only Curator output cap; production default remains 8000 tokens",
    )
    parser.add_argument(
        "--max-major-rewrites",
        type=int,
        default=None,
        help="U6-E-only Writer major-rewrite allowance; production default remains 1",
    )
    parser.add_argument(
        "--max-local-repairs",
        type=int,
        default=None,
        help="U6-E-only Editor local-repair allowance; production default remains 1",
    )
    args = parser.parse_args()
    if args.output_root.exists():
        raise RuntimeError(f"U6-E refuses to reuse output root: {args.output_root}")
    request = CreativeRunRequest.model_validate_json(args.request.read_bytes(), strict=True)
    expected = tuple(range(request.current_chapter + 1, request.target_chapters + 1))
    if args.canary:
        if len(expected) != 1:
            raise ValueError("U6-E canary requires exactly one target chapter")
        if args.output_root.exists():
            raise RuntimeError(f"U6-E refuses to reuse output root: {args.output_root}")
        return _run_canary(args=args, request=request)
    if len(expected) != 50:
        raise ValueError("U6-E requires exactly fifty target chapters")
    if not request.current_chapter < args.restart_after_chapter < request.target_chapters:
        raise ValueError("U6-E restart boundary must be strictly inside the target range")
    if not 0.0 <= args.min_reduction_ratio <= 1.0:
        raise ValueError("U6-E min reduction ratio must be between zero and one")
    if not 0.0 < args.settlement_timeout_seconds <= 900.0:
        raise ValueError("settlement timeout must be between zero and 900 seconds")
    if not 0 < args.settlement_output_tokens <= 131_072:
        raise ValueError("settlement output tokens must be between one and 131072")
    if args.max_major_rewrites is not None and not 0 <= args.max_major_rewrites <= 2:
        raise ValueError("max major rewrites must be between zero and two")
    if args.max_local_repairs is not None and not 0 <= args.max_local_repairs <= 2:
        raise ValueError("max local repairs must be between zero and two")
    args.output_root.mkdir(parents=True)
    _write_once(
        args.output_root / "u6e-endurance-manifest.json",
        {
            "schema": "u6e-endurance-manifest.v1",
            "run_id": request.run_id.root,
            "project_id": request.project_id.root,
            "basis_commit": request.basis_commit.root,
            "target_chapters": expected,
            "restart_after_chapter": args.restart_after_chapter,
            "endpoint_profile": args.endpoint_profile,
            "max_tasks": args.max_tasks,
            "max_slices": args.max_slices,
            "max_provider_retries": args.max_provider_retries,
            "min_reduction_ratio": args.min_reduction_ratio,
            "settlement_timeout_seconds": args.settlement_timeout_seconds,
            "settlement_output_tokens": args.settlement_output_tokens,
            "max_major_rewrites": args.max_major_rewrites,
            "max_local_repairs": args.max_local_repairs,
            "database_descriptor": args.database_url.rsplit("@", 1)[-1],
            "object_store_root": str(args.object_store_root.resolve()),
            "health_probes": ["restart-boundary", "final-completion"],
        },
    )

    baseline = _growth(
        database_url=args.database_url,
        request=request,
        object_store_root=args.object_store_root,
        chapter_index=request.current_chapter,
    )
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
        before=(),
        max_provider_retries=args.max_provider_retries,
        settlement_timeout_seconds=args.settlement_timeout_seconds,
        settlement_output_tokens=args.settlement_output_tokens,
        max_major_rewrites=args.max_major_rewrites,
        max_local_repairs=args.max_local_repairs,
    )
    first_growth = _growth(
        database_url=args.database_url,
        request=request,
        object_store_root=args.object_store_root,
        chapter_index=first.completed_chapters[-1],
    )
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
        max_provider_retries=args.max_provider_retries,
        settlement_timeout_seconds=args.settlement_timeout_seconds,
        settlement_output_tokens=args.settlement_output_tokens,
        max_major_rewrites=args.max_major_rewrites,
        max_local_repairs=args.max_local_repairs,
    )
    final_growth = _growth(
        database_url=args.database_url,
        request=request,
        object_store_root=args.object_store_root,
        chapter_index=second.completed_chapters[-1],
    )
    u6b_report = _main_report(
        request=request,
        final=second,
        phases=(first_phase, second_phase),
        database_url=args.database_url,
        object_store_root=args.object_store_root,
        min_reduction_ratio=args.min_reduction_ratio,
    )
    attempts = _load_attempts(args.database_url, request.run_id.root)
    phase_reports = (_phase_report(first_phase), _phase_report(second_phase))
    health_probes = (
        _health_probe(
            probe_id="restart-boundary",
            chapter_index=first.completed_chapters[-1],
            report=first,
            growth=first_growth,
        ),
        _health_probe(
            probe_id="final-completion",
            chapter_index=second.completed_chapters[-1],
            report=second,
            growth=final_growth,
        ),
    )
    final_tasks = tuple(second.tasks)
    unrecoverable = _unrecoverable_task_count(final_tasks)
    external_wait = _external_wait_count(final_tasks)
    repeated_failures = _repeated_failure_count(attempts)
    duplicate_commits = _duplicate_commit_count(args.database_url, request.project_id.root)
    clean_status: Literal["PASS", "REVIEW_REQUIRED"] = (
        "PASS"
        if u6b_report.status == "PASS"
        and not unrecoverable
        and not external_wait
        and not duplicate_commits
        and second.status is VerticalRunStatus.COMPLETED
        and second_phase.restarted_from_process
        else "REVIEW_REQUIRED"
    )
    report = U6EEnduranceReport(
        status=clean_status,
        run_id=request.run_id,
        project_id=request.project_id,
        basis_commit=request.basis_commit,
        final_commit=second.final_commit,
        expected_chapters=expected,
        completed_chapters=second.completed_chapters,
        restart_boundary_chapter=args.restart_after_chapter,
        worker_phases=phase_reports,
        history_growth=(baseline, first_growth, final_growth),
        health_probes=health_probes,
        phase_usage=u6b_report.phase_usage,
        compaction=u6b_report.compaction,
        model_call_count=u6b_report.model_call_count,
        input_tokens=u6b_report.input_tokens,
        output_tokens=u6b_report.output_tokens,
        event_count=u6b_report.event_count,
        task_count=u6b_report.task_count,
        attempt_count=u6b_report.attempt_count,
        commit_count=u6b_report.commit_count,
        artifact_count=u6b_report.artifact_count,
        future_leakage_count=u6b_report.future_leakage_count,
        duplicate_effect_count=u6b_report.duplicate_effect_count,
        duplicate_commit_count=duplicate_commits,
        unrecoverable_task_count=unrecoverable,
        external_wait_count=external_wait,
        repeated_failure_count=repeated_failures,
        projection_rebuild_verified=u6b_report.projection_rebuild_verified,
        cold_restart_verified=second_phase.restarted_from_process,
        process_memory_dependency=False,
        semantic_findings=u6b_report.semantic_findings,
        repair_count=u6b_report.repair_count,
    )
    _write_once(args.output_root / "u6e-endurance-report.json", report.model_dump(mode="json"))
    print(report.model_dump_json(indent=2))
    return 0 if report.status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
