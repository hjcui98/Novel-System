"""Read-only, version-pinned Stage 2 AgentSpec registry."""

from __future__ import annotations

from collections.abc import Iterable

from novel_agent.domain.ids import ArtifactId
from novel_agent.domain.stage2 import AgentMode, AgentSpec, AgentType, ToolPolicy
from novel_agent.services.content_addressing import content_id


class RegistryError(ValueError):
    """Raised when an immutable registry contract is violated."""


def tool_policy_content_id(policy: ToolPolicy) -> ArtifactId:
    return content_id(policy.model_dump(mode="json", exclude={"content_hash"}))


def seal_tool_policy(policy: ToolPolicy) -> ToolPolicy:
    return policy.model_copy(update={"content_hash": tool_policy_content_id(policy)})


def agent_spec_content_id(spec: AgentSpec) -> ArtifactId:
    return content_id(spec.model_dump(mode="json", exclude={"content_hash"}))


def seal_agent_spec(spec: AgentSpec) -> AgentSpec:
    sealed_policy = seal_tool_policy(spec.tool_policy)
    candidate = spec.model_copy(update={"tool_policy": sealed_policy})
    return candidate.model_copy(update={"content_hash": agent_spec_content_id(candidate)})


class AgentRegistry:
    def __init__(self, specs: Iterable[AgentSpec]) -> None:
        indexed: dict[tuple[AgentType, AgentMode, str], AgentSpec] = {}
        for spec in specs:
            key = (spec.agent_type, spec.mode, spec.version.root)
            if key in indexed:
                raise RegistryError(f"duplicate agent contract: {key}")
            indexed[key] = spec
        self._specs = indexed

    def resolve(self, agent_type: AgentType, mode: AgentMode, version: str) -> AgentSpec:
        try:
            return self._specs[(agent_type, mode, version)]
        except KeyError as error:
            raise RegistryError(
                f"agent contract is not explicitly registered: {agent_type}/{mode}/{version}"
            ) from error

    def all(self) -> tuple[AgentSpec, ...]:
        return tuple(self._specs[key] for key in sorted(self._specs, key=str))
