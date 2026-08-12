"""Formal bounded runner for the admitted Stage 5 A-layer assembly."""

from __future__ import annotations

from novel_agent.domain.creative_runtime import CreativeRunRequest, CreativeRunResult
from novel_agent.domain.stage5_manifest import Stage5DevelopmentManifest
from novel_agent.runtime.creative_assembly import validate_runtime_assembly
from novel_agent.runtime.creative_dispatcher import CreativeDispatcher
from novel_agent.services.creative_runtime import CreativeRuntimeService


class IsolatedRuntimeRunner:
    """Runs only an explicitly admitted fake-Planner/real-Stage3-Writer assembly."""

    def __init__(
        self,
        runtime: CreativeRuntimeService,
        dispatcher: CreativeDispatcher,
        manifest: Stage5DevelopmentManifest,
        *,
        planner: object,
        writer: object,
        plan_materializer: object,
        draft_materializer: object,
    ) -> None:
        validate_runtime_assembly(
            manifest,
            planner=planner,
            writer=writer,
            plan_materializer=plan_materializer,
            draft_materializer=draft_materializer,
            production=False,
        )
        self._runtime = runtime
        self._dispatcher = dispatcher

    async def run_bounded(
        self,
        request: CreativeRunRequest,
        *,
        max_tasks: int,
    ) -> tuple[CreativeRunResult, ...]:
        if max_tasks < 1 or max_tasks > request.policy.max_tasks_per_advance:
            raise ValueError("runner task budget exceeds the pinned run policy")
        started = self._runtime.start(request)
        progressed = await self._dispatcher.run_bounded(max_tasks=max_tasks)
        return (started, *progressed)


__all__ = ["IsolatedRuntimeRunner"]
