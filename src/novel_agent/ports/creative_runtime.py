"""Narrow leaf and trusted-boundary ports owned by the Stage 5 runtime."""

from __future__ import annotations

from typing import Protocol

from novel_agent.domain.changes import CandidateChangeBundle, ValidationReport
from novel_agent.domain.creative_runtime import (
    AcceptedCandidateBinding,
    PlanningLoopRequest,
    PlanningLoopResult,
)
from novel_agent.domain.generation import WritingLoopRequest
from novel_agent.domain.runtime import EffectReceipt
from novel_agent.domain.writing_loop import WritingLoopResult


class PlanningLeafPort(Protocol):
    async def run(self, request: PlanningLoopRequest) -> PlanningLoopResult: ...


class WritingLeafPort(Protocol):
    async def run(self, request: WritingLoopRequest) -> WritingLoopResult: ...


class CandidateMaterializer(Protocol):
    def materialize(
        self, accepted: AcceptedCandidateBinding
    ) -> tuple[CandidateChangeBundle, ValidationReport]: ...


class EffectResolution(Protocol):
    receipt: EffectReceipt


class EffectStatusResolver(Protocol):
    def resolve(self, receipt: EffectReceipt) -> EffectResolution: ...


__all__ = [
    "CandidateMaterializer",
    "EffectStatusResolver",
    "PlanningLeafPort",
    "WritingLeafPort",
]
