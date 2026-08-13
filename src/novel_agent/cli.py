"""Small deterministic command-line entry point for repository diagnostics."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
from collections.abc import Coroutine, Sequence
from pathlib import Path
from typing import Any

from novel_agent.config import AppSettings
from novel_agent.domain.ids import CommitId, ProjectId
from novel_agent.domain.memory import DerivedBuildStatus, DerivedSnapshotLite


def _run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


class _ProjectionBuilder:
    def build(self, project_id: ProjectId, source_commit: CommitId) -> DerivedSnapshotLite:
        from datetime import UTC, datetime

        from novel_agent.domain.ids import StableId

        suffix = source_commit.root.removeprefix("sha256:")
        return DerivedSnapshotLite(
            snapshot_id=StableId(f"snapshot.{suffix}"),
            source_commit=source_commit,
            anchor_build_id=StableId(f"anchor.{suffix[:24]}"),
            anchor_index_version="anchor-v1",
            grounded_index_version="grounded-v1",
            embedding_profile="offline-v1",
            fusion_profile="rrf-v1",
            build_status=DerivedBuildStatus.EXACT,
            published_at=datetime.now(UTC),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-agent")
    subparsers = parser.add_subparsers(dest="top_command", required=True)
    subparsers.add_parser("doctor", help="print non-secret bootstrap diagnostics")
    runtime = subparsers.add_parser("runtime", help="operate the Stage 5 durable runtime")
    runtime.add_argument("--database-url", required=True)
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)
    start = runtime_commands.add_parser("start")
    start.add_argument("--request", type=Path, required=True)
    status = runtime_commands.add_parser("status")
    status.add_argument("--run-id", required=True)
    advance = runtime_commands.add_parser("advance")
    advance.add_argument("--project-id", required=True)
    advance.add_argument("--run-id", required=True)
    advance.add_argument("--policy", type=Path, required=True)
    advance.add_argument("--manifest", type=Path, required=True)
    advance.add_argument("--object-store-root", type=Path, required=True)
    advance.add_argument("--assembly-factory", required=True)
    advance.add_argument("--max-tasks", type=int, required=True)
    for action in ("pause", "resume", "cancel", "retry"):
        control = runtime_commands.add_parser(action)
        control.add_argument("--project-id", required=True)
        control.add_argument("--run-id", required=True)
        control.add_argument("--task-id", required=True)
        control.add_argument("--observed-revision", type=int, required=True)
        control.add_argument("--command-id", required=True)
        control.add_argument("--actor-id", required=True)
        control.add_argument("--reason", required=True)
    extend_budget = runtime_commands.add_parser("extend-budget")
    extend_budget.add_argument("--project-id", required=True)
    extend_budget.add_argument("--run-id", required=True)
    extend_budget.add_argument("--task-id", required=True)
    extend_budget.add_argument("--observed-revision", type=int, required=True)
    extend_budget.add_argument("--command-id", required=True)
    extend_budget.add_argument("--actor-id", required=True)
    extend_budget.add_argument("--reason", required=True)
    extend_budget.add_argument("--additional-attempts", type=int, default=0)
    extend_budget.add_argument(
        "--additional-planner-memory-tranches", type=int, default=0
    )
    reconcile = runtime_commands.add_parser("reconcile-effect")
    reconcile.add_argument("--project-id", required=True)
    reconcile.add_argument("--run-id", required=True)
    reconcile.add_argument("--task-id", required=True)
    reconcile.add_argument("--observed-revision", type=int, required=True)
    reconcile.add_argument("--command-id", required=True)
    reconcile.add_argument("--receipt", type=Path, required=True)
    reconcile_attempt = runtime_commands.add_parser("reconcile")
    reconcile_attempt.add_argument("--project-id", required=True)
    reconcile_attempt.add_argument("--run-id", required=True)
    reconcile_attempt.add_argument("--task-id", required=True)
    reconcile_attempt.add_argument("--observed-revision", type=int, required=True)
    reconcile_attempt.add_argument("--command-id", required=True)
    reconcile_attempt.add_argument("--actor-id", required=True)
    reconcile_attempt.add_argument("--reason", required=True)
    reconcile_attempt.add_argument(
        "--terminal-status", choices=("waiting_retry", "cancelled"), required=True
    )
    unblock = runtime_commands.add_parser("unblock")
    unblock.add_argument("--project-id", required=True)
    unblock.add_argument("--run-id", required=True)
    unblock.add_argument("--observed-revision", type=int, required=True)
    unblock.add_argument("--command", type=Path, required=True)
    for action in ("accept-plan", "reject-plan", "accept-draft", "reject-draft"):
        acceptance = runtime_commands.add_parser(action)
        acceptance.add_argument("--command", type=Path, required=True)
        acceptance.add_argument("--policy", type=Path, required=True)
        acceptance.add_argument("--object-store-root", type=Path, required=True)
    maintenance = runtime_commands.add_parser("maintenance")
    maintenance.add_argument("--command", type=Path, required=True)
    report = runtime_commands.add_parser("export-report")
    report.add_argument("--run-id", required=True)
    report.add_argument("--manifest", type=Path, required=True)
    report.add_argument("--executable-commit", required=True)
    report.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.top_command == "doctor":
        settings = AppSettings()
        print(
            json.dumps(
                {
                    "environment": settings.environment,
                    "log_level": settings.log_level,
                    "python": platform.python_version(),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.top_command == "runtime":
        from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
        from novel_agent.adapters.postgres.database import build_engine, build_session_factory
        from novel_agent.adapters.postgres.runtime import RuntimeTaskQueryRepository
        from novel_agent.domain.creative_runtime import (
            AcceptanceCommand,
            AcceptanceDecision,
            CandidateKind,
            CreativeRunPolicy,
            CreativeRunRequest,
            UnblockCommand,
            commit_task_from_acceptance,
        )
        from novel_agent.domain.ids import ProjectId, RunId, StableId, TaskId
        from novel_agent.domain.runtime import EffectReceipt, TaskStatus
        from novel_agent.services.artifacts import ArtifactRepository
        from novel_agent.services.commits import CommitService
        from novel_agent.services.event_log import RunEventLogRepository
        from novel_agent.services.runtime_acceptance import RuntimeAcceptanceService
        from novel_agent.services.runtime_commands import RuntimeCommandService
        from novel_agent.services.runtime_maintenance import (
            MaintenanceCommand,
            RuntimeMaintenanceService,
        )
        from novel_agent.services.runtime_reporting import RuntimeReportService

        factory = build_session_factory(build_engine(args.database_url))
        events = RunEventLogRepository(factory)
        commands = RuntimeCommandService(
            factory,
            events,
            permission_hash_resolver=lambda _project_id: (_ for _ in ()).throw(
                RuntimeError("runtime CLI does not claim work; dispatcher must inject permissions")
            ),
        )
        if args.runtime_command == "start":
            request = CreativeRunRequest.model_validate_json(args.request.read_bytes())
            task = commands.create_run_and_initial_task(request)
            print(task.model_dump_json())
            return 0
        if args.runtime_command == "status":
            tasks = RuntimeTaskQueryRepository(factory).list_run(RunId(args.run_id))
            print(json.dumps([item.model_dump(mode="json") for item in tasks], sort_keys=True))
            return 0
        if args.runtime_command == "advance":
            from novel_agent.domain.stage5_manifest import load_stage5_manifest
            from novel_agent.runtime.creative_assembly import (
                ProductionAssemblyContext,
                load_production_runtime_assembly,
            )

            policy = CreativeRunPolicy.model_validate_json(args.policy.read_bytes())
            manifest = load_stage5_manifest(args.manifest)
            assembly = load_production_runtime_assembly(
                args.assembly_factory,
                ProductionAssemblyContext(
                    database_url=args.database_url,
                    object_store_root=args.object_store_root,
                    project_id=ProjectId(args.project_id),
                    run_id=RunId(args.run_id),
                    policy=policy,
                    manifest=manifest,
                ),
            )
            results = _run_async(
                assembly.dispatcher.run_bounded(max_tasks=args.max_tasks)
            )
            if not results:
                print(json.dumps({"progressed": 0, "results": []}, sort_keys=True))
                return 0
            print(
                json.dumps(
                    {
                        "progressed": len(results),
                        "results": [item.model_dump(mode="json") for item in results],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.runtime_command in {
            "accept-plan",
            "reject-plan",
            "accept-draft",
            "reject-draft",
        }:
            acceptance_command = AcceptanceCommand.model_validate_json(args.command.read_bytes())
            policy = CreativeRunPolicy.model_validate_json(args.policy.read_bytes())
            expected_kind = (
                CandidateKind.PLAN if args.runtime_command.endswith("plan") else CandidateKind.DRAFT
            )
            expected_decision = (
                AcceptanceDecision.ACCEPT
                if args.runtime_command.startswith("accept")
                else AcceptanceDecision.REJECT
            )
            if (
                acceptance_command.candidate.kind is not expected_kind
                or acceptance_command.decision is not expected_decision
            ):
                raise ValueError("acceptance command does not match the selected CLI operation")
            artifacts = ArtifactRepository(FilesystemObjectStore(args.object_store_root))
            acceptance_receipt = RuntimeAcceptanceService(
                commands, CommitService(factory), artifacts
            ).submit(acceptance_command, policy=policy)
            acceptance_task = commands.get_task(acceptance_command.task_id)
            successor = None
            if acceptance_receipt.accepted_binding is not None:
                successor = commands.create_task(
                    commit_task_from_acceptance(acceptance_task, acceptance_receipt)
                )
            print(
                json.dumps(
                    {
                        "receipt": acceptance_receipt.model_dump(mode="json"),
                        "successor": (
                            None if successor is None else successor.model_dump(mode="json")
                        ),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.runtime_command == "maintenance":
            maintenance_command = MaintenanceCommand.model_validate_json(args.command.read_bytes())
            print(
                RuntimeMaintenanceService(factory).precheck(maintenance_command).model_dump_json()
            )
            return 0
        if args.runtime_command == "export-report":
            report = RuntimeReportService(factory, events).export(
                RunId(args.run_id),
                manifest_path=args.manifest,
                executable_commit=args.executable_commit,
            )
            args.output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
            print(report.model_dump_json())
            return 0
        if args.runtime_command == "reconcile-effect":
            effect_task = commands.get_task(TaskId(args.task_id))
            if effect_task.project_id != ProjectId(args.project_id) or effect_task.run_id != RunId(
                args.run_id
            ):
                raise ValueError("explicit project/run/task identity mismatch")
            effect_receipt = EffectReceipt.model_validate_json(args.receipt.read_bytes())
            effect_result = commands.reconcile_effect(
                TaskId(args.task_id),
                effect_receipt,
                command_id=StableId(args.command_id),
                observed_revision=args.observed_revision,
            )
            print(effect_result.model_dump_json())
            return 0
        if args.runtime_command == "reconcile":
            reconcile_task = commands.get_task(TaskId(args.task_id))
            if reconcile_task.project_id != ProjectId(
                args.project_id
            ) or reconcile_task.run_id != RunId(args.run_id):
                raise ValueError("explicit project/run/task identity mismatch")
            reconcile_result = commands.operator_reconcile_attempt(
                reconcile_task.task_id,
                command_id=StableId(args.command_id),
                actor_id=args.actor_id,
                reason=args.reason,
                terminal_status=TaskStatus(args.terminal_status),
                observed_revision=args.observed_revision,
            )
            print(reconcile_result.model_dump_json())
            return 0
        if args.runtime_command == "unblock":
            unblock_command = UnblockCommand.model_validate_json(args.command.read_bytes())
            blocked_task = commands.get_task(unblock_command.task_id)
            if blocked_task.project_id != ProjectId(
                args.project_id
            ) or blocked_task.run_id != RunId(args.run_id):
                raise ValueError("explicit project/run/task identity mismatch")
            unblock_result = commands.unblock(
                unblock_command.task_id,
                command_id=unblock_command.command_id,
                actor_id=unblock_command.actor_id,
                block_cause_fingerprint=unblock_command.block_cause_fingerprint,
                changed_evidence_refs=unblock_command.changed_evidence_refs,
                observed_revision=args.observed_revision,
            )
            print(unblock_result.model_dump_json())
            return 0
        task_id = TaskId(args.task_id)
        command_id = StableId(args.command_id)
        observed = commands.get_task(task_id)
        if observed.project_id != ProjectId(args.project_id) or observed.run_id != RunId(
            args.run_id
        ):
            raise ValueError("explicit project/run/task identity mismatch")
        if args.runtime_command == "extend-budget":
            task = commands.extend_budget(
                task_id,
                command_id=command_id,
                actor_id=args.actor_id,
                reason=args.reason,
                additional_attempts=args.additional_attempts,
                additional_planner_memory_tranches=(
                    args.additional_planner_memory_tranches
                ),
                observed_revision=args.observed_revision,
            )
        elif args.runtime_command == "resume":
            task = commands.resume(
                task_id,
                command_id=command_id,
                actor_id=args.actor_id,
                reason=args.reason,
                observed_revision=args.observed_revision,
            )
        else:
            task = commands.control(
                task_id,
                command_id=command_id,
                action=args.runtime_command,
                actor_id=args.actor_id,
                reason=args.reason,
                observed_revision=args.observed_revision,
            )
        print(task.model_dump_json())
        return 0
    raise AssertionError(f"unhandled command: {args.top_command}")  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
