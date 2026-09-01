"""Narrow leaf and trusted-boundary ports owned by the Stage 5 runtime."""

from __future__ import annotations

from typing import Protocol

from novel_agent.domain.changes import CandidateChangeBundle, CommitResult, ValidationReport
from novel_agent.domain.creative_runtime import (
    AcceptedCandidateBinding,
    PlanningLoopRequest,
    PlanningLoopResult,
)
from novel_agent.domain.generation import WritingLoopRequest
from novel_agent.domain.ids import RunId, StableId
from novel_agent.domain.memory_write import MemoryRepairFinding, MemoryWriteWorkflowResult
from novel_agent.domain.runtime import EffectReceipt, TaskRecord
from novel_agent.domain.writing_loop import WritingLoopResult


class CandidateMaterializationError(ValueError):
    """An accepted leaf candidate cannot be mapped to the five canonical roots."""


class PlanningLeafPort(Protocol):
    async def run(self, request: PlanningLoopRequest) -> PlanningLoopResult: ...


class WritingLeafPort(Protocol):
    async def run(self, request: WritingLoopRequest) -> WritingLoopResult: ...


class CandidateMaterializer(Protocol):
    def materialize(
        self, accepted: AcceptedCandidateBinding
    ) -> tuple[CandidateChangeBundle, ValidationReport]: ...


class ChapterSettlementPort(Protocol):
    def effect_identity(self, accepted: AcceptedCandidateBinding) -> StableId: ...

    def resolve_commit(self, accepted: AcceptedCandidateBinding) -> CommitResult | None: ...

    async def settle(self, accepted: AcceptedCandidateBinding) -> MemoryWriteWorkflowResult: ...


class MemoryMaintenancePort(Protocol):
    async def run(
        self, task: TaskRecord, finding: MemoryRepairFinding
    ) -> MemoryWriteWorkflowResult: ...


class RuntimeTaskReader(Protocol):
    def list_run(self, run_id: RunId) -> tuple[TaskRecord, ...]: ...


class EffectResolution(Protocol):
    receipt: EffectReceipt


class EffectStatusResolver(Protocol):
    def resolve(self, receipt: EffectReceipt) -> EffectResolution: ...


__all__ = [
    "CandidateMaterializationError",
    "CandidateMaterializer",
    "ChapterSettlementPort",
    "EffectStatusResolver",
    "MemoryMaintenancePort",
    "PlanningLeafPort",
    "RuntimeTaskReader",
    "WritingLeafPort",
]
