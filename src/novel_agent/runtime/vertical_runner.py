"""General Stage 2-5 vertical runner over the production Stage 5 assembly."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, cast

from novel_agent.domain.creative_runtime import (
    CreativeRunRequest,
    CreativeRunResult,
    CreativeRunTerminal,
)
from novel_agent.domain.ids import TaskId
from novel_agent.domain.runtime import TaskKind, TaskPurpose, TaskRecord, TaskStatus
from novel_agent.domain.stage5_evaluation import Stage5VerticalRunReport, VerticalRunStatus
from novel_agent.ports.creative_runtime import RuntimeTaskReader
from novel_agent.runtime.creative_dispatcher import CreativeDispatcher
from novel_agent.services.creative_runtime import CreativeRuntimeService


class DispatchTaskBudget(Protocol):
    def reserve(self, requested: int) -> int: ...

    def release_unused(self, reserved: int, consumed: int) -> None: ...


class VerticalCreativeRunner:
    """Run production leaves until completion, a real wait boundary, or an explicit yield."""

    def __init__(
        self,
        *,
        runtime: CreativeRuntimeService,
        dispatcher: CreativeDispatcher,
        tasks: RuntimeTaskReader,
    ) -> None:
        self._runtime = runtime
        self._dispatcher = dispatcher
        self._tasks = tasks

    async def run(
        self,
        request: CreativeRunRequest,
        *,
        max_tasks: int,
        max_slices: int | None = None,
        stop_after_chapter: int | None = None,
        task_budget: DispatchTaskBudget | None = None,
    ) -> Stage5VerticalRunReport:
        if max_tasks < 1:
            raise ValueError("vertical runner dispatch slice size must be positive")
        if max_slices is not None and max_slices < 1:
            raise ValueError("vertical runner max_slices must be positive")
        if stop_after_chapter is not None and not (
            request.current_chapter < stop_after_chapter <= request.target_chapters
        ):
            raise ValueError(
                "vertical runner stop_after_chapter must be after current and at or before target"
            )

        tasks = self._tasks.list_run(request.run_id)
        results: list[CreativeRunResult] = []
        if not tasks:
            results.append(self._runtime.start(request))
            tasks = self._tasks.list_run(request.run_id)

        dispatch_slices = 0
        while True:
            completed_chapters = self._completed_chapters(request, tasks)
            reached_chapter_boundary = stop_after_chapter is not None and any(
                chapter >= stop_after_chapter for chapter in completed_chapters
            )
            if request.target_chapters in completed_chapters or reached_chapter_boundary:
                break
            recovered = self._recover_boundary(tasks)
            if recovered is not None:
                results.append(recovered)
                tasks = self._tasks.list_run(request.run_id)
                continue
            if self._has_blocking_task(tasks) and not self._has_runnable_background_work(tasks):
                break
            if not self._has_runnable_work(tasks):
                break
            if max_slices is not None and dispatch_slices >= max_slices:
                break

            reserved_tasks = max_tasks if task_budget is None else task_budget.reserve(max_tasks)
            if reserved_tasks < 1:
                break
            try:
                progressed = await self._dispatcher.run_bounded(max_tasks=reserved_tasks)
            except BaseException:
                if task_budget is not None:
                    task_budget.release_unused(reserved_tasks, 0)
                raise
            if task_budget is not None:
                task_budget.release_unused(reserved_tasks, len(progressed))
            dispatch_slices += 1
            results.extend(progressed)
            tasks = self._tasks.list_run(request.run_id)
            if not progressed:
                # READY is a query view. Claim conflicts or another worker may make
                # this slice produce no result; return control instead of spinning.
                break

        completed_chapters = self._completed_chapters(request, tasks)
        reached_chapter_boundary = stop_after_chapter is not None and any(
            chapter >= stop_after_chapter for chapter in completed_chapters
        )
        reached_slice_limit = (
            max_slices is not None
            and dispatch_slices >= max_slices
            and self._has_runnable_work(tasks)
        )
        status = self._status(
            tuple(results),
            completed_target=request.target_chapters in completed_chapters,
            tasks=tasks,
            reached_slice_limit=reached_slice_limit,
            reached_chapter_boundary=reached_chapter_boundary,
        )
        final_commit = (
            results[-1].current_commit
            if results
            else (tasks[-1].basis_commit if tasks else request.basis_commit)
        )
        return Stage5VerticalRunReport(
            run_id=request.run_id,
            project_id=request.project_id,
            current_chapter=request.current_chapter,
            target_chapter=request.target_chapters,
            status=status,
            final_commit=final_commit,
            completed_chapters=completed_chapters,
            dispatch_slices=dispatch_slices,
            runtime_results=tuple(results),
            tasks=tasks,
            outputs_frozen=status is VerticalRunStatus.COMPLETED,
        )

    def _recover_boundary(self, tasks: tuple[TaskRecord, ...]) -> CreativeRunResult | None:
        recover = getattr(self._runtime, "recover_boundary", None)
        if not callable(recover):  # Narrow deterministic runner fixtures.
            return None
        recover_boundary = cast(Callable[[TaskId], CreativeRunResult | None], recover)
        for task in reversed(tasks):
            recoverable = (
                (
                    task.kind in {TaskKind.PLAN_ACCEPTANCE, TaskKind.DRAFT_ACCEPTANCE}
                    and task.status is TaskStatus.WAITING_INPUT
                )
                or (task.kind is TaskKind.DRAFT_COMMIT and task.current_attempt_id is not None)
                or (
                    task.kind is TaskKind.PROJECTION_FRESHNESS
                    and task.projection_after == "draft"
                    and task.status is TaskStatus.SUCCEEDED
                )
            )
            if recoverable and not task.superseded:
                recovered = recover_boundary(task.task_id)
                if recovered is not None:
                    return recovered
        return None

    @staticmethod
    def _completed_chapters(
        request: CreativeRunRequest, tasks: tuple[TaskRecord, ...]
    ) -> tuple[int, ...]:
        return tuple(
            sorted(
                {
                    task.chapter_index
                    for task in tasks
                    if task.kind is TaskKind.PROJECTION_FRESHNESS
                    and task.projection_after == "draft"
                    and task.status is TaskStatus.SUCCEEDED
                    and task.chapter_index > request.current_chapter
                }
            )
        )

    @staticmethod
    def _is_background(task: TaskRecord) -> bool:
        return task.purpose in {TaskPurpose.LOOKAHEAD, TaskPurpose.DERIVED_MAINTENANCE}

    @classmethod
    def _has_blocking_task(cls, tasks: tuple[TaskRecord, ...]) -> bool:
        return any(
            not task.superseded
            and not cls._is_background(task)
            and task.status in {TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.CANCELLED}
            for task in tasks
        )

    @staticmethod
    def _has_runnable_work(tasks: tuple[TaskRecord, ...]) -> bool:
        now = datetime.now(UTC)
        return any(
            task.status is TaskStatus.READY
            and not task.paused
            and not task.superseded
            and task.current_attempt_id is None
            and task.failure_budget > 0
            and (task.scheduled_for is None or task.scheduled_for <= now)
            for task in tasks
        )

    @classmethod
    def _has_runnable_background_work(cls, tasks: tuple[TaskRecord, ...]) -> bool:
        return cls._has_runnable_work(tuple(task for task in tasks if cls._is_background(task)))

    @classmethod
    def _has_recovery_pending(cls, tasks: tuple[TaskRecord, ...]) -> bool:
        return any(
            not task.superseded
            and not cls._is_background(task)
            and (
                task.status in {TaskStatus.RUNNING, TaskStatus.RECOVERY_PENDING}
                or task.current_attempt_id is not None
            )
            for task in tasks
        )

    @classmethod
    def _status(
        cls,
        results: tuple[CreativeRunResult, ...],
        *,
        completed_target: bool,
        tasks: tuple[TaskRecord, ...],
        reached_slice_limit: bool,
        reached_chapter_boundary: bool,
    ) -> VerticalRunStatus:
        if completed_target:
            return VerticalRunStatus.COMPLETED
        if results and results[-1].terminal is CreativeRunTerminal.COMPLETED:
            return VerticalRunStatus.COMPLETED
        if cls._has_recovery_pending(tasks):
            return VerticalRunStatus.RECOVERY_PENDING
        if cls._has_blocking_task(tasks):
            return VerticalRunStatus.BLOCKED
        if results and results[-1].terminal in {
            CreativeRunTerminal.BLOCKED,
            CreativeRunTerminal.CANCELLED,
        }:
            current_task = next(
                (task for task in tasks if task.task_id == results[-1].current_task_id),
                None,
            )
            if current_task is None or (
                not current_task.superseded and not cls._is_background(current_task)
            ):
                return VerticalRunStatus.BLOCKED
        if reached_slice_limit:
            return VerticalRunStatus.YIELDED
        if reached_chapter_boundary:
            return VerticalRunStatus.YIELDED
        return VerticalRunStatus.WAITING


__all__ = ["DispatchTaskBudget", "VerticalCreativeRunner"]
