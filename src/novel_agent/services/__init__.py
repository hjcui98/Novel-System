"""Trusted deterministic application services."""

from typing import Any

from novel_agent.services.artifacts import (
    ArtifactIntegrityError,
    ArtifactRepository,
    object_key,
    sha256_id,
)
from novel_agent.services.commits import CommitService, manifest_commit_id
from novel_agent.services.evaluation import EvaluationHarness, EvaluationLedgerRepository
from novel_agent.services.event_log import RunCheckpointRepository, RunEventLogRepository
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint

__all__ = [
    "AgentContextProjector",
    "AgentContextRuntime",
    "ArtifactIntegrityError",
    "ArtifactRepository",
    "CommitService",
    "ContextCompactor",
    "EvaluationHarness",
    "EvaluationLedgerRepository",
    "ModelGateway",
    "RegisteredModelEndpoint",
    "RunCheckpointRepository",
    "RunEventLogRepository",
    "manifest_commit_id",
    "object_key",
    "sha256_id",
]


def __getattr__(name: str) -> Any:
    if name in {"AgentContextProjector", "AgentContextRuntime", "ContextCompactor"}:
        from novel_agent.services.agent_context import (
            AgentContextProjector,
            AgentContextRuntime,
            ContextCompactor,
        )

        return {
            "AgentContextProjector": AgentContextProjector,
            "AgentContextRuntime": AgentContextRuntime,
            "ContextCompactor": ContextCompactor,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
