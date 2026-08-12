"""Narrow consumer port for the Stage 3-owned shared Context Runtime."""

from __future__ import annotations

from typing import Protocol

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import RunId, TaskId
from novel_agent.domain.planning import PlannerContextPackage, PlannerContextProjection


class PlannerContextRuntimeFailure(RuntimeError):
    """A shared Context runtime operation cannot settle the Planner stream."""


class PlannerContextRuntimePort(Protocol):
    def start(
        self,
        *,
        run_id: RunId,
        task_id: TaskId,
        seed: PlannerContextPackage,
        seed_ref: ArtifactRef,
    ) -> PlannerContextProjection: ...

    def append_delta(
        self,
        *,
        run_id: RunId,
        task_id: TaskId,
        delta_ref: ArtifactRef,
    ) -> PlannerContextProjection: ...

    def project(self, *, run_id: RunId, task_id: TaskId) -> PlannerContextProjection: ...
