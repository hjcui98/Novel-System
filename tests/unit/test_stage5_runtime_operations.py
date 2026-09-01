from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.models import RuntimeTaskProjectionRow
from novel_agent.adapters.postgres.runtime import RuntimeTaskQueryRepository
from novel_agent.domain.creative_runtime import (
    AutomationMode,
    CreativeRunPolicy,
    CreativeRunRequest,
    CreativeRunResult,
    CreativeRunTerminal,
)
from novel_agent.domain.ids import (
    ArtifactId,
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
    ResumabilityStatus,
    RunCheckpoint,
    TaskStatus,
)
from novel_agent.domain.stage5_evaluation import (
    IsolatedKernelStatus,
    Stage5ScenarioEvidence,
)
from novel_agent.domain.stage5_manifest import load_stage5_manifest
from novel_agent.ports.creative_runtime import EffectStatusResolver
from novel_agent.runtime.creative_dispatcher import CreativeDispatcher
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.creative_runtime import CreativeRuntimeService
from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository
from novel_agent.services.runtime_commands import (
    RuntimeCommandConflictError,
    RuntimeCommandService,
)
from novel_agent.services.runtime_maintenance import (
    MaintenanceCommand,
    MaintenanceDisposition,
    MaintenanceKind,
    RuntimeMaintenanceService,
    RuntimeSupervisor,
)
from novel_agent.services.runtime_recovery import RuntimeRecoveryService
from novel_agent.services.stage5_evaluation import REQUIRED_SCENARIOS, IsolatedRuntimeEvaluator
from tests.factories import make_manifest

HASH = "sha256:" + "1" * 64
NOW = datetime(2026, 8, 10, tzinfo=UTC)


@pytest.fixture
def operations_kernel(
    tmp_path: Path,
) -> Iterator[
    tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ]
]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    commits = CommitService(factory)
    base = commits.initialize_project(make_manifest())
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    commands = RuntimeCommandService(
        factory, RunEventLogRepository(factory), lambda _project_id: HASH
    )
    yield factory, commits, artifacts, commands, base
    engine.dispose()


def _request(run_id: str, base: CommitId) -> CreativeRunRequest:
    return CreativeRunRequest(
        run_id=RunId(run_id),
        project_id=ProjectId("project.test"),
        basis_commit=base,
        policy=CreativeRunPolicy(
            automation_mode=AutomationMode.MANUAL,
            policy_hash=HASH,
            permission_hash=HASH,
        ),
    )


class _Resolution:
    def __init__(self, receipt: EffectReceipt) -> None:
        self.receipt = receipt


class _Resolver:
    def __init__(self, status: EffectStatus) -> None:
        self.status = status

    def resolve(self, receipt: EffectReceipt) -> _Resolution:
        return _Resolution(receipt.model_copy(update={"status": self.status, "completed_at": NOW}))


def test_recovery_selects_old_safe_checkpoint_and_reconciles_effects(
    operations_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, commands, base = operations_kernel
    task = commands.create_run_and_initial_task(_request("run.recovery", base))
    _, fence = commands.claim(task.task_id, worker_id="worker.dead")
    commands.mark_started(fence)
    state = artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0"))
    position = RunEventLogRepository(factory).replay(task.run_id)[-1].sequence_no
    safe = RunCheckpoint(
        checkpoint_id=StableId("checkpoint.recovery.safe"),
        run_id=task.run_id,
        event_position=position,
        logical_stage="safe",
        state_artifact_ref=state,
        resumability_status=ResumabilityStatus.RESUMABLE,
    )
    commands.save_checkpoint(fence, safe)
    blocked = safe.model_copy(
        update={
            "checkpoint_id": StableId("checkpoint.recovery.blocked"),
            "event_position": RunEventLogRepository(factory).replay(task.run_id)[-1].sequence_no,
            "resumability_status": ResumabilityStatus.BLOCKED,
            "reason": "effect active",
        }
    )
    commands.save_checkpoint(fence, blocked)
    assert RunCheckpointRepository(factory).latest(task.run_id) == blocked

    requested = EffectReceipt(
        effect_identity=StableId("effect.recovery"),
        external_system="provider",
        request_identity=StableId("request.recovery"),
        status=EffectStatus.REQUESTED,
        attempt_no=1,
    )
    commands.record_effect_requested(fence, requested)
    commands.record_effect_terminal(
        fence, requested.model_copy(update={"status": EffectStatus.UNCERTAIN})
    )
    recovery = RuntimeRecoveryService(
        factory,
        commands,
        RunCheckpointRepository(factory),
        artifacts,
        commits,
        cast(EffectStatusResolver, _Resolver(EffectStatus.COMPLETED)),
    )
    assert recovery.select_safe_checkpoint(task.task_id) == safe
    assert recovery.reconcile_uncertain_effects(task.task_id)[0].status is EffectStatus.COMPLETED
    commands.operator_reconcile_attempt(
        task.task_id,
        command_id=StableId("operator.reconcile.dead"),
        actor_id="operator",
        reason="worker confirmed dead",
    )
    checkpoint, attempt, resumed_fence = recovery.resume(
        task.task_id, worker_id="worker.fresh", actor_id="operator"
    )
    assert checkpoint == safe and attempt.attempt_no == 2
    assert resumed_fence.attempt_id == attempt.attempt_id

    unresolved = RuntimeRecoveryService(
        factory,
        commands,
        RunCheckpointRepository(factory),
        artifacts,
        commits,
        cast(EffectStatusResolver, _Resolver(EffectStatus.UNCERTAIN)),
    )
    assert unresolved.reconcile_uncertain_effects(task.task_id) == ()


def test_recovery_reconciles_max_length_effect_identity(
    operations_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, commands, base = operations_kernel
    task = commands.create_run_and_initial_task(_request("run.recovery-long-effect", base))
    _, fence = commands.claim(task.task_id, worker_id="worker.dead")
    commands.mark_started(fence)
    requested = EffectReceipt(
        effect_identity=StableId("e" * 128),
        external_system="provider",
        request_identity=StableId("request.recovery-long-effect"),
        status=EffectStatus.REQUESTED,
        attempt_no=1,
    )
    commands.record_effect_requested(fence, requested)
    commands.record_effect_terminal(
        fence,
        requested.model_copy(update={"status": EffectStatus.UNCERTAIN}),
    )
    recovery = RuntimeRecoveryService(
        factory,
        commands,
        RunCheckpointRepository(factory),
        artifacts,
        commits,
        cast(EffectStatusResolver, _Resolver(EffectStatus.COMPLETED)),
    )

    resolved = recovery.reconcile_uncertain_effects(task.task_id)

    assert resolved[0].status is EffectStatus.COMPLETED
    events = RunEventLogRepository(factory).replay(task.run_id)
    assert all(len(event.idempotency_identity.root) <= 128 for event in events)


def test_query_maintenance_and_supervisor_are_read_only_and_no_model(
    operations_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, _, _, commands, base = operations_kernel
    task = commands.create_run_and_initial_task(_request("run.ops", base))
    query = RuntimeTaskQueryRepository(factory)
    assert query.next_ready() == task.task_id
    assert query.next_ready(project_id=task.project_id) == task.task_id
    assert query.next_ready(project_id=ProjectId("project.other")) is None
    with pytest.raises(ValueError, match="ready batch limit must be positive"):
        query.ready_batch(limit=0)
    assert query.list_run(task.run_id) == (task,)
    future = datetime.now(UTC) + timedelta(hours=1)
    with factory() as session, session.begin():
        row = session.get(RuntimeTaskProjectionRow, task.task_id.root)
        assert row is not None
        payload = dict(row.task_json)
        payload["scheduled_for"] = future.isoformat()
        row.task_json = payload
        row.scheduled_for = future
    assert query.next_ready() is None
    assert query.ready_batch(limit=8) == ()
    scheduled = query.next_scheduled_at(now=datetime.now(UTC))
    assert scheduled is not None
    assert scheduled > datetime.now(UTC)
    assert query.future_scheduled_count() == 1
    assert query.next_scheduled_at(now=future + timedelta(seconds=1)) is None

    maintenance = RuntimeMaintenanceService(factory)
    projection = maintenance.precheck(
        MaintenanceCommand(
            command_id=StableId("maintenance.projection"),
            kind=MaintenanceKind.RECONCILE_PROJECTION_FRESHNESS,
            project_id=task.project_id,
            requested_at=NOW,
        )
    )
    assert projection.disposition is MaintenanceDisposition.WORK_REQUIRED
    assert projection.model_requests_created == 0
    no_work = maintenance.precheck(
        MaintenanceCommand(
            command_id=StableId("maintenance.artifacts"),
            kind=MaintenanceKind.VERIFY_ARTIFACT_REFERENCES,
            requested_at=NOW,
        )
    )
    assert no_work.disposition is MaintenanceDisposition.NO_WORK

    with factory() as session, session.begin():
        row = session.get(RuntimeTaskProjectionRow, task.task_id.root)
        assert row is not None
        row.status = TaskStatus.WAITING_RETRY.value
        row.updated_at = NOW - timedelta(days=1)
    assert query.next_ready() is None
    audit = maintenance.precheck(
        MaintenanceCommand(
            command_id=StableId("maintenance.audit"),
            kind=MaintenanceKind.AUDIT_STUCK_OR_POISON_TASKS,
            requested_at=NOW,
        )
    )
    assert audit.item_count == 1
    findings = RuntimeSupervisor(factory, stuck_after=timedelta(minutes=1)).inspect()
    assert len(findings) == 1 and findings[0].requires_operator


def test_supervisor_finding_preserves_max_length_task_identity(
    operations_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, _, _, commands, base = operations_kernel
    task = commands.create_run_and_initial_task(_request("t" * 128, base))
    with factory() as session, session.begin():
        row = session.get(RuntimeTaskProjectionRow, task.task_id.root)
        assert row is not None
        row.status = TaskStatus.WAITING_RETRY.value
        row.updated_at = NOW - timedelta(days=1)

    findings = RuntimeSupervisor(factory, stuck_after=timedelta(minutes=1)).inspect()

    assert len(findings) == 1
    assert findings[0].task_id == task.task_id
    assert findings[0].finding_id.root == task.task_id.root
    assert len(findings[0].finding_id.root) == 128


def test_claim_rechecks_current_permission_hash(
    operations_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, _, _, _, base = operations_kernel
    commands = RuntimeCommandService(
        factory,
        RunEventLogRepository(factory),
        lambda _project_id: "sha256:" + "9" * 64,
    )
    task = commands.create_run_and_initial_task(_request("run.permission-changed", base))
    with pytest.raises(RuntimeCommandConflictError, match="permission_changed"):
        commands.claim(task.task_id, worker_id="worker.denied")


class _TaskSource:
    def __init__(self, task_id: TaskId | None) -> None:
        self.task_id = task_id

    def next_ready(
        self,
        *,
        project_id: ProjectId | None = None,
        run_id: RunId | None = None,
    ) -> TaskId | None:
        result, self.task_id = self.task_id, None
        return result


class _Runtime:
    def __init__(self, *, conflict: bool = False) -> None:
        self.conflict = conflict

    async def advance(self, task_id: TaskId, *, worker_id: str) -> CreativeRunResult:
        if self.conflict:
            raise RuntimeCommandConflictError("lost race")
        commit = CommitId("sha256:" + "2" * 64)
        return CreativeRunResult(
            run_id=RunId("run.dispatch"),
            project_id=ProjectId("project.test"),
            terminal=CreativeRunTerminal.PROGRESSED,
            current_task_id=task_id,
            basis_commit=commit,
            current_commit=commit,
            reason_code=worker_id,
        )


def test_dispatcher_is_bounded_and_claim_conflicts_are_normal() -> None:
    task_id = TaskId("task.dispatch")
    dispatcher = CreativeDispatcher(
        cast(RuntimeTaskQueryRepository, _TaskSource(task_id)),
        cast(CreativeRuntimeService, _Runtime()),
        worker_id="worker.dispatch",
    )
    results = asyncio.run(dispatcher.run_bounded(max_tasks=2))
    assert len(results) == 1 and results[0].reason_code == "worker.dispatch"
    assert asyncio.run(dispatcher.poll_one()) is None
    with pytest.raises(ValueError, match="positive"):
        asyncio.run(dispatcher.run_bounded(max_tasks=0))
    with pytest.raises(ValueError, match="required"):
        CreativeDispatcher(
            cast(RuntimeTaskQueryRepository, _TaskSource(None)),
            cast(CreativeRuntimeService, _Runtime()),
            worker_id="",
        )
    conflict = CreativeDispatcher(
        cast(RuntimeTaskQueryRepository, _TaskSource(task_id)),
        cast(CreativeRuntimeService, _Runtime(conflict=True)),
        worker_id="worker.dispatch",
    )
    assert asyncio.run(conflict.poll_one()) is None


def test_manifest_versions_features_and_formal_evaluator(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    manifest_path = root / "src/novel_agent/runtime/stage5_development_manifest.json"
    assert load_stage5_manifest(manifest_path).stage3_gate == "CONDITIONAL"
    broken = tmp_path / "a/b/c/manifest.json"
    broken.parent.mkdir(parents=True)
    broken.write_text(manifest_path.read_text(encoding="utf-8"), encoding="utf-8")
    copied = load_stage5_manifest(broken)
    assert copied.stage4_implementation_status == "INTEGRATED"
    mismatched = root / "src/novel_agent/runtime/stage5_development_manifest.json"
    payload = mismatched.read_text(encoding="utf-8").replace(
        '"stage2_schema_fingerprint": "sha256:7',
        '"stage2_schema_fingerprint": "sha256:0',
        1,
    )
    mismatch_path = tmp_path / "stage5-provenance-only.json"
    mismatch_path.write_text(payload, encoding="utf-8")
    assert load_stage5_manifest(mismatch_path).stage2_schema_fingerprint.startswith("sha256:0")
    invalid = tmp_path / "invalid.json"
    invalid.write_text("not-json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="missing or invalid"):
        load_stage5_manifest(invalid)

    async def scenario(name: str) -> Stage5ScenarioEvidence:
        return Stage5ScenarioEvidence(
            scenario_id=name,
            passed=True,
            evidence_hash=ArtifactId("sha256:" + hashlib.sha256(name.encode()).hexdigest()),
            reason="passed",
        )

    callbacks: dict[str, Callable[[], Awaitable[Stage5ScenarioEvidence]]] = {
        name: partial(scenario, name) for name in REQUIRED_SCENARIOS
    }
    report = asyncio.run(
        IsolatedRuntimeEvaluator().evaluate(
            callbacks,
            executable_commit="a" * 40,
            manifest_fingerprint=ArtifactId("sha256:" + "f" * 64),
        )
    )
    assert report.status is IsolatedKernelStatus.PASS
    failed_callbacks = dict(callbacks)

    async def failed(name: str) -> Stage5ScenarioEvidence:
        return Stage5ScenarioEvidence(
            scenario_id=name,
            passed=False,
            evidence_hash=ArtifactId("sha256:" + hashlib.sha256(name.encode()).hexdigest()),
            reason="failed",
        )

    failed_callbacks["three_chapter_topology"] = lambda: failed("three_chapter_topology")
    assert (
        asyncio.run(
            IsolatedRuntimeEvaluator().evaluate(
                failed_callbacks,
                executable_commit="a" * 40,
                manifest_fingerprint=ArtifactId("sha256:" + "f" * 64),
            )
        ).status
        is IsolatedKernelStatus.FAILED
    )
    wrong_callbacks = dict(callbacks)
    wrong_callbacks["three_chapter_topology"] = lambda: scenario("wrong")
    with pytest.raises(ValueError, match="identities"):
        asyncio.run(
            IsolatedRuntimeEvaluator().evaluate(
                wrong_callbacks,
                executable_commit="a" * 40,
                manifest_fingerprint=ArtifactId("sha256:" + "f" * 64),
            )
        )
    with pytest.raises(ValueError, match="registry mismatch"):
        asyncio.run(
            IsolatedRuntimeEvaluator().evaluate(
                {},
                executable_commit="a" * 40,
                manifest_fingerprint=ArtifactId("sha256:" + "f" * 64),
            )
        )
