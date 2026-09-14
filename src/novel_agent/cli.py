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
from novel_agent.domain.creative_runtime import (
    CreativeRunPolicy,
    CreativeRunResult,
    CreativeRunTerminal,
)
from novel_agent.domain.ids import CommitId, ProjectId
from novel_agent.domain.memory import DerivedBuildStatus, DerivedSnapshotLite
from novel_agent.domain.runtime import FailureClass, RunEvent, TaskRecord
from novel_agent.ports.model_endpoint import ModelEndpointError
from novel_agent.runtime.creative_assembly import DEFAULT_PRODUCTION_ASSEMBLY_FACTORY
from novel_agent.runtime.production_bootstrap import resolve_registered_model_endpoints
from novel_agent.runtime.production_dispatch_coordinator import ProductionRunDescriptor
from novel_agent.runtime.production_novel_bootstrap import (
    BOOTSTRAP_MAX_OUTPUT_TOKENS,
    BOOTSTRAP_REQUEST_TIMEOUT_SECONDS,
)

# The scheduling timeout is part of the configuration fingerprint.  The
# bootstrap commit records it for the run, so a dispatch or advance that did
# not receive it explicitly has to use the same value instead of a different
# CLI default, which would fail every run closed with RUN_CONFIGURATION_CHANGED.
DEFAULT_SCHEDULING_TIMEOUT_SECONDS = 300.0


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


def _advance_outcome(results: Sequence[CreativeRunResult]) -> tuple[str, int]:
    """Map durable runtime terminals to an honest CLI status and exit code.

    ``WAITING_RETRY`` is a settled, recoverable frontier: the stage driver must
    be able to classify it before deciding whether a retry is safe.  The other
    non-progress terminals require an operator or an owning module and therefore
    must not be emitted as a successful advance.
    """

    terminals = {item.terminal for item in results}
    if CreativeRunTerminal.RECOVERY_PENDING in terminals:
        return "recovery_pending", 3
    if CreativeRunTerminal.BLOCKED in terminals:
        return "blocked", 2
    if CreativeRunTerminal.REVIEW_REQUIRED in terminals:
        return "review_required", 2
    if CreativeRunTerminal.BUDGET_REVIEW in terminals:
        return "budget_review", 2
    if CreativeRunTerminal.CANCELLED in terminals:
        return "cancelled", 2
    if CreativeRunTerminal.WAITING_RETRY in terminals:
        return "waiting_retry", 0
    if terminals & {
        CreativeRunTerminal.WAITING_PLAN_ACCEPTANCE,
        CreativeRunTerminal.WAITING_DRAFT_ACCEPTANCE,
    }:
        return "waiting_input", 0
    return "succeeded", 0


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


def _resolve_retrieval_options(
    args: argparse.Namespace,
    descriptors: tuple[ProductionRunDescriptor, ...],
) -> dict[str, str | None]:
    """Resolve the retrieval deployment, deferring to what the run committed.

    The retrieval profile and service URLs are all inputs to the configuration
    fingerprint.  A dispatch that fell back to a CLI default therefore computed a
    different fingerprint than the frozen descriptor and failed every run closed
    with RUN_CONFIGURATION_CHANGED, which reads like configuration drift rather
    than a missing flag.  An explicit flag that contradicts the committed value is
    rejected instead of silently overriding it.
    """

    committed = {d.retrieval_backend_profile for d in descriptors if d.retrieval_backend_profile}
    if len(committed) > 1:
        raise RuntimeError(
            "run descriptors were frozen against different retrieval profiles: "
            + ", ".join(sorted(committed))
        )
    committed_profile = next(iter(committed), None)
    if (
        args.retrieval_backend_profile is not None
        and committed_profile is not None
        and args.retrieval_backend_profile != committed_profile
    ):
        raise RuntimeError(
            "--retrieval-backend-profile contradicts the committed run configuration: "
            f"{args.retrieval_backend_profile} != {committed_profile}"
        )
    resolved: dict[str, str | None] = {
        "retrieval_backend_profile": (
            args.retrieval_backend_profile or committed_profile or "memory"
        )
    }
    for name in ("opensearch_url", "embedding_url", "reranker_url"):
        explicit = getattr(args, name)
        if explicit is not None:
            resolved[name] = explicit
            continue
        from_descriptor = {getattr(d, name) for d in descriptors if getattr(d, name) is not None}
        if len(from_descriptor) > 1:
            raise RuntimeError(
                f"run descriptors were frozen against different {name} values: "
                + ", ".join(sorted(from_descriptor))
            )
        resolved[name] = next(iter(from_descriptor), None)
    return resolved


def _add_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--runtime-parallelism", type=int, choices=(1, 2))
    lookahead = parser.add_mutually_exclusive_group()
    lookahead.add_argument("--planner-lookahead", dest="planner_lookahead", action="store_true")
    lookahead.add_argument("--no-planner-lookahead", dest="planner_lookahead", action="store_false")
    parser.set_defaults(planner_lookahead=None)
    parser.add_argument("--endpoint-request-limit", type=int, choices=(1, 2), default=1)
    parser.add_argument("--kv-token-budget", type=int)
    # No default here: the frozen run descriptors already carry the scheduling
    # timeout that was committed into the configuration fingerprint, and a CLI
    # default would silently disagree with it and fail the run closed.
    parser.add_argument("--scheduling-timeout-seconds", type=float)
    # No default: the retrieval deployment is part of the configuration
    # fingerprint the run was frozen against, so an unset flag must defer to the
    # committed descriptor instead of silently selecting memory.
    parser.add_argument(
        "--retrieval-backend-profile",
        choices=("memory", "real_hybrid"),
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
    preflight = subparsers.add_parser(
        "preflight-endpoint",
        help="verify one registered endpoint profile against the live service",
    )
    preflight.add_argument(
        "--endpoint-profile",
        required=True,
        help="registered profile to verify; identity drift fails the preflight",
    )
    preflight.add_argument(
        "--live-generation",
        action="store_true",
        help="also run one bounded schema-constrained generation through the adapter",
    )
    preflight.add_argument("--timeout-seconds", type=float, default=120.0)
    preflight.add_argument(
        "--embedding-url",
        help="also probe this embedding endpoint (for example http://127.0.0.1:8081/v1/embeddings)",
    )
    preflight.add_argument(
        "--reranker-url",
        help="also probe this reranker endpoint and require it to discriminate",
    )
    runtime = subparsers.add_parser("runtime", help="operate the Stage 5 durable runtime")
    runtime.add_argument("--database-url", required=True)
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)
    start = runtime_commands.add_parser("start")
    start.add_argument("--request", type=Path, required=True)
    status = runtime_commands.add_parser("status")
    status.add_argument("--run-id", required=True)
    classify = runtime_commands.add_parser(
        "classify",
        help="the canonical recovery position of one task, read from its attempt ledger",
    )
    classify.add_argument("--task-id", required=True)
    roots = runtime_commands.add_parser(
        "roots",
        help="the current committed roots and fail-closed stage evidence",
    )
    roots.add_argument("--project-id", required=True)
    roots.add_argument("--object-store-root", type=Path)
    roots.add_argument(
        "--run-id",
        help="optional run whose durable task/event stream proves acceptance and projection",
    )
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
    for action in ("pause", "resume", "cancel", "retry", "supersede"):
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
    audit = runtime_commands.add_parser("audit-memory-projection")
    audit.add_argument("--project-id", required=True)
    audit.add_argument("--commit", required=True)
    audit.add_argument("--snapshot-id", required=True)
    audit.add_argument("--object-store-root", type=Path, required=True)
    audit.add_argument("--opensearch-url")
    audit.add_argument("--output", type=Path)
    prepare = runtime_commands.add_parser("bootstrap-prepare")
    prepare.add_argument("--brief", type=Path, required=True)
    prepare.add_argument("--project-id", required=True)
    prepare.add_argument("--object-store-root", type=Path, required=True)
    prepare.add_argument("--endpoint-profile", required=True)
    prepare.add_argument("--prepared", type=Path, required=True)
    prepare.add_argument("--preview", type=Path)
    prepare.add_argument("--planning-locks", type=Path)
    prepare.add_argument(
        "--max-output-tokens",
        type=int,
        default=BOOTSTRAP_MAX_OUTPUT_TOKENS,
        help="output budget for the bootstrap Planner and Curator requests",
    )
    prepare.add_argument(
        "--bootstrap-timeout-seconds",
        type=float,
        default=BOOTSTRAP_REQUEST_TIMEOUT_SECONDS,
        help="request timeout for the bootstrap Planner and Curator calls",
    )
    prepare.add_argument("--run-id", required=True)
    commit = runtime_commands.add_parser("bootstrap-commit")
    commit.add_argument("--prepared", type=Path, required=True)
    commit.add_argument("--author-id", required=True)
    commit.add_argument("--reason", required=True)
    commit.add_argument("--target-chapters", type=int, required=True)
    commit.add_argument("--object-store-root", type=Path, required=True)
    commit.add_argument(
        "--endpoint-profile",
        required=True,
        help=(
            "the registered endpoint profile this run is frozen against; the "
            "configuration fingerprint must name the endpoints the run will use"
        ),
    )
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
    if args.top_command == "preflight-endpoint":
        from novel_agent.runtime.endpoint_preflight import (
            run_endpoint_preflight,
            run_retrieval_preflight,
        )

        result = run_endpoint_preflight(
            args.endpoint_profile,
            live_generation=bool(args.live_generation),
            generation_timeout_seconds=float(args.timeout_seconds),
        )
        payload: dict[str, object] = dict(result.as_payload())
        retrieval_ok = True
        if args.embedding_url and args.reranker_url:
            retrieval = run_retrieval_preflight(
                embedding_url=str(args.embedding_url),
                reranker_url=str(args.reranker_url),
                timeout_seconds=float(args.timeout_seconds),
            )
            payload["retrieval"] = retrieval.as_payload()
            retrieval_ok = retrieval.ok
        elif bool(args.embedding_url) != bool(args.reranker_url):
            payload["retrieval_error"] = "both --embedding-url and --reranker-url are required"
            retrieval_ok = False
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if result.ok and retrieval_ok else 2
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
        commits = CommitService(factory)
        events = RunEventLogRepository(factory)
        commands = RuntimeCommandService(
            factory,
            events,
            permission_hash_resolver=lambda _project_id: (_ for _ in ()).throw(
                RuntimeError("runtime CLI does not claim work; dispatcher must inject permissions")
            ),
        )
        if args.runtime_command == "audit-memory-projection":
            from novel_agent.domain.benchmark import TextRootDocument
            from novel_agent.services.memory_projection_audit import MemoryProjectionAuditor
            from novel_agent.services.projection import DerivedSnapshotRepository

            artifacts = ArtifactRepository(FilesystemObjectStore(args.object_store_root))
            commits = CommitService(factory)
            commit_id = CommitId(args.commit)
            manifest = commits.load_manifest(commit_id)
            text_root = TextRootDocument.model_validate_json(
                artifacts.read_verified(manifest.text_root), strict=True
            )
            index = None
            grounded_index = None
            anchor_index = None
            if args.opensearch_url:
                from opensearchpy import OpenSearch

                from novel_agent.adapters.opensearch.search_index import OpenSearchIndex
                from novel_agent.services.search_retrieval import Stage2RSearchIndexer

                index = OpenSearchIndex(OpenSearch(args.opensearch_url))
                anchor_index, grounded_index = Stage2RSearchIndexer.aliases(
                    ProjectId(args.project_id)
                )
            report = MemoryProjectionAuditor(
                session_factory=factory,
                snapshots=DerivedSnapshotRepository(factory),
                index=index,
                grounded_index=grounded_index,
                anchor_index=anchor_index,
            ).audit(
                project_id=ProjectId(args.project_id),
                commit_id=commit_id,
                snapshot_id=StableId(args.snapshot_id),
                text_root=text_root,
            )
            payload = report.model_dump(mode="json")
            if args.output is not None:
                _write_json_once(args.output, payload)
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        if args.runtime_command == "start":
            request = CreativeRunRequest.model_validate_json(args.request.read_bytes())
            task = commands.create_run_and_initial_task(request)
            print(task.model_dump_json())
            return 0
        if args.runtime_command == "status":
            tasks = RuntimeTaskQueryRepository(factory).list_run(RunId(args.run_id))
            print(json.dumps([item.model_dump(mode="json") for item in tasks], sort_keys=True))
            return 0
        if args.runtime_command == "roots":
            # A stage's exit is proven from committed artifacts, not from the
            # absence of a READY task, so the driver needs to read the canonical
            # roots rather than infer progress from the task list.
            from novel_agent.adapters.runtime.materializers import PLAN_REVIEW_MEDIA_TYPE
            from novel_agent.domain.creative_runtime import CandidateBinding
            from novel_agent.services.projection import DerivedSnapshotRepository
            from novel_agent.services.stage_exit_audit import (
                StageRuntimeEvidence,
                audit_stage_roots,
                plan_review_is_reachable,
                runtime_evidence_from_tasks,
            )

            project_id = ProjectId(args.project_id)
            current_commit = commits.current_commit(project_id)
            manifest = commits.load_manifest(current_commit)
            roots_payload: dict[str, object] = {
                "commit": current_commit.root,
                "plan_root": manifest.plan_root.artifact_id.root,
                "world_root": manifest.world_root.artifact_id.root,
                "text_root": manifest.text_root.artifact_id.root,
                "project_profile_root": manifest.project_profile_root.artifact_id.root,
                "committed_chapters": 0,
                "committed_volumes": 0,
                "g0_evidence_complete": False,
                "g1_evidence_complete": False,
                "g2_evidence_complete": False,
                "g3_evidence_complete": False,
            }
            if args.object_store_root is not None:
                from novel_agent.domain.benchmark import (
                    PlanRootDocument,
                    TextRootDocument,
                )
                from novel_agent.domain.memory import WorldRootDocument
                from novel_agent.services.artifacts import ArtifactRepository

                store = ArtifactRepository(FilesystemObjectStore(args.object_store_root))
                text_root = TextRootDocument.model_validate_json(
                    store.read_verified(manifest.text_root), strict=True
                )
                plan_root = PlanRootDocument.model_validate_json(
                    store.read_verified(manifest.plan_root), strict=True
                )
                world_root = WorldRootDocument.model_validate_json(
                    store.read_verified(manifest.world_root), strict=True
                )
                roots_payload["committed_chapters"] = len(text_root.chapters)
                roots_payload["committed_volumes"] = sum(
                    1
                    for node in plan_root.nodes
                    if node.plan_level is not None and node.plan_level.value == "arc_volume"
                )
                run_tasks: tuple[TaskRecord, ...] = ()
                run_events: tuple[RunEvent, ...] = ()
                plan_reviewed = False
                if args.run_id:
                    run_id = RunId(args.run_id)
                    run_tasks = RuntimeTaskQueryRepository(factory).list_run(run_id)
                    run_events = events.replay(run_id)
                    for task in run_tasks:
                        if (
                            task.kind.value != "plan_acceptance"
                            or task.status.value != "succeeded"
                            or task.candidate_binding_ref is None
                        ):
                            continue
                        try:
                            binding = CandidateBinding.model_validate_json(
                                store.read_verified(task.candidate_binding_ref)
                            )
                        except (ValueError, RuntimeError):
                            continue
                        if plan_review_is_reachable(
                            binding, review_media_type=PLAN_REVIEW_MEDIA_TYPE
                        ):
                            plan_reviewed = True
                            break
                runtime_evidence = (
                    runtime_evidence_from_tasks(
                        tuple(run_tasks),
                        tuple(run_events),
                        plan_reviewed=plan_reviewed,
                        artifact_reader=store.read_verified,
                        plan=plan_root,
                        text=text_root,
                        world=world_root,
                        model_calls=SqlModelCallLedger(factory).list_for_run(run_id),
                    )
                    if args.run_id
                    else StageRuntimeEvidence()
                )
                snapshot = DerivedSnapshotRepository(factory).get_for_commit(current_commit)
                roots_payload.update(
                    audit_stage_roots(
                        commit_id=current_commit,
                        plan=plan_root,
                        world=world_root,
                        text=text_root,
                        snapshot=snapshot,
                        tasks=tuple(run_tasks),
                        events=tuple(run_events),
                        runtime=runtime_evidence,
                        artifact_reader=store.read_verified,
                    )
                )
            print(json.dumps(roots_payload, ensure_ascii=False, sort_keys=True))
            return 0
        if args.runtime_command == "classify":
            from novel_agent.services.attempt_classification import classify_attempt

            repository = RuntimeTaskQueryRepository(factory)
            task_id = TaskId(args.task_id)
            task = repository.get_task(task_id)
            attempt = repository.last_settled_attempt(task_id)
            ledger = repository.attempt_effect_evidence(task_id)
            classification = classify_attempt(
                task_id=StableId(task_id.root),
                task_status=task.status,
                attempt=attempt,
                unsettled_sends=ledger.unsettled_sends,
                outstanding_request_ids=ledger.outstanding_request_ids,
                completed_response_refs=ledger.completed_response_refs,
                consumed_response_ids=ledger.consumed_response_ids,
                unavailable_response_ids=ledger.unavailable_response_ids,
                frontier_attempt_id=ledger.frontier_attempt_id,
                block_cause=task.block_cause,
            )
            print(
                json.dumps(
                    {
                        "classification": classification.model_dump(mode="json"),
                        "safe_to_retry": classification.safe_to_retry,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.runtime_command == "bootstrap-prepare":
            from novel_agent.domain.planning_locks import load_author_planning_locks
            from novel_agent.runtime.production_novel_bootstrap import ProductionNovelBootstrap

            brief_text = args.brief.read_text(encoding="utf-8")
            planning_locks = None
            if args.planning_locks is not None:
                planning_locks = load_author_planning_locks(args.planning_locks.read_bytes())
                if (
                    planning_locks.project_id is not None
                    and planning_locks.project_id != args.project_id
                ):
                    raise RuntimeError(
                        "author planning locks belong to another project: "
                        f"{planning_locks.project_id}"
                    )
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
                    bootstrap_max_output_tokens=args.max_output_tokens,
                    bootstrap_request_timeout_seconds=args.bootstrap_timeout_seconds,
                ).prepare(
                    project_id=project_id,
                    brief_text=brief_text,
                    planning_locks=planning_locks,
                )
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
            frozen_endpoints = resolve_registered_model_endpoints(args.endpoint_profile)
            policy, request, descriptor = ProductionNovelBootstrap(
                artifacts=artifacts,
                session_factory=factory,
                endpoints=frozen_endpoints,
            ).commit(
                prepared=document,
                author_id=StableId(args.author_id),
                reason=args.reason,
                target_chapters=args.target_chapters,
                run_id=RunId(args.run_id),
                object_store_root=args.object_store_root,
                retrieval_backend_profile=args.retrieval_backend_profile,
                endpoint_request_limit=args.endpoint_request_limit,
                kv_token_budget=args.kv_token_budget,
                scheduling_timeout_seconds=(
                    args.scheduling_timeout_seconds
                    if args.scheduling_timeout_seconds is not None
                    else DEFAULT_SCHEDULING_TIMEOUT_SECONDS
                ),
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
                        # Record the deployment the fingerprint was computed
                        # against, so dispatch can reuse it instead of guessing.
                        "retrieval_backend_profile": args.retrieval_backend_profile,
                        "opensearch_url": args.opensearch_url,
                        "embedding_url": args.embedding_url,
                        "reranker_url": args.reranker_url,
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
            retrieval = _resolve_retrieval_options(args, descriptors)
            coordinator = ProductionDispatchCoordinator(
                database_url=args.database_url,
                manifest=manifest,
                runs=descriptors,
                model_endpoints=resolve_registered_model_endpoints(args.endpoint_profile),
                assembly_factory=args.assembly_factory,
                project_parallelism=args.project_parallelism,
                endpoint_request_limit=args.endpoint_request_limit,
                kv_token_budget=args.kv_token_budget,
                scheduling_timeout_seconds=(
                    args.scheduling_timeout_seconds
                    if args.scheduling_timeout_seconds is not None
                    else DEFAULT_SCHEDULING_TIMEOUT_SECONDS
                ),
                max_total_tasks=args.max_total_tasks,
                retrieval_backend_profile=retrieval["retrieval_backend_profile"] or "memory",
                opensearch_url=retrieval["opensearch_url"] or "",
                embedding_url=retrieval["embedding_url"] or "",
                reranker_url=retrieval["reranker_url"] or "",
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
                        scheduling_timeout_seconds=(
                            args.scheduling_timeout_seconds
                            if args.scheduling_timeout_seconds is not None
                            else DEFAULT_SCHEDULING_TIMEOUT_SECONDS
                        ),
                        retrieval_backend_profile=args.retrieval_backend_profile or "memory",
                        opensearch_url=args.opensearch_url,
                        embedding_url=args.embedding_url,
                        reranker_url=args.reranker_url,
                    ),
                )
            except RuntimeError as error:
                if "requires registered model endpoints" in str(error):
                    return _resource_blocked(error)
                raise
            attestation = getattr(assembly, "attestation", None)
            if (
                attestation is not None
                and policy.policy_hash != attestation.configuration_fingerprint.root
            ):
                output = {
                    "status": "failed",
                    "error_type": "RUN_CONFIGURATION_CHANGED",
                    "error_message": "RUN_CONFIGURATION_CHANGED",
                }
                print(json.dumps(output, ensure_ascii=False, sort_keys=True))
                return 2
            try:
                results = _run_async(assembly.dispatcher.run_bounded(max_tasks=args.max_tasks))
            except (ModelEndpointError, ConnectionError, TimeoutError, OSError) as error:
                return _resource_blocked(error)
            advance_status, advance_exit_code = _advance_outcome(results)
            output = {
                "status": advance_status,
                "progressed": len(results),
                "results": [item.model_dump(mode="json") for item in results],
            }
            admission = _admission_receipt(assembly)
            if admission is not None:
                output["admission"] = admission
            if args.receipt is not None:
                if attestation is None:
                    raise RuntimeError("production assembly did not provide a CLI attestation")
                _write_json_once(
                    args.receipt,
                    {
                        "receipt_type": "runtime_cli_advance",
                        "status": advance_status,
                        "assembly_factory": args.assembly_factory,
                        "endpoint_profile": args.endpoint_profile,
                        "spec_locator": attestation.factory_locator,
                        "session_factory_identity": attestation.session_factory_identity,
                        "model_gateway": attestation.model_gateway,
                        "endpoints": [
                            item.model_dump(mode="json") for item in attestation.endpoints
                        ],
                        **output,
                    },
                )
            print(json.dumps(output, sort_keys=True))
            return advance_exit_code
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
        elif args.runtime_command == "supersede":
            task = commands.supersede_task(
                task_id,
                reason=args.reason,
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
