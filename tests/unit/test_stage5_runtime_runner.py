"""Deterministic coverage for the Stage 5 isolated runner, audit report, and assembly gates."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.adapters.postgres.runtime import RuntimeTaskQueryRepository
from novel_agent.adapters.runtime.isolated import (
    StrictDeterministicCandidateMaterializer,
    StrictFakePlanningLeaf,
)
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.creative_runtime import (
    AutomationMode,
    CandidateKind,
    CreativeRunPolicy,
    CreativeRunRequest,
    CreativeRunResult,
    CreativeRunTerminal,
)
from novel_agent.domain.generation import WritingLoopRequest
from novel_agent.domain.ids import (
    CommitId,
    ProjectId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
)
from novel_agent.domain.memory import DerivedBuildStatus, DerivedSnapshotLite
from novel_agent.domain.runtime import (
    EffectReceipt,
    EffectStatus,
    TaskKind,
    TaskPurpose,
    TaskRecord,
    TaskStatus,
)
from novel_agent.domain.stage5_manifest import Stage5DevelopmentManifest, load_stage5_manifest
from novel_agent.ports.creative_runtime import WritingLeafPort
from novel_agent.runtime.creative_assembly import validate_runtime_assembly
from novel_agent.runtime.creative_dispatcher import CreativeDispatcher
from novel_agent.runtime.isolated_runner import IsolatedRuntimeRunner
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.creative_runtime import CreativeRuntimeService
from novel_agent.services.event_log import RunEventLogRepository
from novel_agent.services.projection import (
    DerivedProjectionService,
    DerivedSnapshotRepository,
    ProjectionOutboxRepository,
)
from novel_agent.services.runtime_acceptance import RuntimeAcceptanceService
from novel_agent.services.runtime_commands import RuntimeCommandService
from novel_agent.services.runtime_reporting import RuntimeReportService
from tests.factories import make_manifest

HASH = "sha256:" + "1" * 64
PERMISSION_HASH = "sha256:" + "2" * 64
NOW = datetime(2026, 8, 10, tzinfo=UTC)


class _WritingRequest:
    def __init__(self, task: TaskRecord) -> None:
        self.run_id = task.run_id
        self.task_id = task.task_id
        self.base_commit = task.basis_commit
        self.snapshot_id = task.basis_snapshot


class _WritingResult:
    def __init__(self, ref: ArtifactRef) -> None:
        self.status = "draft_candidate_ready"
        self.final_candidate_id = ref.artifact_id
        self.final_text_artifact = ref
        self.artifacts = (ref,)


class _Writer:
    is_fixture = False

    def __init__(self, artifacts: ArtifactRepository) -> None:
        self._artifacts = artifacts

    async def run(self, request: WritingLoopRequest) -> object:
        ref = self._artifacts.put(
            request.task_id.root.encode(), "text/plain", SchemaVersion("1.0.0")
        )
        return _WritingResult(ref)


class _ProjectionBuilder:
    def build(self, project_id: ProjectId, source_commit: CommitId) -> DerivedSnapshotLite:
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
            published_at=NOW,
        )


def _manifest() -> Stage5DevelopmentManifest:
    root = Path(__file__).parents[2]
    return load_stage5_manifest(root / "src/novel_agent/runtime/stage5_development_manifest.json")


@pytest.fixture
def runner_kernel(
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
        factory, RunEventLogRepository(factory), lambda _project_id: PERMISSION_HASH
    )
    yield factory, commits, artifacts, commands, base
    engine.dispose()


def _request(base: CommitId, *, run_id: str = "run.runner") -> CreativeRunRequest:
    return CreativeRunRequest(
        run_id=RunId(run_id),
        project_id=ProjectId("project.test"),
        basis_commit=base,
        policy=CreativeRunPolicy(
            automation_mode=AutomationMode.MANUAL,
            policy_hash=HASH,
            permission_hash=PERMISSION_HASH,
        ),
    )


def test_validate_runtime_assembly_rejects_bad_production_and_isolated_mixes() -> None:
    from novel_agent.domain.stage5_manifest import Stage5FeatureAdmission

    manifest = _manifest()
    deferred = manifest.model_copy(
        update={
            "stage4_implementation_status": "DEFERRED",
            "feature_admission": manifest.feature_admission.model_copy(
                update={"real_stage4_adapter": False}
            ),
        }
    )
    admitted = Stage5DevelopmentManifest.model_construct(  # type: ignore[call-arg]
        feature_admission=Stage5FeatureAdmission(real_stage4_adapter=True)
    )
    real = object()

    class _Fixture:
        is_fixture = True

    with pytest.raises(RuntimeError, match="real Stage 4"):
        validate_runtime_assembly(
            deferred,
            planner=real,
            writer=real,
            plan_materializer=real,
            draft_materializer=real,
            production=True,
        )
    with pytest.raises(RuntimeError, match="rejects fixture"):
        validate_runtime_assembly(
            admitted,
            planner=_Fixture(),
            writer=real,
            plan_materializer=real,
            draft_materializer=real,
            production=True,
        )
    # Admitted production assembly with real components passes.
    validate_runtime_assembly(
        admitted,
        planner=real,
        writer=real,
        plan_materializer=real,
        draft_materializer=real,
        production=True,
    )
    with pytest.raises(RuntimeError, match="strict fake Planner"):
        validate_runtime_assembly(
            manifest,
            planner=real,
            writer=real,
            plan_materializer=real,
            draft_materializer=real,
            production=False,
        )
    with pytest.raises(RuntimeError, match="real Stage 3 Writer"):
        validate_runtime_assembly(
            manifest,
            planner=_Fixture(),
            writer=_Fixture(),
            plan_materializer=_Fixture(),
            draft_materializer=_Fixture(),
            production=False,
        )
    validate_runtime_assembly(
        manifest,
        planner=_Fixture(),
        writer=real,
        plan_materializer=_Fixture(),
        draft_materializer=_Fixture(),
        production=False,
    )


def test_isolated_runner_enforces_bounded_run_policy(
    runner_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, commits, artifacts, commands, base = runner_kernel
    policy = CreativeRunPolicy(
        automation_mode=AutomationMode.MANUAL,
        policy_hash=HASH,
        permission_hash=PERMISSION_HASH,
        max_tasks_per_advance=3,
    )
    plan_materializer = StrictDeterministicCandidateMaterializer(
        commits, candidate_kind=CandidateKind.PLAN
    )
    draft_materializer = StrictDeterministicCandidateMaterializer(
        commits, candidate_kind=CandidateKind.DRAFT
    )
    writer = cast(WritingLeafPort, _Writer(artifacts))
    runtime = CreativeRuntimeService(
        commands,
        RuntimeAcceptanceService(commands, commits, artifacts),
        commits,
        artifacts,
        StrictFakePlanningLeaf(artifacts),
        writer,
        lambda task: cast(WritingLoopRequest, _WritingRequest(task)),
        plan_materializer,
        draft_materializer,
        DerivedProjectionService(ProjectionOutboxRepository(factory), _ProjectionBuilder()),
        DerivedSnapshotRepository(factory),
        lambda policy_hash: policy
        if policy_hash == policy.policy_hash
        else (_ for _ in ()).throw(KeyError(policy_hash)),
    )
    dispatcher = CreativeDispatcher(
        RuntimeTaskQueryRepository(factory),
        runtime,
        worker_id="runner",
    )
    runner = IsolatedRuntimeRunner(
        runtime,
        dispatcher,
        _manifest(),
        planner=StrictFakePlanningLeaf(artifacts),
        writer=writer,
        plan_materializer=plan_materializer,
        draft_materializer=draft_materializer,
    )
    with pytest.raises(ValueError, match="runner task budget"):
        asyncio.run(runner.run_bounded(_request(base), max_tasks=99))
    with pytest.raises(ValueError, match="runner task budget"):
        asyncio.run(runner.run_bounded(_request(base), max_tasks=0))
    bounded_request = _request(base).model_copy(update={"policy": policy})
    results = asyncio.run(runner.run_bounded(bounded_request, max_tasks=2))
    assert results and results[0].terminal is CreativeRunTerminal.PROGRESSED


def test_dispatcher_run_bounded_consumes_full_budget_without_break() -> None:
    from novel_agent.domain.ids import ProjectId as _ProjectId
    from novel_agent.domain.ids import RunId as _RunId
    from novel_agent.domain.ids import TaskId as _TaskId

    class _EveryReady:
        def __init__(self) -> None:
            self.calls = 0

        def next_ready(
            self,
            *,
            project_id: _ProjectId | None = None,
            run_id: _RunId | None = None,
        ) -> _TaskId | None:
            self.calls += 1
            return _TaskId(f"task.bounded.{self.calls}")

    class _EveryProgress:
        async def advance(self, task_id: _TaskId, *, worker_id: str) -> CreativeRunResult:
            return CreativeRunResult(
                run_id=RunId("run.bounded"),
                project_id=ProjectId("project.test"),
                terminal=CreativeRunTerminal.PROGRESSED,
                current_task_id=task_id,
                basis_commit=CommitId("sha256:" + "1" * 64),
                current_commit=CommitId("sha256:" + "1" * 64),
                reason_code=worker_id,
            )

    from novel_agent.adapters.postgres.runtime import RuntimeTaskQueryRepository
    from novel_agent.services.creative_runtime import CreativeRuntimeService as _S

    dispatcher = CreativeDispatcher(
        cast(RuntimeTaskQueryRepository, _EveryReady()),
        cast(_S, _EveryProgress()),
        worker_id="bounded",
    )
    results = asyncio.run(dispatcher.run_bounded(max_tasks=3))
    assert len(results) == 3
    with pytest.raises(ValueError, match="positive"):
        asyncio.run(dispatcher.run_bounded(max_tasks=0))


def test_dispatcher_overlaps_only_current_draft_and_planner_lookahead() -> None:
    basis = CommitId("sha256:" + "1" * 64)

    def task(identity: str, kind: TaskKind, purpose: TaskPurpose) -> TaskRecord:
        return TaskRecord(
            task_id=TaskId(identity),
            run_id=RunId("run.parallel"),
            project_id=ProjectId("project.test"),
            kind=kind,
            purpose=purpose,
            task_revision=0,
            status=TaskStatus.READY,
            basis_commit=basis,
            basis_snapshot=StableId("snapshot.parallel"),
            policy_hash=HASH,
            permission_hash=PERMISSION_HASH,
            chapter_index=1,
            target_chapters=3,
            horizon_start=(2 if purpose is TaskPurpose.LOOKAHEAD else None),
            horizon_end=(3 if purpose is TaskPurpose.LOOKAHEAD else None),
            protected_chapter_index=(1 if purpose is TaskPurpose.LOOKAHEAD else None),
        )

    ready = (
        task("task.parallel.draft", TaskKind.DRAFT_CANDIDATE, TaskPurpose.NORMAL),
        task("task.parallel.lookahead", TaskKind.PLAN_CANDIDATE, TaskPurpose.LOOKAHEAD),
    )

    class _Batch:
        def __init__(self) -> None:
            self.used = False

        def ready_batch(self, **_: object) -> tuple[TaskRecord, ...]:
            if self.used:
                return ()
            self.used = True
            return ready

    class _Overlap:
        def __init__(self) -> None:
            self.in_flight = 0
            self.max_in_flight = 0
            self.both_started = asyncio.Event()

        async def advance(self, task_id: TaskId, *, worker_id: str) -> CreativeRunResult:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            if self.in_flight == 2:
                self.both_started.set()
            await self.both_started.wait()
            self.in_flight -= 1
            return CreativeRunResult(
                run_id=RunId("run.parallel"),
                project_id=ProjectId("project.test"),
                terminal=CreativeRunTerminal.PROGRESSED,
                current_task_id=task_id,
                basis_commit=basis,
                current_commit=basis,
                reason_code=worker_id,
            )

    overlap = _Overlap()
    dispatcher = CreativeDispatcher(
        cast(RuntimeTaskQueryRepository, _Batch()),
        cast(CreativeRuntimeService, overlap),
        worker_id="parallel",
        parallelism=2,
    )
    results = asyncio.run(dispatcher.run_bounded(max_tasks=2))
    assert len(results) == 2
    assert overlap.max_in_flight == 2

    normal_plan = task("task.parallel.normal-plan", TaskKind.PLAN_CANDIDATE, TaskPurpose.NORMAL)
    assert CreativeDispatcher._parallel_batch((ready[0], normal_plan)) == (ready[0],)


def test_audit_report_derives_from_durable_truth(
    runner_kernel: tuple[
        sessionmaker[Session],
        CommitService,
        ArtifactRepository,
        RuntimeCommandService,
        CommitId,
    ],
) -> None:
    factory, _, artifacts, commands, base = runner_kernel
    events = RunEventLogRepository(factory)
    manifest_path = (
        Path(__file__).parents[2] / "src/novel_agent/runtime/stage5_development_manifest.json"
    )
    task = commands.create_run_and_initial_task(_request(base, run_id="run.report"))
    _, fence = commands.claim(task.task_id, worker_id="worker")
    commands.mark_started(fence)
    artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0"))
    report = RuntimeReportService(factory, events).export(
        task.run_id,
        manifest_path=manifest_path,
        executable_commit="a" * 40,
    )
    assert report.run_id == task.run_id
    assert report.manifest_fingerprint.root.startswith("sha256:")
    assert report.effects == ()
    assert report.skill_hashes == ()
    assert report.active_feature_flags == ("real_stage4_adapter",)
    assert report.model_request_count == 0
    with factory() as session, session.begin():
        from novel_agent.adapters.postgres.models import RuntimeEffectProjectionRow

        row = RuntimeEffectProjectionRow(
            effect_identity="effect.report",
            request_identity="request.report",
            run_id=task.run_id.root,
            task_id=task.task_id.root,
            attempt_id=fence.attempt_id.root,
            status=EffectStatus.REQUESTED.value,
            provider_request_id=None,
            result_ref_json=None,
            effect_json=EffectReceipt(
                effect_identity=StableId("effect.report"),
                external_system="provider",
                request_identity=StableId("request.report"),
                status=EffectStatus.REQUESTED,
                attempt_no=1,
            ).model_dump(mode="json"),
        )
        session.add(row)
    report_with_effect = RuntimeReportService(factory, events).export(
        task.run_id,
        manifest_path=manifest_path,
        executable_commit="a" * 40,
    )
    assert len(report_with_effect.effects) == 1
    assert report_with_effect.model_request_count == 0
