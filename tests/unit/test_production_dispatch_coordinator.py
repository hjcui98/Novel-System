from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from novel_agent.domain.creative_runtime import (
    AutomationMode,
    CreativeRunPolicy,
    CreativeRunRequest,
)
from novel_agent.domain.ids import CommitId, ProjectId, RunId, TaskId
from novel_agent.domain.runtime import TaskKind, TaskRecord, TaskStatus
from novel_agent.domain.stage5_evaluation import Stage5VerticalRunReport, VerticalRunStatus
from novel_agent.domain.stage5_manifest import load_stage5_manifest
from novel_agent.runtime.production_dispatch_coordinator import (
    ProductionDispatchCoordinator,
    ProductionRunDescriptor,
    load_production_run_descriptors,
)
from novel_agent.services.model_request_admission import ModelRequestAdmissionController

HASH = "sha256:" + "1" * 64
PERMISSION = "sha256:" + "2" * 64
BASE = CommitId("sha256:" + "3" * 64)
MANIFEST = Path(__file__).parents[2] / "src/novel_agent/runtime/stage5_development_manifest.json"


def _policy(**updates: object) -> CreativeRunPolicy:
    payload: dict[str, object] = {
        "automation_mode": AutomationMode.AUTO,
        "policy_hash": HASH,
        "permission_hash": PERMISSION,
        "auto_accept_plan": True,
        "auto_accept_draft": True,
    }
    payload.update(updates)
    return CreativeRunPolicy.model_validate(payload)


def _request(suffix: str, policy: CreativeRunPolicy | None = None) -> CreativeRunRequest:
    return CreativeRunRequest(
        run_id=RunId(f"run.{suffix}"),
        project_id=ProjectId(f"project.{suffix}"),
        basis_commit=BASE,
        policy=policy or _policy(),
    )


def _descriptor(tmp_path: Path, suffix: str, **updates: object) -> ProductionRunDescriptor:
    policy = cast(CreativeRunPolicy, updates.pop("policy", _policy()))
    request = cast(CreativeRunRequest | None, updates.pop("request", _request(suffix, policy)))
    payload: dict[str, object] = {
        "project_id": ProjectId(f"project.{suffix}"),
        "run_id": RunId(f"run.{suffix}"),
        "object_store_root": tmp_path / suffix,
        "policy": policy,
        "max_tasks": 1,
        "request": request,
    }
    payload.update(updates)
    return ProductionRunDescriptor(**payload)  # type: ignore[arg-type]


def _report(
    request: CreativeRunRequest,
    status: VerticalRunStatus,
) -> Stage5VerticalRunReport:
    return Stage5VerticalRunReport(
        run_id=request.run_id,
        project_id=request.project_id,
        current_chapter=request.current_chapter,
        target_chapter=request.target_chapters,
        status=status,
        final_commit=request.basis_commit,
        completed_chapters=() if status is not VerticalRunStatus.COMPLETED else (21,),
        runtime_results=(),
        tasks=(),
        outputs_frozen=status is VerticalRunStatus.COMPLETED,
    )


def _manifest() -> Any:
    return load_stage5_manifest(MANIFEST)


class _FakeReader:
    def __init__(self, tasks: tuple[TaskRecord, ...] = ()) -> None:
        self._tasks = tasks

    def list_run(self, run_id: RunId) -> tuple[TaskRecord, ...]:
        del run_id
        return self._tasks

    def next_scheduled_at(self, **_: object) -> datetime | None:
        now = datetime.now(UTC)
        times = tuple(
            task.scheduled_for
            for task in self._tasks
            if task.scheduled_for is not None and task.scheduled_for > now
        )
        return min(times) if times else None

    def future_scheduled_count(self, **_: object) -> int:
        now = datetime.now(UTC)
        return sum(
            1 for task in self._tasks if task.scheduled_for is not None and task.scheduled_for > now
        )


def _loader_for(
    admissions: list[ModelRequestAdmissionController],
    tasks: dict[str, tuple[TaskRecord, ...]] | None = None,
) -> Any:
    def _load(spec: str, context: Any) -> SimpleNamespace:
        del spec
        admissions.append(context.admission)
        return SimpleNamespace(
            runtime=object(),
            dispatcher=object(),
            task_reader=_FakeReader((tasks or {}).get(context.run_id.root, ())),
            admission=context.admission,
        )

    return _load


def test_load_production_run_descriptors_resolves_relative_paths(tmp_path: Path) -> None:
    policy = _policy()
    (tmp_path / "policy.json").write_text(policy.model_dump_json(), encoding="utf-8")
    (tmp_path / "runs.json").write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "project_id": "project.a",
                        "run_id": "run.a",
                        "object_store_root": "objects-a",
                        "policy": "policy.json",
                        "max_tasks": 2,
                        "max_slices": 8,
                        "stop_after_chapter": 21,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    descriptors = load_production_run_descriptors(tmp_path / "runs.json")
    assert len(descriptors) == 1
    assert descriptors[0].object_store_root == tmp_path / "objects-a"
    assert descriptors[0].max_tasks == 2
    assert descriptors[0].stop_after_chapter == 21
    (tmp_path / "bad-runs.json").write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "project_id": "project.a",
                        "run_id": "run.a",
                        "object_store_root": "objects-a",
                        "policy": "policy.json",
                        "max_tasks": "two",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="integer field"):
        load_production_run_descriptors(tmp_path / "bad-runs.json")
    (tmp_path / "timeout-runs.json").write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "project_id": "project.a",
                        "run_id": "run.a",
                        "object_store_root": "objects-a",
                        "policy": "policy.json",
                        "settlement_timeout_seconds": "slow",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="number field"):
        load_production_run_descriptors(tmp_path / "timeout-runs.json")
    (tmp_path / "campaign-runs.json").write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "project_id": "project.a",
                        "run_id": "run.a",
                        "object_store_root": "objects-a",
                        "policy": "policy.json",
                        "settlement_timeout_seconds": 90,
                        "settlement_output_tokens": 12000,
                        "settlement_token_budget": 128000,
                        "max_major_rewrites": 2,
                        "max_local_repairs": 1,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    campaign = load_production_run_descriptors(tmp_path / "campaign-runs.json")[0]
    assert campaign.settlement_timeout_seconds == 90.0
    assert campaign.settlement_output_tokens == 12000
    assert campaign.settlement_token_budget == 128000
    assert campaign.max_major_rewrites == 2
    assert campaign.max_local_repairs == 1


def test_coordinator_shares_admission_and_does_not_cancel_sibling_projects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admissions: list[ModelRequestAdmissionController] = []
    seen: list[str] = []

    class _Runner:
        def __init__(self, **_: object) -> None:
            return None

        async def run(self, request: CreativeRunRequest, **_: object) -> Stage5VerticalRunReport:
            seen.append(request.project_id.root)
            if request.project_id.root.endswith(".boom"):
                raise RuntimeError("injected project crash")
            return _report(request, VerticalRunStatus.COMPLETED)

    monkeypatch.setattr(
        "novel_agent.runtime.production_dispatch_coordinator.VerticalCreativeRunner",
        _Runner,
    )
    coordinator = ProductionDispatchCoordinator(
        database_url="sqlite+pysqlite:///:memory:",
        manifest=_manifest(),
        runs=(
            _descriptor(tmp_path, "ok"),
            _descriptor(tmp_path, "boom"),
        ),
        model_endpoints=(),
        project_parallelism=2,
        endpoint_request_limit=2,
        assembly_loader=_loader_for(admissions),
    )
    result = asyncio.run(coordinator.run_once())
    assert {item.status for item in result.per_project} == {"completed", "failed"}
    assert result.status == "failed"
    assert admissions[0] is admissions[1] is coordinator.admission
    assert set(seen) == {"project.ok", "project.boom"}
    snapshot = coordinator.admission.snapshot()
    assert snapshot["acquired_requests"] == snapshot["released_requests"]
    assert snapshot["inflight_requests"] == 0
    assert result.endpoint_request_limit == 2
    assert result.project_parallelism == 2


def test_coordinator_bounds_project_parallelism(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = 0
    peak = 0

    class _Runner:
        def __init__(self, **_: object) -> None:
            return None

        async def run(self, request: CreativeRunRequest, **_: object) -> Stage5VerticalRunReport:
            nonlocal current, peak
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.05)
            current -= 1
            return _report(request, VerticalRunStatus.COMPLETED)

    monkeypatch.setattr(
        "novel_agent.runtime.production_dispatch_coordinator.VerticalCreativeRunner",
        _Runner,
    )
    coordinator = ProductionDispatchCoordinator(
        database_url="sqlite+pysqlite:///:memory:",
        manifest=_manifest(),
        runs=tuple(_descriptor(tmp_path, suffix) for suffix in ("a", "b", "c")),
        model_endpoints=(),
        project_parallelism=2,
        assembly_loader=_loader_for([]),
    )
    result = asyncio.run(coordinator.run_once())
    assert result.status == "completed"
    assert peak == 2
    assert result.all_projects_terminal is True


def test_watch_waits_for_durable_schedule_then_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    scheduled = TaskRecord(
        task_id=TaskId("task.future"),
        run_id=RunId("run.watch"),
        project_id=ProjectId("project.watch"),
        kind=TaskKind.PLAN_CANDIDATE,
        task_revision=0,
        status=TaskStatus.READY,
        basis_commit=BASE,
        policy_hash=HASH,
        permission_hash=PERMISSION,
        scheduled_for=future,
    )
    cycles = {"n": 0}

    class _Runner:
        def __init__(self, **_: object) -> None:
            return None

        async def run(self, request: CreativeRunRequest, **_: object) -> Stage5VerticalRunReport:
            cycles["n"] += 1
            status = VerticalRunStatus.WAITING if cycles["n"] == 1 else VerticalRunStatus.COMPLETED
            return _report(request, status)

    monkeypatch.setattr(
        "novel_agent.runtime.production_dispatch_coordinator.VerticalCreativeRunner",
        _Runner,
    )
    coordinator = ProductionDispatchCoordinator(
        database_url="sqlite+pysqlite:///:memory:",
        manifest=_manifest(),
        runs=(_descriptor(tmp_path, "watch"),),
        model_endpoints=(),
        assembly_loader=_loader_for([], {"run.watch": (scheduled,)}),
    )
    result = asyncio.run(coordinator.run_watch(poll_interval_seconds=0.01, max_cycles=2))
    assert cycles["n"] == 2
    assert result.status == "completed"
    assert result.remaining_scheduled_tasks == 1
    assert result.next_scheduled_at is not None


def test_watch_shutdown_releases_admission_leases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    admission = ModelRequestAdmissionController(endpoint_request_limit=1)

    class _Runner:
        def __init__(self, **_: object) -> None:
            return None

        async def run(self, request: CreativeRunRequest, **_: object) -> Stage5VerticalRunReport:
            del request
            lease = admission.acquire(1)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                lease.release()
            raise AssertionError("cancelled runner should not complete")

    monkeypatch.setattr(
        "novel_agent.runtime.production_dispatch_coordinator.VerticalCreativeRunner",
        _Runner,
    )
    coordinator = ProductionDispatchCoordinator(
        database_url="sqlite+pysqlite:///:memory:",
        manifest=_manifest(),
        runs=(_descriptor(tmp_path, "hold"),),
        model_endpoints=(),
        admission=admission,
        assembly_loader=_loader_for([]),
    )

    async def _cancel() -> None:
        task = asyncio.create_task(coordinator.run_once())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_cancel())
    snapshot = admission.snapshot()
    assert snapshot["inflight_requests"] == 0
    assert snapshot["acquired_requests"] == snapshot["released_requests"]


def test_scheduled_summary_falls_back_to_list_run_and_watch_stop_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    future = datetime.now(UTC) + timedelta(hours=1)

    class _ListOnlyReader:
        def list_run(self, run_id: RunId) -> tuple[TaskRecord, ...]:
            del run_id
            return (
                TaskRecord(
                    task_id=TaskId("task.listed"),
                    run_id=RunId("run.listed"),
                    project_id=ProjectId("project.listed"),
                    kind=TaskKind.PLAN_CANDIDATE,
                    task_revision=0,
                    status=TaskStatus.READY,
                    basis_commit=BASE,
                    policy_hash=HASH,
                    permission_hash=PERMISSION,
                    scheduled_for=future,
                ),
            )

    class _Runner:
        def __init__(self, **_: object) -> None:
            return None

        async def run(self, request: CreativeRunRequest, **_: object) -> Stage5VerticalRunReport:
            return _report(request, VerticalRunStatus.WAITING)

    monkeypatch.setattr(
        "novel_agent.runtime.production_dispatch_coordinator.VerticalCreativeRunner",
        _Runner,
    )

    def _load(spec: str, context: Any) -> Any:
        del spec, context
        return SimpleNamespace(
            runtime=object(),
            dispatcher=object(),
            task_reader=_ListOnlyReader(),
        )

    coordinator = ProductionDispatchCoordinator(
        database_url="sqlite+pysqlite:///:memory:",
        manifest=_manifest(),
        runs=(_descriptor(tmp_path, "listed"),),
        model_endpoints=(),
        assembly_loader=_load,
    )
    stop = asyncio.Event()
    stop.set()
    result = asyncio.run(coordinator.run_watch(poll_interval_seconds=0.01, stop_event=stop))
    assert result.status == "waiting"
    assert result.remaining_scheduled_tasks == 1
    assert result.next_scheduled_at == future
    with pytest.raises(ValueError, match="poll interval"):
        asyncio.run(coordinator.run_watch(poll_interval_seconds=0))


def test_coordinator_rejects_invalid_construction(tmp_path: Path) -> None:
    admission = ModelRequestAdmissionController(endpoint_request_limit=2)
    with pytest.raises(ValueError, match="at least one run"):
        ProductionDispatchCoordinator(
            database_url="sqlite+pysqlite:///:memory:",
            manifest=_manifest(),
            runs=(),
            model_endpoints=(),
        )
    with pytest.raises(ValueError, match="cannot be combined"):
        ProductionDispatchCoordinator(
            database_url="sqlite+pysqlite:///:memory:",
            manifest=_manifest(),
            runs=(_descriptor(tmp_path, "mix"),),
            model_endpoints=(),
            admission=admission,
            endpoint_request_limit=2,
        )


def test_coordinator_records_assembly_failure_and_blocked_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Runner:
        def __init__(self, **_: object) -> None:
            return None

        async def run(self, request: CreativeRunRequest, **_: object) -> Stage5VerticalRunReport:
            return _report(request, VerticalRunStatus.BLOCKED)

    monkeypatch.setattr(
        "novel_agent.runtime.production_dispatch_coordinator.VerticalCreativeRunner",
        _Runner,
    )

    def _load(spec: str, context: Any) -> Any:
        del spec
        if context.run_id.root.endswith(".missing"):
            raise RuntimeError("assembly boom")
        return SimpleNamespace(
            runtime=object(),
            dispatcher=object(),
            task_reader=_FakeReader(),
        )

    coordinator = ProductionDispatchCoordinator(
        database_url="sqlite+pysqlite:///:memory:",
        manifest=_manifest(),
        runs=(_descriptor(tmp_path, "blocked"), _descriptor(tmp_path, "missing")),
        model_endpoints=(),
        assembly_loader=_load,
    )
    result = asyncio.run(coordinator.run_once())
    statuses = {item.run_id.root: item.status for item in result.per_project}
    assert statuses["run.blocked"] == "blocked"
    assert statuses["run.missing"] == "assembly_failed"
    assert result.status == "failed"
