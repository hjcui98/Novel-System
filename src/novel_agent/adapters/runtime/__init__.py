"""Stage 5 runtime leaf adapters."""

from novel_agent.adapters.runtime.materializers import (
    DraftCandidateMaterializer,
    PlanCandidateMaterializer,
)
from novel_agent.adapters.runtime.stage3_writer import Stage3WritingLeafAdapter
from novel_agent.adapters.runtime.stage4_planner import (
    Stage4PlanningInvocation,
    Stage4PlanningLeafAdapter,
)

__all__ = [
    "DraftCandidateMaterializer",
    "PlanCandidateMaterializer",
    "Stage3WritingLeafAdapter",
    "Stage4PlanningInvocation",
    "Stage4PlanningLeafAdapter",
]
