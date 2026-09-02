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
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.creative_runtime import CreativeRunPolicy
from novel_agent.domain.ids import CommitId, ProjectId
from novel_agent.domain.memory import DerivedBuildStatus, DerivedSnapshotLite
from novel_agent.domain.runtime import FailureClass
from novel_agent.ports.model_endpoint import ModelEndpointError
from novel_agent.runtime.creative_assembly import DEFAULT_PRODUCTION_ASSEMBLY_FACTORY
from novel_agent.runtime.production_bootstrap import resolve_registered_model_endpoints


def _run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def _resource_blocked(error: BaseException) -> int:
    print(
        json.dumps(
            {"status": "RESOURCE_BLOCKED", "reason": str(error)},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 2


def _write_json_once(path: Path, payload: object) -> None:
    if path.exists():
        raise RuntimeError(f"runtime CLI refuses to overwrite receipt: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_artifact_refs(path: Path | None) -> tuple[ArtifactRef, ...]:
    """Load a JSON list of already-issued refs for an operator command."""

    if path is None:
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"artifact refs file is not valid JSON: {path}") from error
    if not isinstance(payload, list):
        raise ValueError("artifact refs file must contain a JSON list")
    try:
        return tuple(ArtifactRef.model_validate(item, strict=True) for item in payload)
    except ValueError as error:
        raise ValueError("artifact refs file contains an invalid ArtifactRef") from error


def _add_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--runtime-parallelism", type=int, choices=(1, 2))
    lookahead = parser.add_mutually_exclusive_group()
    lookahead.add_argument("--planner-lookahead", dest="planner_lookahead", action="store_true")
    lookahead.add_argument("--no-planner-lookahead", dest="planner_lookahead", action="store_false")
    parser.set_defaults(planner_lookahead=None)
    parser.add_argument("--endpoint-request-limit", type=int, choices=(1, 2), default=1)
    parser.add_argument("--kv-token-budget", type=int)
    parser.add_argument("--scheduling-timeout-seconds", type=float, default=120.0)
    parser.add_argument(
        "--retrieval-backend-profile",
        choices=("memory", "real_hybrid"),
        default="memory",
    )
    parser.add_argument("--opensearch-url")
    parser.add_argument("--embedding-url")
    parser.add_argument("--reranker-url")


def _policy_with_runtime_options(
    policy: CreativeRunPolicy,
    *,
    runtime_parallelism: int | None,
    planner_lookahead: bool | None,
) -> CreativeRunPolicy:
    updates: dict[str, object] = {}
    if runtime_parallelism is not None:
        updates["runtime_parallelism"] = runtime_parallelism
    if planner_lookahead is not None:
        updates["enable_planner_lookahead"] = planner_lookahead
    if not updates:
        return policy
    return CreativeRunPolicy.model_validate(
        {**policy.model_dump(mode="json"), **updates}, strict=False
    )


def _admission_receipt(assembly: object) -> dict[str, object] | None:
    gateway = getattr(assembly, "model_gateway", None)
    controller = getattr(gateway, "admission_controller", None)
    if controller is None or not callable(getattr(controller, "snapshot", None)):
        return None
    snapshot = controller.snapshot()
    keys = (
        "endpoint_request_limit",
        "configured_kv_token_budget",
        "effective_kv_token_budget",
        "kv_safety_reserve_ratio",
        "default_scheduling_timeout_seconds",
        "queue_depth",
        "max_inflight_requests",
        "total_wait_seconds",
        "scheduling_timeouts",
        "acquired_requests",
        "released_requests",
    )
    return {key: snapshot[key] for key in keys if key in snapshot}


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
    advance.add_argument(
        "--endpoint-profile",
        help="explicit registered endpoint profile; omit to fail closed without an endpoint",
    )
    advance.add_argument(
        "--assembly-factory",
        default=DEFAULT_PRODUCTION_ASSEMBLY_FACTORY,
    )
    advance.add_argument("--max-tasks", type=int, required=True)
    advance.add_argument("--receipt", type=Path)
    _add_runtime_options(advance)
    dispatch = runtime_commands.add_parser("dispatch")
    dispatch.add_argument("--runs", type=Path, required=True)
    dispatch.add_argument("--manifest", type=Path, required=True)
    dispatch.add_argument("--endpoint-profile")
    dispatch.add_argument("--assembly-factory", default=DEFAULT_PRODUCTION_ASSEMBLY_FACTORY)
    dispatch.add_argument("--project-parallelism", type=int, default=1)
    dispatch.add_argument("--max-total-tasks", type=int, default=100)
    dispatch.add_argument("--poll-interval-seconds", type=float, default=5.0)
    dispatch.add_argument("--receipt", type=Path)
    mode = dispatch.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--watch", action="store_true")
    _add_runtime_options(dispatch)
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
    extend_budget.add_argument("--additional-planner-memory-tranches", type=int, default=0)
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
        "--terminal-status", choices=("waiting_retry", "blocked", "cancelled"), required=True
    )
    reconcile_attempt.add_argument(
        "--failure-class", choices=tuple(item.value for item in FailureClass)
    )
    reconcile_attempt.add_argument(
        "--artifact-refs",
        type=Path,
        help="JSON list of existing ArtifactRef objects to attach to the settlement",
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
    prepare = runtime_commands.add_parser("bootstrap-prepare")
    prepare.add_argument("--brief", type=Path, required=True)
    prepare.add_argument("--project-id", required=True)
    prepare.add_argument("--object-store-root", type=Path, required=True)
    prepare.add_argument("--endpoint-profile", required=True)
    prepare.add_argument("--prepared", type=Path, required=True)
    prepare.add_argument("--preview", type=Path)
    prepare.add_argument("--run-id", required=True)
    commit = runtime_commands.add_parser("bootstrap-commit")
    commit.add_argument("--prepared", type=Path, required=True)
    commit.add_argument("--author-id", required=True)
    commit.add_argument("--reason", required=True)
    commit.add_argument("--target-chapters", type=int, required=True)
    commit.add_argument("--object-store-root", type=Path, required=True)
    commit.add_argument("--run-id", required=True)
    commit.add_argument("--policy", type=Path, required=True)
    commit.add_argument("--request", type=Path, required=True)
    commit.add_argument("--runs", type=Path, required=True)
    _add_runtime_options(commit)
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
        from novel_agent.adapters.postgres.model_call_ledger import SqlModelCallLedger
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
        if args.runtime_command == "bootstrap-prepare":
            from novel_agent.runtime.production_novel_bootstrap import ProductionNovelBootstrap

            brief_text = args.brief.read_text(encoding="utf-8")
            artifacts = ArtifactRepository(FilesystemObjectStore(args.object_store_root))
            project_id = ProjectId(args.project_id)
            run_id = RunId(args.run_id)
            endpoints = resolve_registered_model_endpoints(args.endpoint_profile)
            if not endpoints:
                return _resource_blocked(RuntimeError("bootstrap prepare requires an endpoint"))
            prepared = _run_async(
                ProductionNovelBootstrap(
                    artifacts=artifacts,
                    session_factory=factory,
                    endpoints=endpoints,
                    run_id=run_id,
                ).prepare(project_id=project_id, brief_text=brief_text)
            )
            approval = prepared.document.approval_request
            _write_json_once(
                args.prepared,
                {
                    "artifact": prepared.artifact.model_dump(mode="json"),
                    "preview": prepared.document.preview,
                    "approval_request": (
                        None if approval is None else approval.model_dump(mode="json")
                    ),
                    "validation_status": prepared.document.validation.status.value,
                },
            )
            if args.preview is not None:
                _write_json_once(args.preview, prepared.document.preview)
            print(json.dumps(prepared.document.preview, ensure_ascii=False, sort_keys=True))
            return 0
        if args.runtime_command == "bootstrap-commit":
            from novel_agent.domain.artifacts import ArtifactRef
            from novel_agent.domain.ids import ProjectId, RunId, StableId
            from novel_agent.runtime.production_novel_bootstrap import (
                ProductionNovelBootstrap,
                load_prepared_bootstrap,
            )

            artifacts = ArtifactRepository(FilesystemObjectStore(args.object_store_root))
            payload = json.loads(args.prepared.read_text(encoding="utf-8"))
            reference = ArtifactRef.model_validate(payload["artifact"], strict=True)
            document = load_prepared_bootstrap(artifacts, reference)
            policy, request, descriptor = ProductionNovelBootstrap(
                artifacts=artifacts,
                session_factory=factory,
            ).commit(
                prepared=document,
                author_id=StableId(args.author_id),
                reason=args.reason,
                target_chapters=args.target_chapters,
                run_id=RunId(args.run_id),
                object_store_root=args.object_store_root,
            )
            if args.retrieval_backend_profile == "real_hybrid":
                from novel_agent.runtime.real_hybrid import assemble_production_real_hybrid
                from novel_agent.services.commits import CommitService

                assemble_production_real_hybrid(
                    session_factory=factory,
                    commits=CommitService(factory),
                    artifacts=artifacts,
                    project_id=request.project_id,
                    run_id=request.run_id,
                    opensearch_url=args.opensearch_url or "",
                    embedding_url=args.embedding_url or "",
                    reranker_url=args.reranker_url or "",
                )
            _write_json_once(args.policy, policy.model_dump(mode="json"))
            _write_json_once(args.request, request.model_dump(mode="json"))
            _write_json_once(
                args.runs,
                [
                    {
                        "project_id": descriptor.project_id.root,
                        "run_id": descriptor.run_id.root,
                        "object_store_root": str(descriptor.object_store_root),
                        "policy": str(args.policy),
                        "request": str(args.request),
                        "stop_after_chapter": descriptor.stop_after_chapter,
                    }
                ],
            )
            print(
                json.dumps(
                    {
                        "project_id": request.project_id.root,
                        "run_id": request.run_id.root,
                        "basis_commit": request.basis_commit.root,
                        "basis_snapshot": (
                            None if request.basis_snapshot is None else request.basis_snapshot.root
                        ),
                        "target_chapters": request.target_chapters,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.runtime_command == "dispatch":
            from novel_agent.domain.stage5_manifest import load_stage5_manifest
            from novel_agent.runtime.production_dispatch_coordinator import (
                ProductionDispatchCoordinator,
                load_production_run_descriptors,
            )

            manifest = load_stage5_manifest(args.manifest)
            descriptors = tuple(
                descriptor.with_runtime_options(
                    runtime_parallelism=args.runtime_parallelism,
                    planner_lookahead=args.planner_lookahead,
                )
                for descriptor in load_production_run_descriptors(args.runs)
            )
            coordinator = ProductionDispatchCoordinator(
                database_url=args.database_url,
                manifest=manifest,
                runs=descriptors,
                model_endpoints=resolve_registered_model_endpoints(args.endpoint_profile),
                assembly_factory=args.assembly_factory,
                project_parallelism=args.project_parallelism,
                endpoint_request_limit=args.endpoint_request_limit,
                kv_token_budget=args.kv_token_budget,
                scheduling_timeout_seconds=args.scheduling_timeout_seconds,
                max_total_tasks=args.max_total_tasks,
                retrieval_backend_profile=args.retrieval_backend_profile,
                opensearch_url=args.opensearch_url,
                embedding_url=args.embedding_url,
                reranker_url=args.reranker_url,
            )
            try:
                result = _run_async(
                    coordinator.run_watch(poll_interval_seconds=args.poll_interval_seconds)
                    if args.watch
                    else coordinator.run_once()
                )
            except (ModelEndpointError, ConnectionError, TimeoutError, OSError) as error:
                return _resource_blocked(error)
            output = result.to_payload()
            if args.receipt is not None:
                _write_json_once(
                    args.receipt,
                    {
                        "receipt_type": "runtime_cli_dispatch",
                        **output,
                    },
                )
            print(json.dumps(output, ensure_ascii=False, sort_keys=True))
            return 2 if result.status in {"failed", "blocked"} else 0
        if args.runtime_command == "advance":
            from novel_agent.domain.stage5_manifest import load_stage5_manifest
            from novel_agent.runtime.creative_assembly import (
                ProductionAssemblyContext,
                load_production_runtime_assembly,
            )

            policy = CreativeRunPolicy.model_validate_json(args.policy.read_bytes())
            policy = _policy_with_runtime_options(
                policy,
                runtime_parallelism=args.runtime_parallelism,
                planner_lookahead=args.planner_lookahead,
            )
            manifest = load_stage5_manifest(args.manifest)
            try:
                assembly = load_production_runtime_assembly(
                    args.assembly_factory,
                    ProductionAssemblyContext(
                        database_url=args.database_url,
                        object_store_root=args.object_store_root,
                        project_id=ProjectId(args.project_id),
                        run_id=RunId(args.run_id),
                        policy=policy,
                        manifest=manifest,
                        model_endpoints=resolve_registered_model_endpoints(args.endpoint_profile),
                        endpoint_request_limit=args.endpoint_request_limit,
                        kv_token_budget=args.kv_token_budget,
                        scheduling_timeout_seconds=args.scheduling_timeout_seconds,
                        retrieval_backend_profile=args.retrieval_backend_profile,
                        opensearch_url=args.opensearch_url,
                        embedding_url=args.embedding_url,
                        reranker_url=args.reranker_url,
                    ),
                )
            except RuntimeError as error:
                if "requires registered model endpoints" in str(error):
                    return _resource_blocked(error)
                raise
            try:
                results = _run_async(assembly.dispatcher.run_bounded(max_tasks=args.max_tasks))
            except (ModelEndpointError, ConnectionError, TimeoutError, OSError) as error:
                return _resource_blocked(error)
            output = {
                "progressed": len(results),
                "results": [item.model_dump(mode="json") for item in results],
            }
            admission = _admission_receipt(assembly)
            if admission is not None:
                output["admission"] = admission
            if args.receipt is not None:
                if assembly.attestation is None:
                    raise RuntimeError("production assembly did not provide a CLI attestation")
                _write_json_once(
                    args.receipt,
                    {
                        "receipt_type": "runtime_cli_advance",
                        "status": "succeeded",
                        "assembly_factory": args.assembly_factory,
                        "endpoint_profile": args.endpoint_profile,
                        "spec_locator": assembly.attestation.factory_locator,
                        "session_factory_identity": assembly.attestation.session_factory_identity,
                        "model_gateway": assembly.attestation.model_gateway,
                        "endpoints": [
                            item.model_dump(mode="json") for item in assembly.attestation.endpoints
                        ],
                        **output,
                    },
                )
            print(json.dumps(output, sort_keys=True))
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
            report = RuntimeReportService(
                factory,
                events,
                SqlModelCallLedger(factory),
            ).export(
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
                failure_class=(
                    None if args.failure_class is None else FailureClass(args.failure_class)
                ),
                observed_revision=args.observed_revision,
                artifact_refs=_load_artifact_refs(args.artifact_refs),
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
                additional_planner_memory_tranches=(args.additional_planner_memory_tranches),
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
