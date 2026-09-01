"""Deterministic CLI coverage for the Stage 5 runtime subcommands."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.runtime import RuntimeTaskQueryRepository
from novel_agent.cli import main
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.creative_runtime import (
    AcceptanceCommand,
    AcceptanceDecision,
    ActorKind,
    AutomationMode,
    CandidateBinding,
    CandidateKind,
    CreativeRunPolicy,
    CreativeRunRequest,
    CreativeRunResult,
    CreativeRunTerminal,
    UnblockCommand,
)
from novel_agent.domain.ids import (
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.runtime import (
    EffectReceipt,
    EffectStatus,
    TaskKind,
    TaskRecord,
    TaskStatus,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.event_log import RunEventLogRepository
from novel_agent.services.runtime_commands import RuntimeCommandService
from tests.factories import make_manifest

HASH = "sha256:" + "1" * 64
PERMISSION_HASH = "sha256:" + "2" * 64
NOW = datetime(2026, 8, 10, tzinfo=UTC)


def _install_production_loader(
    monkeypatch: pytest.MonkeyPatch,
    *,
    progressed: bool,
) -> list[Any]:
    contexts: list[Any] = []

    def _load(spec: str, context: Any) -> object:
        assert spec == "tests.production_assembly:build"
        contexts.append(context)

        class _Dispatcher:
            async def run_bounded(self, *, max_tasks: int) -> tuple[CreativeRunResult, ...]:
                assert max_tasks == 2
                if not progressed:
                    return ()
                return (
                    CreativeRunResult(
                        run_id=context.run_id,
                        project_id=context.project_id,
                        terminal=CreativeRunTerminal.PROGRESSED,
                        basis_commit=CommitId("sha256:" + "3" * 64),
                        current_commit=CommitId("sha256:" + "4" * 64),
                        reason_code="test_progress",
                    ),
                )

        return SimpleNamespace(dispatcher=_Dispatcher())

    monkeypatch.setattr(
        "novel_agent.runtime.creative_assembly.load_production_runtime_assembly",
        _load,
    )
    return contexts


@pytest.fixture
def cli_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.sqlite"
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    commits = CommitService(factory)
    file_base = commits.initialize_project(make_manifest())
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    commands = RuntimeCommandService(
        factory, RunEventLogRepository(factory), lambda _project_id: PERMISSION_HASH
    )
    request = CreativeRunRequest(
        run_id=RunId("run.cli"),
        project_id=ProjectId("project.test"),
        basis_commit=file_base,
        policy=CreativeRunPolicy(
            automation_mode=AutomationMode.MANUAL,
            policy_hash=HASH,
            permission_hash=PERMISSION_HASH,
        ),
    )
    commands.create_run_and_initial_task(request)
    candidate_ref = artifacts.put(
        b'{"plan":"candidate"}',
        "application/vnd.novel-agent.stage5-plan-candidate+json",
        SchemaVersion("1.0.0"),
    )
    (tmp_path / "artifact-refs.json").write_text(
        json.dumps([candidate_ref.model_dump(mode="json")]),
        encoding="utf-8",
    )
    (tmp_path / "policy.json").write_text(
        json.dumps(
            CreativeRunPolicy(
                automation_mode=AutomationMode.MANUAL,
                policy_hash=HASH,
                permission_hash=PERMISSION_HASH,
            ).model_dump(mode="json")
        ),
        encoding="utf-8",
    )
    (tmp_path / "request.json").write_text(
        json.dumps(request.model_dump(mode="json")), encoding="utf-8"
    )
    candidate = CandidateBinding(
        candidate_id=StableId("candidate.cli"),
        kind=CandidateKind.PLAN,
        artifact_ref=candidate_ref,
        candidate_hash=candidate_ref.artifact_id.root,
        basis_commit=file_base,
    )
    candidate_binding_ref = artifacts.put(
        candidate.model_dump_json().encode("utf-8"),
        "application/vnd.novel-agent.stage5-candidate-binding+json",
        SchemaVersion("1.0.0"),
    )
    waiting = TaskRecord(
        task_id=TaskId("run.cli.plan.accept"),
        run_id=request.run_id,
        project_id=request.project_id,
        kind=TaskKind.PLAN_ACCEPTANCE,
        task_revision=0,
        status=TaskStatus.WAITING_INPUT,
        basis_commit=file_base,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
        input_artifact_refs=(candidate_ref,),
        candidate_binding_ref=candidate_binding_ref,
        dependency_task_ids=(TaskId("run.cli.plan"),),
    )
    commands.create_task(waiting)
    # Claim the plan task and record an effect for the reconcile subcommands.
    _, fence = commands.claim(TaskId("run.cli.plan"), worker_id="cli-worker")
    commands.mark_started(fence)
    commands.record_effect_requested(
        fence,
        EffectReceipt(
            effect_identity=StableId("effect.cli"),
            external_system="provider",
            request_identity=StableId("request.cli"),
            status=EffectStatus.REQUESTED,
            attempt_no=1,
        ),
    )
    # Create a blocked task for the unblock subcommand.
    blocked = TaskRecord(
        task_id=TaskId("run.cli.plan.blocked"),
        run_id=request.run_id,
        project_id=request.project_id,
        kind=TaskKind.PLAN_COMMIT,
        task_revision=0,
        status=TaskStatus.BLOCKED,
        basis_commit=file_base,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
        block_cause="validation rejected",
        dependency_task_ids=(TaskId("run.cli.plan"),),
    )
    commands.create_task(blocked)
    commands.create_task(
        TaskRecord(
            task_id=TaskId("run.cli.plan.budget"),
            run_id=request.run_id,
            project_id=request.project_id,
            kind=TaskKind.PLAN_CANDIDATE,
            task_revision=0,
            status=TaskStatus.BUDGET_REVIEW,
            basis_commit=file_base,
            policy_hash=HASH,
            permission_hash=PERMISSION_HASH,
            dependency_task_ids=(TaskId("run.cli.plan"),),
        )
    )
    from novel_agent.services.runtime_commands import _digest

    blocked_unblock = UnblockCommand(
        command_id=StableId("unblock.cli"),
        run_id=request.run_id,
        task_id=blocked.task_id,
        actor_id="operator",
        reason="evidence changed",
        block_cause_fingerprint=_digest("validation rejected"),
        changed_evidence_refs=(candidate_ref,),
    )
    (tmp_path / "unblock.json").write_text(
        json.dumps(blocked_unblock.model_dump(mode="json")), encoding="utf-8"
    )
    accept = AcceptanceCommand(
        command_id=StableId("accept.cli"),
        project_id=request.project_id,
        run_id=request.run_id,
        task_id=waiting.task_id,
        candidate=candidate,
        acceptance_policy_hash=HASH,
        actor_kind=ActorKind.AUTHOR,
        actor_id="author",
        decision=AcceptanceDecision.ACCEPT,
        reason="approved",
        expected_project_commit=file_base,
        idempotency_identity=StableId("accept.cli.identity"),
        issued_at=NOW,
    )
    (tmp_path / "accept.json").write_text(
        json.dumps(accept.model_dump(mode="json")), encoding="utf-8"
    )
    effect = EffectReceipt(
        effect_identity=StableId("effect.cli"),
        external_system="provider",
        request_identity=StableId("request.cli"),
        status=EffectStatus.COMPLETED,
        attempt_no=1,
        completed_at=NOW,
    )
    (tmp_path / "effect.json").write_text(
        json.dumps(effect.model_dump(mode="json")), encoding="utf-8"
    )
    engine.dispose()
    return db_path


def test_runtime_start_and_status_subcommands(cli_db: Path) -> None:
    url = f"sqlite+pysqlite:///{cli_db}"
    request_path = cli_db.parent / "request2.json"
    request = CreativeRunRequest.model_validate_json(
        (cli_db.parent / "request.json").read_text(encoding="utf-8")
    )
    (request_path).write_text(
        json.dumps(
            request.model_copy(update={"run_id": RunId("run.cli.start")}).model_dump(mode="json")
        ),
        encoding="utf-8",
    )
    start = main(["runtime", "--database-url", url, "start", "--request", str(request_path)])
    assert start == 0
    status = main(["runtime", "--database-url", url, "status", "--run-id", "run.cli.start"])
    assert status == 0


def test_runtime_maintenance_subcommand(cli_db: Path) -> None:
    from novel_agent.services.runtime_maintenance import MaintenanceCommand, MaintenanceKind

    url = f"sqlite+pysqlite:///{cli_db}"
    command_path = cli_db.parent / "maintenance.json"
    command = MaintenanceCommand(
        command_id=StableId("maintenance.cli"),
        kind=MaintenanceKind.VERIFY_ARTIFACT_REFERENCES,
        requested_at=NOW,
    )
    command_path.write_text(json.dumps(command.model_dump(mode="json")), encoding="utf-8")
    args = ["runtime", "--database-url", url, "maintenance", "--command", str(command_path)]
    assert main(args) == 0


def test_runtime_export_report_subcommand(cli_db: Path) -> None:
    url = f"sqlite+pysqlite:///{cli_db}"
    manifest_path = (
        Path(__file__).parents[2] / "src/novel_agent/runtime/stage5_development_manifest.json"
    )
    output = cli_db.parent / "report.json"
    assert (
        main(
            [
                "runtime",
                "--database-url",
                url,
                "export-report",
                "--run-id",
                "run.cli",
                "--manifest",
                str(manifest_path),
                "--executable-commit",
                "a" * 40,
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert output.exists()


def test_runtime_accept_and_reconcile_effect_subcommands(cli_db: Path) -> None:
    url = f"sqlite+pysqlite:///{cli_db}"
    assert (
        main(
            [
                "runtime",
                "--database-url",
                url,
                "accept-plan",
                "--command",
                str(cli_db.parent / "accept.json"),
                "--policy",
                str(cli_db.parent / "policy.json"),
                "--object-store-root",
                str(cli_db.parent / "objects"),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "runtime",
                "--database-url",
                url,
                "reconcile-effect",
                "--project-id",
                "project.test",
                "--run-id",
                "run.cli",
                "--task-id",
                "run.cli.plan",
                "--observed-revision",
                "1",
                "--command-id",
                "reconcile-effect.cli",
                "--receipt",
                str(cli_db.parent / "effect.json"),
            ]
        )
        == 0
    )


def test_runtime_reconcile_attempt_and_unblock_subcommands(cli_db: Path) -> None:
    url = f"sqlite+pysqlite:///{cli_db}"
    # Settle the recorded effect first so the attempt frontier is resolved.
    assert (
        main(
            [
                "runtime",
                "--database-url",
                url,
                "reconcile-effect",
                "--project-id",
                "project.test",
                "--run-id",
                "run.cli",
                "--task-id",
                "run.cli.plan",
                "--observed-revision",
                "1",
                "--command-id",
                "reconcile-effect.cli",
                "--receipt",
                str(cli_db.parent / "effect.json"),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "runtime",
                "--database-url",
                url,
                "reconcile",
                "--project-id",
                "project.test",
                "--run-id",
                "run.cli",
                "--task-id",
                "run.cli.plan",
                "--observed-revision",
                "1",
                "--command-id",
                "reconcile.cli",
                "--actor-id",
                "operator",
                "--reason",
                "reconcile via cli",
                "--terminal-status",
                "blocked",
                "--failure-class",
                "validation_rejected",
                "--artifact-refs",
                str(cli_db.parent / "artifact-refs.json"),
            ]
        )
        == 0
    )
    # Unblock the blocked task via the CLI.
    assert (
        main(
            [
                "runtime",
                "--database-url",
                url,
                "unblock",
                "--project-id",
                "project.test",
                "--run-id",
                "run.cli",
                "--observed-revision",
                "0",
                "--command",
                str(cli_db.parent / "unblock.json"),
            ]
        )
        == 0
    )
    settled = RuntimeTaskQueryRepository(build_session_factory(create_engine(url))).list_run(
        RunId("run.cli")
    )
    plan = next(item for item in settled if item.task_id == TaskId("run.cli.plan"))
    artifact_ref = ArtifactRef.model_validate(
        json.loads((cli_db.parent / "artifact-refs.json").read_text(encoding="utf-8"))[0],
        strict=True,
    )
    assert plan.status is TaskStatus.BLOCKED
    assert artifact_ref in plan.terminal_artifact_refs


def test_runtime_pause_resume_and_unblock_subcommands(cli_db: Path) -> None:
    url = f"sqlite+pysqlite:///{cli_db}"
    assert (
        main(
            [
                "runtime",
                "--database-url",
                url,
                "pause",
                "--project-id",
                "project.test",
                "--run-id",
                "run.cli",
                "--task-id",
                "run.cli.plan.accept",
                "--observed-revision",
                "0",
                "--command-id",
                "pause.cli",
                "--actor-id",
                "operator",
                "--reason",
                "pause via cli",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "runtime",
                "--database-url",
                url,
                "resume",
                "--project-id",
                "project.test",
                "--run-id",
                "run.cli",
                "--task-id",
                "run.cli.plan.accept",
                "--observed-revision",
                "1",
                "--command-id",
                "resume.cli",
                "--actor-id",
                "operator",
                "--reason",
                "resume via cli",
            ]
        )
        == 0
    )


def test_runtime_extend_budget_can_add_a_planner_memory_tranche(cli_db: Path) -> None:
    url = f"sqlite+pysqlite:///{cli_db}"
    assert (
        main(
            [
                "runtime",
                "--database-url",
                url,
                "extend-budget",
                "--project-id",
                "project.test",
                "--run-id",
                "run.cli",
                "--task-id",
                "run.cli.plan.budget",
                "--observed-revision",
                "0",
                "--command-id",
                "extend-budget.cli",
                "--actor-id",
                "operator",
                "--reason",
                "allow another planner memory tranche",
                "--additional-planner-memory-tranches",
                "1",
            ]
        )
        == 0
    )


def test_runtime_accept_plan_rejects_mismatched_command(cli_db: Path) -> None:
    url = f"sqlite+pysqlite:///{cli_db}"
    accept = AcceptanceCommand.model_validate_json(
        (cli_db.parent / "accept.json").read_text(encoding="utf-8")
    ).model_copy(update={"decision": AcceptanceDecision.REJECT})
    (cli_db.parent / "accept.json").write_text(
        json.dumps(accept.model_dump(mode="json")), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="does not match"):
        main(
            [
                "runtime",
                "--database-url",
                url,
                "accept-plan",
                "--command",
                str(cli_db.parent / "accept.json"),
                "--policy",
                str(cli_db.parent / "policy.json"),
                "--object-store-root",
                str(cli_db.parent / "objects"),
            ]
        )


def test_runtime_identity_mismatches_are_rejected(cli_db: Path) -> None:
    url = f"sqlite+pysqlite:///{cli_db}"
    with pytest.raises(ValueError, match="identity mismatch"):
        main(
            [
                "runtime",
                "--database-url",
                url,
                "reconcile-effect",
                "--project-id",
                "project.other",
                "--run-id",
                "run.cli",
                "--task-id",
                "run.cli.plan",
                "--observed-revision",
                "1",
                "--command-id",
                "reconcile-effect.cli",
                "--receipt",
                str(cli_db.parent / "effect.json"),
            ]
        )
    with pytest.raises(ValueError, match="identity mismatch"):
        main(
            [
                "runtime",
                "--database-url",
                url,
                "reconcile",
                "--project-id",
                "project.other",
                "--run-id",
                "run.cli",
                "--task-id",
                "run.cli.plan",
                "--observed-revision",
                "1",
                "--command-id",
                "reconcile.cli",
                "--actor-id",
                "operator",
                "--reason",
                "r",
                "--terminal-status",
                "waiting_retry",
            ]
        )
    from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
    from novel_agent.domain.creative_runtime import UnblockCommand
    from novel_agent.domain.ids import SchemaVersion
    from novel_agent.services.artifacts import ArtifactRepository
    from novel_agent.services.runtime_commands import _digest

    ref = ArtifactRepository(FilesystemObjectStore(cli_db.parent / "objects")).put(
        b"{}", "application/json", SchemaVersion("1.0.0")
    )
    bad = UnblockCommand(
        command_id=StableId("unblock.other"),
        run_id=RunId("run.cli"),
        task_id=TaskId("run.cli.plan.blocked"),
        actor_id="operator",
        reason="x",
        block_cause_fingerprint=_digest("validation rejected"),
        changed_evidence_refs=(ref,),
    )
    (cli_db.parent / "unblock-bad.json").write_text(
        json.dumps(bad.model_dump(mode="json")), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        main(
            [
                "runtime",
                "--database-url",
                url,
                "unblock",
                "--project-id",
                "project.test",
                "--run-id",
                "run.other",
                "--observed-revision",
                "0",
                "--command",
                str(cli_db.parent / "unblock-bad.json"),
            ]
        )
    with pytest.raises(ValueError, match="identity mismatch"):
        main(
            [
                "runtime",
                "--database-url",
                url,
                "pause",
                "--project-id",
                "project.other",
                "--run-id",
                "run.cli",
                "--task-id",
                "run.cli.plan.accept",
                "--observed-revision",
                "0",
                "--command-id",
                "pause.cli",
                "--actor-id",
                "operator",
                "--reason",
                "x",
            ]
        )


def test_runtime_reject_plan_subcommand(cli_db: Path) -> None:
    from sqlalchemy import create_engine as _ce

    from novel_agent.adapters.postgres.database import build_session_factory as _bsf

    url = f"sqlite+pysqlite:///{cli_db}"
    engine = _ce(f"sqlite+pysqlite:///{cli_db}")
    factory = _bsf(engine)
    commands = RuntimeCommandService(
        factory, RunEventLogRepository(factory), lambda _project_id: PERMISSION_HASH
    )
    # The accept task was settled by an earlier test only if it ran; in a fresh
    # fixture the accept task is WAITING_INPUT, so reject it here.
    waiting = commands.get_task(TaskId("run.cli.plan.accept"))
    assert waiting.status is TaskStatus.WAITING_INPUT
    reject = AcceptanceCommand.model_validate_json(
        (cli_db.parent / "accept.json").read_text(encoding="utf-8")
    ).model_copy(
        update={
            "command_id": StableId("reject.cli"),
            "decision": AcceptanceDecision.REJECT,
            "idempotency_identity": StableId("reject.cli.identity"),
        }
    )
    (cli_db.parent / "reject.json").write_text(
        json.dumps(reject.model_dump(mode="json")), encoding="utf-8"
    )
    engine.dispose()
    assert (
        main(
            [
                "runtime",
                "--database-url",
                url,
                "reject-plan",
                "--command",
                str(cli_db.parent / "reject.json"),
                "--policy",
                str(cli_db.parent / "policy.json"),
                "--object-store-root",
                str(cli_db.parent / "objects"),
            ]
        )
        == 0
    )


def test_runtime_advance_uses_explicit_production_assembly(
    cli_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+pysqlite:///{cli_db}"
    manifest_path = (
        Path(__file__).parents[2] / "src/novel_agent/runtime/stage5_development_manifest.json"
    )
    request_path = cli_db.parent / "advance-request.json"
    request = CreativeRunRequest.model_validate_json(
        (cli_db.parent / "request.json").read_text(encoding="utf-8")
    )
    (request_path).write_text(
        json.dumps(
            request.model_copy(update={"run_id": RunId("run.cli.advance")}).model_dump(mode="json")
        ),
        encoding="utf-8",
    )
    assert main(["runtime", "--database-url", url, "start", "--request", str(request_path)]) == 0
    contexts = _install_production_loader(monkeypatch, progressed=True)
    assert (
        main(
            [
                "runtime",
                "--database-url",
                url,
                "advance",
                "--project-id",
                "project.test",
                "--run-id",
                "run.cli.advance",
                "--policy",
                str(cli_db.parent / "policy.json"),
                "--manifest",
                str(manifest_path),
                "--object-store-root",
                str(cli_db.parent / "objects"),
                "--assembly-factory",
                "tests.production_assembly:build",
                "--max-tasks",
                "2",
            ]
        )
        == 0
    )
    assert len(contexts) == 1
    assert contexts[0].project_id == ProjectId("project.test")
    assert contexts[0].run_id == RunId("run.cli.advance")
    assert contexts[0].object_store_root == cli_db.parent / "objects"


def test_runtime_advance_no_ready_task_reports_progressed_zero(
    cli_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+pysqlite:///{cli_db}"
    manifest_path = (
        Path(__file__).parents[2] / "src/novel_agent/runtime/stage5_development_manifest.json"
    )
    contexts = _install_production_loader(monkeypatch, progressed=False)
    assert (
        main(
            [
                "runtime",
                "--database-url",
                url,
                "advance",
                "--project-id",
                "project.test",
                "--run-id",
                "run.cli",
                "--policy",
                str(cli_db.parent / "policy.json"),
                "--manifest",
                str(manifest_path),
                "--object-store-root",
                str(cli_db.parent / "objects"),
                "--assembly-factory",
                "tests.production_assembly:build",
                "--max-tasks",
                "2",
            ]
        )
        == 0
    )
    assert len(contexts) == 1


def test_runtime_advance_defaults_to_repo_production_factory(
    cli_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novel_agent.runtime.creative_assembly import DEFAULT_PRODUCTION_ASSEMBLY_FACTORY

    url = f"sqlite+pysqlite:///{cli_db}"
    manifest_path = (
        Path(__file__).parents[2] / "src/novel_agent/runtime/stage5_development_manifest.json"
    )
    specs: list[str] = []

    def _load(spec: str, context: object) -> object:
        specs.append(spec)

        class _Dispatcher:
            async def run_bounded(self, *, max_tasks: int) -> tuple[object, ...]:
                del max_tasks
                return ()

        return type("Assembly", (), {"dispatcher": _Dispatcher()})()

    monkeypatch.setattr(
        "novel_agent.runtime.creative_assembly.load_production_runtime_assembly",
        _load,
    )
    assert (
        main(
            [
                "runtime",
                "--database-url",
                url,
                "advance",
                "--project-id",
                "project.test",
                "--run-id",
                "run.cli",
                "--policy",
                str(cli_db.parent / "policy.json"),
                "--manifest",
                str(manifest_path),
                "--object-store-root",
                str(cli_db.parent / "objects"),
                "--max-tasks",
                "1",
            ]
        )
        == 0
    )
    assert specs == [DEFAULT_PRODUCTION_ASSEMBLY_FACTORY]


def test_runtime_advance_binds_run_identity_under_same_project(
    cli_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+pysqlite:///{cli_db}"
    manifest_path = (
        Path(__file__).parents[2] / "src/novel_agent/runtime/stage5_development_manifest.json"
    )
    policy_path = str(cli_db.parent / "policy.json")
    objects_root = str(cli_db.parent / "objects")
    request = CreativeRunRequest.model_validate_json(
        (cli_db.parent / "request.json").read_text(encoding="utf-8")
    )
    for run_id in ("run.cli.identity-a", "run.cli.identity-b"):
        request_path = cli_db.parent / f"{run_id}.json"
        request_path.write_text(
            json.dumps(
                request.model_copy(update={"run_id": RunId(run_id)}).model_dump(mode="json")
            ),
            encoding="utf-8",
        )
        assert (
            main(["runtime", "--database-url", url, "start", "--request", str(request_path)]) == 0
        )

    contexts = _install_production_loader(monkeypatch, progressed=True)

    def _advance(run_id: str) -> int:
        return main(
            [
                "runtime",
                "--database-url",
                url,
                "advance",
                "--project-id",
                "project.test",
                "--run-id",
                run_id,
                "--policy",
                policy_path,
                "--manifest",
                str(manifest_path),
                "--object-store-root",
                objects_root,
                "--assembly-factory",
                "tests.production_assembly:build",
                "--max-tasks",
                "2",
            ]
        )

    assert _advance("run.cli.identity-a") == 0
    assert _advance("run.cli.identity-missing") == 0
    assert [context.run_id for context in contexts] == [
        RunId("run.cli.identity-a"),
        RunId("run.cli.identity-missing"),
    ]
