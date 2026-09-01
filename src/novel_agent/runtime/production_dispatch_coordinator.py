"""One-process production dispatch across independent project assemblies."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from novel_agent.domain.creative_runtime import CreativeRunPolicy, CreativeRunRequest
from novel_agent.domain.ids import ProjectId, RunId
from novel_agent.domain.runtime import TaskStatus
from novel_agent.domain.stage5_evaluation import Stage5VerticalRunReport, VerticalRunStatus
from novel_agent.domain.stage5_manifest import Stage5DevelopmentManifest
from novel_agent.runtime.creative_assembly import (
    DEFAULT_PRODUCTION_ASSEMBLY_FACTORY,
    ProductionAssemblyContext,
    ProductionRuntimeAssembly,
    load_production_runtime_assembly,
)
from novel_agent.runtime.vertical_runner import DispatchTaskBudget, VerticalCreativeRunner
from novel_agent.services.model_gateway import RegisteredModelEndpoint
from novel_agent.services.model_request_admission import ModelRequestAdmissionController


@dataclass(frozen=True, slots=True)
class ProductionRunDescriptor:
    """Durable identity and bounds for one project/run dispatch lane."""

    project_id: ProjectId
    run_id: RunId
    object_store_root: Path
    policy: CreativeRunPolicy
    max_tasks: int = 1
    max_slices: int | None = 200
    request: CreativeRunRequest | None = None
    stop_after_chapter: int | None = None
    settlement_timeout_seconds: float | None = None
    settlement_output_tokens: int | None = None
    max_major_rewrites: int | None = None
    max_local_repairs: int | None = None

    def __post_init__(self) -> None:
        if self.max_tasks < 1:
            raise ValueError("production run max_tasks must be positive")
        if self.max_slices is not None and self.max_slices < 1:
            raise ValueError("production run max_slices must be positive")
        if self.request is not None and (
            self.request.project_id != self.project_id or self.request.run_id != self.run_id
        ):
            raise ValueError("production run request identity does not match its descriptor")
        if self.request is not None and self.request.policy.policy_hash != self.policy.policy_hash:
            raise ValueError("production run request policy does not match its descriptor")

    def with_runtime_options(
        self,
        *,
        runtime_parallelism: int | None = None,
        planner_lookahead: bool | None = None,
    ) -> ProductionRunDescriptor:
        updates: dict[str, object] = {}
        if runtime_parallelism is not None:
            updates["runtime_parallelism"] = runtime_parallelism
        if planner_lookahead is not None:
            updates["enable_planner_lookahead"] = planner_lookahead
        if not updates:
            return self
        policy = CreativeRunPolicy.model_validate(
            {**self.policy.model_dump(mode="json"), **updates}, strict=False
        )
        request = (
            None if self.request is None else self.request.model_copy(update={"policy": policy})
        )
        return replace(self, policy=policy, request=request)

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, object],
        *,
        base_dir: Path,
    ) -> ProductionRunDescriptor:
        project_id = ProjectId(str(payload["project_id"]))
        run_id = RunId(str(payload["run_id"]))
        object_store_value = str(payload["object_store_root"])
        object_store_root = Path(object_store_value)
        if not object_store_root.is_absolute():
            object_store_root = base_dir / object_store_root
        policy = _load_policy(payload["policy"], base_dir)
        request_value = payload.get("request")
        request = None if request_value is None else _load_request(request_value, base_dir)
        return cls(
            project_id=project_id,
            run_id=run_id,
            object_store_root=object_store_root,
            policy=policy,
            max_tasks=_payload_int(payload.get("max_tasks", 1)),
            max_slices=(
                None
                if payload.get("max_slices") is None and "max_slices" in payload
                else _payload_int(payload.get("max_slices", 200))
            ),
            request=request,
            stop_after_chapter=_optional_int(payload.get("stop_after_chapter")),
            settlement_timeout_seconds=_optional_float(payload.get("settlement_timeout_seconds")),
            settlement_output_tokens=_optional_int(payload.get("settlement_output_tokens")),
            max_major_rewrites=_optional_int(payload.get("max_major_rewrites")),
            max_local_repairs=_optional_int(payload.get("max_local_repairs")),
        )


@dataclass(frozen=True, slots=True)
class ProductionProjectDispatchResult:
    project_id: ProjectId
    run_id: RunId
    status: str
    report: Stage5VerticalRunReport | None = None
    error_type: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "project_id": self.project_id.root,
            "run_id": self.run_id.root,
            "status": self.status,
            "error_type": self.error_type,
            "report": None if self.report is None else self.report.model_dump(mode="json"),
        }


@dataclass(frozen=True, slots=True)
class ProductionDispatchResult:
    status: str
    runtime_parallelism: int | None
    planner_lookahead: bool | None
    project_parallelism: int
    endpoint_request_limit: int
    configured_kv_token_budget: int | None
    effective_kv_token_budget: int | None
    queue_depth: int
    max_inflight_requests: int
    total_wait_seconds: float
    scheduling_timeouts: int
    acquired_requests: int
    released_requests: int
    per_project: tuple[ProductionProjectDispatchResult, ...]
    remaining_scheduled_tasks: int
    next_scheduled_at: datetime | None
    all_projects_terminal: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "runtime_parallelism": self.runtime_parallelism,
            "planner_lookahead": self.planner_lookahead,
            "project_parallelism": self.project_parallelism,
            "endpoint_request_limit": self.endpoint_request_limit,
            "configured_kv_token_budget": self.configured_kv_token_budget,
            "effective_kv_token_budget": self.effective_kv_token_budget,
            "queue_depth": self.queue_depth,
            "max_inflight_requests": self.max_inflight_requests,
            "total_wait_seconds": self.total_wait_seconds,
            "scheduling_timeouts": self.scheduling_timeouts,
            "acquired_requests": self.acquired_requests,
            "released_requests": self.released_requests,
            "per_project_status": [
                {
                    "project_id": item.project_id.root,
                    "run_id": item.run_id.root,
                    "status": item.status,
                }
                for item in self.per_project
            ],
            "projects": [item.to_payload() for item in self.per_project],
            "remaining_scheduled_tasks": self.remaining_scheduled_tasks,
            "next_scheduled_time": (
                None if self.next_scheduled_at is None else self.next_scheduled_at.isoformat()
            ),
            "all_projects_terminal": self.all_projects_terminal,
        }


class _GlobalDispatchTaskBudget:
    def __init__(self, maximum: int | None) -> None:
        self._remaining = maximum

    def reserve(self, requested: int) -> int:
        if requested < 1:
            raise ValueError("dispatch budget reservation must be positive")
        if self._remaining is None:
            return requested
        reserved = min(requested, self._remaining)
        self._remaining -= reserved
        return reserved

    def release_unused(self, reserved: int, consumed: int) -> None:
        if reserved < 0 or consumed < 0 or consumed > reserved:
            raise ValueError("invalid dispatch budget release")
        if self._remaining is not None:
            self._remaining += reserved - consumed


class ProductionDispatchCoordinator:
    """Coordinate bounded per-project runners while sharing one admission controller."""

    def __init__(
        self,
        *,
        database_url: str,
        manifest: Stage5DevelopmentManifest,
        runs: Sequence[ProductionRunDescriptor],
        model_endpoints: tuple[RegisteredModelEndpoint, ...],
        assembly_factory: str = DEFAULT_PRODUCTION_ASSEMBLY_FACTORY,
        project_parallelism: int = 1,
        endpoint_request_limit: int = 1,
        kv_token_budget: int | None = None,
        kv_safety_reserve_ratio: float = 0.20,
        scheduling_timeout_seconds: float = 120.0,
        max_total_tasks: int | None = 100,
        worker_id: str = "production.dispatcher",
        admission: ModelRequestAdmissionController | None = None,
        assembly_loader: Callable[
            [str, ProductionAssemblyContext], ProductionRuntimeAssembly
        ] = load_production_runtime_assembly,
        retrieval_backend_profile: str = "memory",
        opensearch_url: str | None = None,
        embedding_url: str | None = None,
        reranker_url: str | None = None,
    ) -> None:
        if not runs:
            raise ValueError("production dispatch requires at least one run")
        if project_parallelism < 1:
            raise ValueError("project_parallelism must be positive")
        if endpoint_request_limit not in (1, 2):
            raise ValueError("production endpoint_request_limit must be 1 or 2")
        if max_total_tasks is not None and max_total_tasks < 1:
            raise ValueError("max_total_tasks must be positive")
        if admission is not None and (
            endpoint_request_limit != 1
            or kv_token_budget is not None
            or kv_safety_reserve_ratio != 0.20
            or scheduling_timeout_seconds != 120.0
        ):
            raise ValueError(
                "an explicit admission controller cannot be combined with admission overrides"
            )
        self._database_url = database_url
        self._manifest = manifest
        self._runs = tuple(runs)
        self._model_endpoints = model_endpoints
        self._assembly_factory = assembly_factory
        self._project_parallelism = project_parallelism
        self._endpoint_request_limit = endpoint_request_limit
        self._kv_token_budget = kv_token_budget
        self._kv_safety_reserve_ratio = kv_safety_reserve_ratio
        self._scheduling_timeout_seconds = scheduling_timeout_seconds
        self._max_total_tasks = max_total_tasks
        self._worker_id = worker_id
        self._admission = admission or ModelRequestAdmissionController(
            endpoint_request_limit=endpoint_request_limit,
            kv_token_budget=kv_token_budget,
            kv_safety_reserve_ratio=kv_safety_reserve_ratio,
            default_scheduling_timeout_seconds=scheduling_timeout_seconds,
        )
        actual_limit = cast(int, self._admission.snapshot()["endpoint_request_limit"])
        if actual_limit not in (1, 2):
            raise ValueError("production endpoint_request_limit must be 1 or 2")
        self._assembly_loader = assembly_loader
        self._retrieval_backend_profile = retrieval_backend_profile
        self._opensearch_url = opensearch_url
        self._embedding_url = embedding_url
        self._reranker_url = reranker_url
        self._assemblies: dict[tuple[ProjectId, RunId], ProductionRuntimeAssembly] = {}
        self._assembly_errors: dict[tuple[ProjectId, RunId], Exception] = {}

    @property
    def admission(self) -> ModelRequestAdmissionController:
        return self._admission

    @property
    def assemblies(self) -> dict[tuple[ProjectId, RunId], ProductionRuntimeAssembly]:
        return dict(self._assemblies)

    def _context(self, descriptor: ProductionRunDescriptor) -> ProductionAssemblyContext:
        suffix = f"{descriptor.project_id.root}.{descriptor.run_id.root}"
        return ProductionAssemblyContext(
            database_url=self._database_url,
            object_store_root=descriptor.object_store_root,
            project_id=descriptor.project_id,
            run_id=descriptor.run_id,
            policy=descriptor.policy,
            manifest=self._manifest,
            model_endpoints=self._model_endpoints,
            worker_id=f"{self._worker_id}.{suffix}"[:128],
            admission=self._admission,
            settlement_timeout_seconds=descriptor.settlement_timeout_seconds,
            settlement_output_tokens=descriptor.settlement_output_tokens,
            max_major_rewrites=descriptor.max_major_rewrites,
            max_local_repairs=descriptor.max_local_repairs,
            retrieval_backend_profile=self._retrieval_backend_profile,
            opensearch_url=self._opensearch_url,
            embedding_url=self._embedding_url,
            reranker_url=self._reranker_url,
        )

    def _ensure_assemblies(self) -> None:
        for descriptor in self._runs:
            key = (descriptor.project_id, descriptor.run_id)
            if key in self._assemblies or key in self._assembly_errors:
                continue
            try:
                assembly = self._assembly_loader(self._assembly_factory, self._context(descriptor))
                self._assemblies[key] = assembly
            except Exception as error:
                self._assembly_errors[key] = error

    def _request_for(
        self,
        descriptor: ProductionRunDescriptor,
        assembly: ProductionRuntimeAssembly,
    ) -> CreativeRunRequest:
        if descriptor.request is not None:
            return descriptor.request
        tasks = assembly.task_reader.list_run(descriptor.run_id)
        if not tasks:
            raise RuntimeError("production dispatch requires a request for an empty run")
        matching = tuple(
            task
            for task in tasks
            if task.project_id == descriptor.project_id and not task.superseded
        )
        if not matching:
            raise RuntimeError("production run has no matching project tasks")
        if any(task.policy_hash != descriptor.policy.policy_hash for task in matching):
            raise RuntimeError("production run policy does not match durable task policy")
        first = min(matching, key=lambda task: (task.chapter_index, task.task_id.root))
        return CreativeRunRequest(
            run_id=descriptor.run_id,
            project_id=descriptor.project_id,
            basis_commit=first.basis_commit,
            basis_snapshot=first.basis_snapshot,
            policy=descriptor.policy,
            input_artifact_refs=first.input_artifact_refs,
            continuation_artifact_refs=first.terminal_artifact_refs,
            current_chapter=first.chapter_index,
            target_chapters=first.target_chapters,
        )

    async def _run_project(
        self,
        descriptor: ProductionRunDescriptor,
        semaphore: asyncio.Semaphore,
        budget: DispatchTaskBudget,
    ) -> ProductionProjectDispatchResult:
        key = (descriptor.project_id, descriptor.run_id)
        assembly_error = self._assembly_errors.get(key)
        if assembly_error is not None:
            return ProductionProjectDispatchResult(
                descriptor.project_id,
                descriptor.run_id,
                "assembly_failed",
                error_type=type(assembly_error).__name__,
            )
        assembly = self._assemblies[key]
        async with semaphore:
            try:
                request = self._request_for(descriptor, assembly)
                runner = VerticalCreativeRunner(
                    runtime=assembly.runtime,
                    dispatcher=assembly.dispatcher,
                    tasks=assembly.task_reader,
                )
                report = await runner.run(
                    request,
                    max_tasks=descriptor.max_tasks,
                    max_slices=descriptor.max_slices,
                    stop_after_chapter=descriptor.stop_after_chapter,
                    task_budget=budget,
                )
            except Exception as error:
                return ProductionProjectDispatchResult(
                    descriptor.project_id,
                    descriptor.run_id,
                    "failed",
                    error_type=type(error).__name__,
                )
            return ProductionProjectDispatchResult(
                descriptor.project_id,
                descriptor.run_id,
                report.status.value,
                report=report,
            )

    def _assert_admission_released(self) -> dict[str, object]:
        snapshot = self._admission.snapshot()
        inflight = cast(int, snapshot["inflight_requests"])
        acquired = cast(int, snapshot["acquired_requests"])
        released = cast(int, snapshot["released_requests"])
        if inflight != 0 or acquired != released:
            raise RuntimeError("production dispatch returned with model leases still active")
        return snapshot

    def _scheduled_summary(self) -> tuple[int, datetime | None]:
        now = datetime.now(UTC)
        count = 0
        next_at: datetime | None = None
        for descriptor in self._runs:
            assembly = self._assemblies.get((descriptor.project_id, descriptor.run_id))
            if assembly is None:
                continue
            reader = assembly.task_reader
            count_fn = getattr(reader, "future_scheduled_count", None)
            next_fn = getattr(reader, "next_scheduled_at", None)
            if callable(count_fn) and callable(next_fn):
                count += int(
                    count_fn(
                        project_id=descriptor.project_id,
                        run_id=descriptor.run_id,
                        now=now,
                    )
                )
                candidate = next_fn(
                    project_id=descriptor.project_id,
                    run_id=descriptor.run_id,
                    now=now,
                )
                if candidate is not None:
                    next_at = candidate if next_at is None else min(next_at, candidate)
                continue
            for task in reader.list_run(descriptor.run_id):
                if (
                    task.status is not TaskStatus.READY
                    or task.scheduled_for is None
                    or task.scheduled_for <= now
                    or task.paused
                    or task.superseded
                    or task.current_attempt_id is not None
                    or task.failure_budget <= 0
                ):
                    continue
                count += 1
                next_at = (
                    task.scheduled_for if next_at is None else min(next_at, task.scheduled_for)
                )
        return count, next_at

    def _build_result(
        self,
        projects: tuple[ProductionProjectDispatchResult, ...],
        snapshot: dict[str, object],
    ) -> ProductionDispatchResult:
        terminal_statuses = {
            "completed",
            "blocked",
            "failed",
            "assembly_failed",
        }
        all_terminal = all(item.status in terminal_statuses for item in projects)
        if all(item.status == VerticalRunStatus.COMPLETED.value for item in projects):
            status = "completed"
        elif all_terminal and any(
            item.status in {"failed", "assembly_failed"} for item in projects
        ):
            status = "failed"
        elif all_terminal:
            status = "blocked"
        elif any(item.status == VerticalRunStatus.RECOVERY_PENDING.value for item in projects):
            status = "recovery_pending"
        elif any(item.report is not None and item.report.runtime_results for item in projects):
            status = "progressed"
        else:
            status = "waiting"
        remaining, next_at = self._scheduled_summary()
        parallelisms = {item.policy.runtime_parallelism for item in self._runs}
        lookaheads = {item.policy.enable_planner_lookahead for item in self._runs}
        return ProductionDispatchResult(
            status=status,
            runtime_parallelism=next(iter(parallelisms)) if len(parallelisms) == 1 else None,
            planner_lookahead=next(iter(lookaheads)) if len(lookaheads) == 1 else None,
            project_parallelism=self._project_parallelism,
            endpoint_request_limit=cast(int, snapshot["endpoint_request_limit"]),
            configured_kv_token_budget=cast(int | None, snapshot["configured_kv_token_budget"]),
            effective_kv_token_budget=cast(int | None, snapshot["effective_kv_token_budget"]),
            queue_depth=cast(int, snapshot["queue_depth"]),
            max_inflight_requests=cast(int, snapshot["max_inflight_requests"]),
            total_wait_seconds=cast(float, snapshot["total_wait_seconds"]),
            scheduling_timeouts=cast(int, snapshot["scheduling_timeouts"]),
            acquired_requests=cast(int, snapshot["acquired_requests"]),
            released_requests=cast(int, snapshot["released_requests"]),
            per_project=projects,
            remaining_scheduled_tasks=remaining,
            next_scheduled_at=next_at,
            all_projects_terminal=all_terminal,
        )

    async def run_once(self) -> ProductionDispatchResult:
        self._ensure_assemblies()
        semaphore = asyncio.Semaphore(self._project_parallelism)
        budget = _GlobalDispatchTaskBudget(self._max_total_tasks)
        try:
            projects = tuple(
                await asyncio.gather(
                    *(self._run_project(descriptor, semaphore, budget) for descriptor in self._runs)
                )
            )
        except BaseException:
            await asyncio.sleep(0)
            self._assert_admission_released()
            raise
        await asyncio.sleep(0)
        snapshot = self._assert_admission_released()
        return self._build_result(projects, snapshot)

    async def run_watch(
        self,
        *,
        poll_interval_seconds: float = 5.0,
        stop_event: asyncio.Event | None = None,
        max_cycles: int | None = None,
    ) -> ProductionDispatchResult:
        if poll_interval_seconds <= 0:
            raise ValueError("watch poll interval must be positive")
        if max_cycles is not None and max_cycles < 1:
            raise ValueError("watch max_cycles must be positive")
        result: ProductionDispatchResult | None = None
        cycles = 0
        while True:
            result = await self.run_once()
            cycles += 1
            if result.all_projects_terminal or (max_cycles is not None and cycles >= max_cycles):
                return result
            if stop_event is not None and stop_event.is_set():
                return result
            delay = poll_interval_seconds
            if result.next_scheduled_at is not None:
                seconds = (result.next_scheduled_at - datetime.now(UTC)).total_seconds()
                if seconds > 0:
                    delay = min(delay, seconds)
            if stop_event is None:
                await asyncio.sleep(delay)
                continue
            with suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=delay)


def load_production_run_descriptors(path: Path) -> tuple[ProductionRunDescriptor, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_runs = payload.get("runs") if isinstance(payload, dict) else payload
    if not isinstance(raw_runs, list) or not raw_runs:
        raise ValueError("production runs manifest must contain a non-empty runs list")
    base_dir = path.parent
    return tuple(
        ProductionRunDescriptor.from_payload(cast(dict[str, object], item), base_dir=base_dir)
        for item in raw_runs
    )


def _payload_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("production run integer field is invalid")
    return value


def _optional_int(value: object) -> int | None:
    return None if value is None else _payload_int(value)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("production run number field is invalid")
    return float(value)


def _load_json_value(value: object, base_dir: Path) -> object:
    if isinstance(value, str):
        path = Path(value)
        if not path.is_absolute():
            path = base_dir / path
        return json.loads(path.read_text(encoding="utf-8"))
    return value


def _load_policy(value: object, base_dir: Path) -> CreativeRunPolicy:
    return CreativeRunPolicy.model_validate(_load_json_value(value, base_dir), strict=False)


def _load_request(value: object, base_dir: Path) -> CreativeRunRequest:
    return CreativeRunRequest.model_validate(_load_json_value(value, base_dir), strict=False)


__all__ = [
    "ProductionDispatchCoordinator",
    "ProductionDispatchResult",
    "ProductionProjectDispatchResult",
    "ProductionRunDescriptor",
    "load_production_run_descriptors",
]
