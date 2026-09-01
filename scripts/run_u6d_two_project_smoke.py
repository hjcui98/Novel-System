#!/usr/bin/env python3
"""Run the minimum two-project U6-D production admission smoke."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, select

from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.adapters.postgres.models import (
    CommitRow,
    ModelCallLedgerRow,
    RunEventRow,
    RuntimeEffectProjectionRow,
    RuntimeTaskAttemptRow,
    RuntimeTaskProjectionRow,
)
from novel_agent.domain.creative_runtime import CreativeRunRequest
from novel_agent.domain.ids import ProjectId, RunId, SchemaVersion, StableId
from novel_agent.domain.runtime import TaskKind, TaskRecord, TaskStatus
from novel_agent.domain.stage5_evaluation import Stage5VerticalRunReport, VerticalRunStatus
from novel_agent.domain.stage5_manifest import load_stage5_manifest
from novel_agent.domain.u6d_two_project import (
    U6DAdmissionEvidence,
    U6DProjectSmokeResult,
    U6DTwoProjectSmokeReport,
)
from novel_agent.runtime.creative_assembly import (
    ProductionRuntimeAssembly,
)
from novel_agent.runtime.production_bootstrap import (
    resolve_registered_model_endpoints,
)
from novel_agent.runtime.production_dispatch_coordinator import (
    ProductionDispatchCoordinator,
    ProductionRunDescriptor,
)
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.model_request_admission import ModelRequestAdmissionController
from novel_agent.services.runtime_commands import RuntimeCommandService

SCHEMA_VERSION = SchemaVersion("1.0.0")
EVIDENCE_MEDIA_TYPE = "application/vnd.novel-agent.u6d-project-smoke+json"
PER_PROJECT_ATTESTATION_FIELDS = {
    "object_store_root",
    "session_factory_identity",
    "configuration_fingerprint",
}
REQUIRED_CHAIN: tuple[tuple[TaskKind, int], ...] = (
    (TaskKind.PLAN_CANDIDATE, 20),
    (TaskKind.PLAN_ACCEPTANCE, 20),
    (TaskKind.PLAN_COMMIT, 20),
    (TaskKind.DRAFT_CANDIDATE, 21),
    (TaskKind.DRAFT_ACCEPTANCE, 21),
    (TaskKind.DRAFT_COMMIT, 21),
    (TaskKind.PROJECTION_FRESHNESS, 21),
)


def _write_once(path: Path, payload: object) -> None:
    if path.exists():
        raise RuntimeError(f"U6-D refuses to overwrite output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _database_descriptor(database_url: str) -> str:
    return database_url.rsplit("@", 1)[-1]


def _count(session: Any, model: Any, column: Any, value: str) -> int:
    return int(session.scalar(select(func.count()).select_from(model).where(column == value)) or 0)


def _project_snapshot(
    database_url: str,
    *,
    project_id: ProjectId,
    run_id: RunId,
) -> tuple[TaskRecord, ...]:
    engine = build_engine(database_url)
    try:
        factory = build_session_factory(engine)
        with factory() as session:
            rows = session.scalars(
                select(RuntimeTaskProjectionRow)
                .where(
                    RuntimeTaskProjectionRow.project_id == project_id.root,
                    RuntimeTaskProjectionRow.run_id == run_id.root,
                )
                .order_by(RuntimeTaskProjectionRow.task_id)
            )
            return tuple(
                TaskRecord.model_validate_json(json.dumps(row.task_json), strict=True)
                for row in rows
            )
    finally:
        engine.dispose()


def _database_counts(
    database_url: str,
    *,
    project_id: ProjectId,
    run_id: RunId,
) -> dict[str, int]:
    engine = build_engine(database_url)
    try:
        factory = build_session_factory(engine)
        with factory() as session:
            return {
                "events": _count(session, RunEventRow, RunEventRow.run_id, run_id.root),
                "tasks": _count(
                    session,
                    RuntimeTaskProjectionRow,
                    RuntimeTaskProjectionRow.run_id,
                    run_id.root,
                ),
                "attempts": int(
                    session.scalar(
                        select(func.count())
                        .select_from(RuntimeTaskAttemptRow)
                        .join(
                            RuntimeTaskProjectionRow,
                            RuntimeTaskProjectionRow.task_id == RuntimeTaskAttemptRow.task_id,
                        )
                        .where(RuntimeTaskProjectionRow.run_id == run_id.root)
                    )
                    or 0
                ),
                "effects": _count(
                    session,
                    RuntimeEffectProjectionRow,
                    RuntimeEffectProjectionRow.run_id,
                    run_id.root,
                ),
                "commits": _count(session, CommitRow, CommitRow.project_id, project_id.root),
                "model_calls": _count(
                    session,
                    ModelCallLedgerRow,
                    ModelCallLedgerRow.run_id,
                    run_id.root,
                ),
            }
    finally:
        engine.dispose()


def _chain_task_kinds(tasks: tuple[TaskRecord, ...]) -> tuple[str, ...]:
    def status_for(kind: TaskKind, chapter: int) -> str:
        return next(
            (
                task.status.value
                for task in tasks
                if task.kind is kind and task.chapter_index == chapter
            ),
            "missing",
        )

    return tuple(
        f"{kind.value}@C{chapter}:{status_for(kind, chapter)}" for kind, chapter in REQUIRED_CHAIN
    )


def _chain_is_complete(tasks: tuple[TaskRecord, ...]) -> bool:
    return all(
        any(
            task.kind is kind
            and task.chapter_index == chapter
            and task.status is TaskStatus.SUCCEEDED
            and not task.superseded
            for task in tasks
        )
        for kind, chapter in REQUIRED_CHAIN
    )


def _task_belongs_to_run(
    task_id: str,
    run_id: RunId,
    expected_task_ids: set[str],
) -> bool:
    """Accept projection tasks and durable internal owners such as ``run.settlement``."""

    return task_id in expected_task_ids or task_id.startswith(f"{run_id.root}.")


def _make_project_result(
    *,
    database_url: str,
    request: CreativeRunRequest,
    report: Stage5VerticalRunReport,
    assembly: ProductionRuntimeAssembly,
    output_root: Path,
) -> U6DProjectSmokeResult:
    tasks = tuple(report.tasks)
    counts = _database_counts(database_url, project_id=request.project_id, run_id=request.run_id)
    chain_complete = _chain_is_complete(tasks)
    status: Literal["PASS", "REVIEW_REQUIRED"] = (
        "PASS"
        if report.status is VerticalRunStatus.YIELDED and chain_complete
        else "REVIEW_REQUIRED"
    )
    payload = {
        "schema": "u6d-project-smoke-evidence.v1",
        "project_id": request.project_id.root,
        "run_id": request.run_id.root,
        "basis_commit": request.basis_commit.root,
        "final_commit": report.final_commit.root,
        "vertical_status": report.status.value,
        "completed_chapters": list(report.completed_chapters),
        "chain_task_kinds": list(_chain_task_kinds(tasks)),
        "counts": counts,
    }
    artifacts = assembly.artifacts
    if artifacts is None:
        raise RuntimeError("U6-D production assembly has no artifact repository")
    evidence_ref = artifacts.put(canonical_json_bytes(payload), EVIDENCE_MEDIA_TYPE, SCHEMA_VERSION)
    return U6DProjectSmokeResult(
        project_id=request.project_id,
        run_id=request.run_id,
        basis_commit=request.basis_commit,
        final_commit=report.final_commit,
        status=status,
        completed_chapters=tuple(report.completed_chapters),
        chain_task_kinds=_chain_task_kinds(tasks),
        event_count=counts["events"],
        task_count=counts["tasks"],
        attempt_count=counts["attempts"],
        effect_count=counts["effects"],
        commit_count=counts["commits"],
        model_call_count=counts["model_calls"],
        object_store_root=str(output_root.resolve()),
        evidence_artifact_refs=(evidence_ref,),
        notes=(
            f"vertical_status={report.status.value}",
            "natural-boundary-stop=after-C21",
        ),
    )


def _cross_project_integrity_counts(
    database_url: str,
    projects: tuple[tuple[ProjectId, RunId], tuple[ProjectId, RunId]],
) -> tuple[int, int]:
    engine = build_engine(database_url)
    try:
        factory = build_session_factory(engine)
        expected_task_ids: dict[str, set[str]] = {}
        leakage = 0
        duplicate_effect_count = 0
        with factory() as session:
            for project_id, run_id in projects:
                task_rows = tuple(
                    session.scalars(
                        select(RuntimeTaskProjectionRow).where(
                            RuntimeTaskProjectionRow.run_id == run_id.root
                        )
                    )
                )
                expected_task_ids[run_id.root] = {row.task_id for row in task_rows}
                leakage += sum(row.project_id != project_id.root for row in task_rows)
                events = tuple(
                    session.scalars(select(RunEventRow).where(RunEventRow.run_id == run_id.root))
                )
                leakage += sum(
                    event.task_id is not None
                    and event.task_id not in expected_task_ids[run_id.root]
                    for event in events
                )
                effects = tuple(
                    session.scalars(
                        select(RuntimeEffectProjectionRow).where(
                            RuntimeEffectProjectionRow.run_id == run_id.root
                        )
                    )
                )
                leakage += sum(
                    effect.task_id not in expected_task_ids[run_id.root] for effect in effects
                )
                calls = tuple(
                    session.scalars(
                        select(ModelCallLedgerRow).where(ModelCallLedgerRow.run_id == run_id.root)
                    )
                )
                leakage += sum(
                    not _task_belongs_to_run(
                        call.task_id,
                        run_id,
                        expected_task_ids[run_id.root],
                    )
                    for call in calls
                )
            all_effects = tuple(session.scalars(select(RuntimeEffectProjectionRow)))
            effect_ids = [effect.effect_identity for effect in all_effects]
            duplicate_effect_count = len(effect_ids) - len(set(effect_ids))
        return leakage, duplicate_effect_count
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-one", type=Path, required=True)
    parser.add_argument("--request-two", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--object-root-one", type=Path, required=True)
    parser.add_argument("--object-root-two", type=Path, required=True)
    parser.add_argument("--endpoint-profile", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--max-slices", type=int, default=200)
    parser.add_argument("--stop-after-chapter", type=int, default=21)
    args = parser.parse_args()
    if args.output_root.exists():
        raise RuntimeError(f"U6-D refuses to overwrite output root: {args.output_root}")

    request_one = CreativeRunRequest.model_validate_json(args.request_one.read_bytes(), strict=True)
    request_two = CreativeRunRequest.model_validate_json(args.request_two.read_bytes(), strict=True)
    if request_one.project_id == request_two.project_id or request_one.run_id == request_two.run_id:
        raise RuntimeError("U6-D requests must use two distinct project/run identities")
    if request_one.current_chapter != request_two.current_chapter:
        raise RuntimeError("U6-D projects must start from the same current chapter")
    endpoints = resolve_registered_model_endpoints(args.endpoint_profile)
    admission = ModelRequestAdmissionController(
        endpoint_request_limit=2,
    )
    coordinator = ProductionDispatchCoordinator(
        database_url=args.database_url,
        manifest=load_stage5_manifest(args.manifest),
        runs=(
            ProductionRunDescriptor(
                project_id=request_one.project_id,
                run_id=request_one.run_id,
                object_store_root=args.object_root_one,
                policy=request_one.policy,
                max_tasks=1,
                max_slices=args.max_slices,
                request=request_one,
                stop_after_chapter=args.stop_after_chapter,
            ),
            ProductionRunDescriptor(
                project_id=request_two.project_id,
                run_id=request_two.run_id,
                object_store_root=args.object_root_two,
                policy=request_two.policy,
                max_tasks=1,
                max_slices=args.max_slices,
                request=request_two,
                stop_after_chapter=args.stop_after_chapter,
            ),
        ),
        model_endpoints=endpoints,
        project_parallelism=2,
        admission=admission,
    )
    dispatch_result = asyncio.run(coordinator.run_once())
    assemblies = coordinator.assemblies
    assembly_one = assemblies[(request_one.project_id, request_one.run_id)]
    assembly_two = assemblies[(request_two.project_id, request_two.run_id)]
    project_reports = {
        (item.project_id, item.run_id): item.report for item in dispatch_result.per_project
    }
    report_one = project_reports[(request_one.project_id, request_one.run_id)]
    report_two = project_reports[(request_two.project_id, request_two.run_id)]
    if report_one is None or report_two is None:
        raise RuntimeError("U6-D coordinator did not return both project reports")
    if assembly_one.attestation is None or assembly_two.attestation is None:
        raise RuntimeError("U6-D assemblies must expose startup attestations")
    attestation_one = assembly_one.attestation.model_dump(mode="json")
    attestation_two = assembly_two.attestation.model_dump(mode="json")
    for payload in (attestation_one, attestation_two):
        for field in PER_PROJECT_ATTESTATION_FIELDS:
            payload.pop(field, None)
    shared_composition_verified = attestation_one == attestation_two

    result_one = _make_project_result(
        database_url=args.database_url,
        request=request_one,
        report=report_one,
        assembly=assembly_one,
        output_root=args.object_root_one,
    )
    result_two = _make_project_result(
        database_url=args.database_url,
        request=request_two,
        report=report_two,
        assembly=assembly_two,
        output_root=args.object_root_two,
    )

    before_stop = _project_snapshot(
        args.database_url, project_id=request_two.project_id, run_id=request_two.run_id
    )
    commands = getattr(assembly_one.runtime, "_commands", None)
    if not isinstance(commands, RuntimeCommandService):
        raise RuntimeError("U6-D could not access the existing runtime command owner")
    next_successor = next(
        (
            task
            for task in _project_snapshot(
                args.database_url, project_id=request_one.project_id, run_id=request_one.run_id
            )
            if task.status is TaskStatus.READY and task.current_attempt_id is None
        ),
        None,
    )
    if next_successor is None:
        raise RuntimeError("U6-D expected a next ready successor task at the C21 boundary")
    paused = commands.control(
        next_successor.task_id,
        command_id=StableId(f"u6d-pause.{request_one.run_id.root}"[:128]),
        action="pause",
        actor_id="u6d-two-project-smoke",
        reason="stop project one at the natural C21 boundary",
        observed_revision=next_successor.task_revision,
    )
    after_stop = _project_snapshot(
        args.database_url, project_id=request_two.project_id, run_id=request_two.run_id
    )
    other_project_unaffected = before_stop == after_stop
    leakage, duplicate_effect_count = _cross_project_integrity_counts(
        args.database_url,
        (
            (request_one.project_id, request_one.run_id),
            (request_two.project_id, request_two.run_id),
        ),
    )
    snapshot = admission.snapshot()

    def snapshot_int(key: str) -> int:
        value = snapshot.get(key)
        if not isinstance(value, int):
            raise RuntimeError(f"U6-D admission snapshot field is not an integer: {key}")
        return value

    admission_evidence = U6DAdmissionEvidence(
        endpoint_request_limit=snapshot_int("endpoint_request_limit"),
        acquired_requests=snapshot_int("acquired_requests"),
        released_requests=snapshot_int("released_requests"),
        max_inflight_requests=snapshot_int("max_inflight_requests"),
        inflight_requests_after_run=snapshot_int("inflight_requests"),
        endpoint_admission_shared=True,
    )
    report = U6DTwoProjectSmokeReport(
        experiment_id=args.experiment_id,
        database_descriptor=_database_descriptor(args.database_url),
        status="PASS"
        if result_one.status == "PASS"
        and result_two.status == "PASS"
        and other_project_unaffected
        and shared_composition_verified
        and leakage == 0
        and duplicate_effect_count == 0
        else "REVIEW_REQUIRED",
        projects=(result_one, result_two),
        admission=admission_evidence,
        worker_stop_project_id=request_one.project_id,
        worker_stop_status=(f"natural-boundary:{next_successor.kind.value}:{paused.status.value}"),
        other_project_unaffected=other_project_unaffected,
        shared_composition_verified=shared_composition_verified,
        cross_project_leakage_count=leakage,
        duplicate_effect_count=duplicate_effect_count,
    )
    _write_once(
        args.output_root / "project-one.vertical-run-report.json",
        report_one.model_dump(mode="json"),
    )
    _write_once(
        args.output_root / "project-two.vertical-run-report.json",
        report_two.model_dump(mode="json"),
    )
    _write_once(
        args.output_root / "u6d-two-project-smoke-report.json",
        report.model_dump(mode="json"),
    )
    print(report.model_dump_json(indent=2))
    return 0 if report.status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
