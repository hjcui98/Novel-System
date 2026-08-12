"""Bounded single-dispatcher loop for the isolated Stage 5 kernel."""

from __future__ import annotations

from novel_agent.adapters.postgres.runtime import RuntimeTaskQueryRepository
from novel_agent.domain.creative_runtime import CreativeRunResult
from novel_agent.domain.ids import ProjectId, RunId
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
    ) -> None:
        if not worker_id:
            raise ValueError("dispatcher worker_id is required")
        self._tasks = tasks
        self._runtime = runtime
        self._worker_id = worker_id
        self._project_id = project_id
        self._run_id = run_id

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
        for _ in range(max_tasks):
            result = await self.poll_one()
            if result is None:
                break
            results.append(result)
        return tuple(results)


__all__ = ["CreativeDispatcher"]
