"""Bounded single-dispatcher loop for the isolated Stage 5 kernel."""

from __future__ import annotations

import asyncio

from novel_agent.adapters.postgres.runtime import RuntimeTaskQueryRepository
from novel_agent.domain.creative_runtime import CreativeRunResult
from novel_agent.domain.ids import ProjectId, RunId
from novel_agent.domain.runtime import TaskKind, TaskPurpose, TaskRecord
from novel_agent.services.creative_runtime import CreativeRuntimeService
from novel_agent.services.runtime_commands import RuntimeCommandConflictError


class CreativeDispatcher:
    def __init__(
        self,
        tasks: RuntimeTaskQueryRepository,
        runtime: CreativeRuntimeService,
        *,
        worker_id: str,
        project_id: ProjectId | None = None,
        run_id: RunId | None = None,
        parallelism: int = 1,
    ) -> None:
        if not worker_id:
            raise ValueError("dispatcher worker_id is required")
        if parallelism not in {1, 2, 4, 6, 8}:
            raise ValueError("dispatcher parallelism must be one of 1, 2, 4, 6, or 8")
        self._tasks = tasks
        self._runtime = runtime
        self._worker_id = worker_id
        self._project_id = project_id
        self._run_id = run_id
        self._parallelism = parallelism

    async def poll_one(self) -> CreativeRunResult | None:
        task_id = self._tasks.next_ready(project_id=self._project_id, run_id=self._run_id)
        if task_id is None:
            return None
        try:
            return await self._runtime.advance(task_id, worker_id=self._worker_id)
        except RuntimeCommandConflictError:
            # Ready is a cache/query view. Another dispatcher or a basis flip may
            # invalidate it; claim remains the authoritative fail-closed check.
            return None

    async def run_bounded(self, *, max_tasks: int) -> tuple[CreativeRunResult, ...]:
        if max_tasks < 1:
            raise ValueError("dispatcher max_tasks must be positive")
        results: list[CreativeRunResult] = []
        while len(results) < max_tasks:
            remaining = max_tasks - len(results)
            if self._parallelism == 1:
                result = await self.poll_one()
                if result is None:
                    break
                results.append(result)
                continue
            ready = self._tasks.ready_batch(
                # Read beyond the execution limit so ineligible same-project
                # siblings cannot hide otherwise eligible projects.
                limit=max(self._parallelism * 2, remaining),
                project_id=self._project_id,
                run_id=self._run_id,
            )
            selected = self._parallel_batch(ready, limit=min(self._parallelism, remaining))
            if not selected:
                break
            outcomes = await asyncio.gather(
                *(
                    self._advance(task, worker_suffix=index)
                    for index, task in enumerate(selected, start=1)
                ),
                return_exceptions=True,
            )
            first_error: BaseException | None = None
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    first_error = first_error or outcome
                elif outcome is not None:
                    results.append(outcome)
            if first_error is not None:
                raise first_error
            if all(outcome is None for outcome in outcomes):
                # All candidates lost their authoritative claim. Let the outer
                # runner re-read durable state instead of polling this stale view.
                break
        return tuple(results)

    async def _advance(self, task: TaskRecord, *, worker_suffix: int) -> CreativeRunResult | None:
        worker_id = f"{self._worker_id}.{worker_suffix}"[:128]
        try:
            return await self._runtime.advance(task.task_id, worker_id=worker_id)
        except RuntimeCommandConflictError:
            return None

    @staticmethod
    def _parallel_batch(
        ready: tuple[TaskRecord, ...], *, limit: int = 2
    ) -> tuple[TaskRecord, ...]:
        if not ready:
            return ()
        first = ready[0]
        leaf_kinds = {TaskKind.PLAN_CANDIDATE, TaskKind.DRAFT_CANDIDATE}
        if first.kind not in leaf_kinds:
            return (first,)
        selected = [first]
        selected_by_project: dict[ProjectId, list[TaskRecord]] = {
            first.project_id: [first]
        }
        for candidate in ready[1:]:
            if len(selected) >= limit:
                break
            if candidate.kind not in leaf_kinds:
                continue
            same_project = selected_by_project.get(candidate.project_id, [])
            if same_project:
                if len(same_project) >= 2:
                    continue
                sibling = same_project[0]
                pair = {candidate.kind, sibling.kind}
                planner = candidate if candidate.kind is TaskKind.PLAN_CANDIDATE else sibling
                if (
                    pair != leaf_kinds
                    or planner.purpose is not TaskPurpose.LOOKAHEAD
                    or candidate.basis_commit != sibling.basis_commit
                    or planner.protected_chapter_index
                    != (
                        candidate.chapter_index
                        if candidate.kind is TaskKind.DRAFT_CANDIDATE
                        else sibling.chapter_index
                    )
                ):
                    continue
            selected.append(candidate)
            selected_by_project.setdefault(candidate.project_id, []).append(candidate)
        return tuple(selected)


__all__ = ["CreativeDispatcher"]
