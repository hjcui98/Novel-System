"""Build one normalized, content-addressed Stage 2 runtime configuration inventory."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable
from typing import Any, TypeVar

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import SchemaVersion, StableId
from novel_agent.domain.stage2 import (
    AgentSpec,
    PromptContractRef,
    SkillContractRef,
    Stage2ConfigurationManifest,
    ToolPolicy,
)
from novel_agent.services.content_addressing import content_id

T = TypeVar("T")


class Stage2ConfigurationBuilder:
    def build(
        self,
        *,
        manifest_id: StableId,
        schema_version: SchemaVersion,
        agent_specs: Iterable[AgentSpec],
        prompt_contracts: Iterable[PromptContractRef],
        skill_contracts: Iterable[SkillContractRef],
        tool_policies: Iterable[ToolPolicy],
        schema_artifacts: Iterable[ArtifactRef] = (),
    ) -> Stage2ConfigurationManifest:
        agents = self._unique_sorted(
            agent_specs,
            key=lambda item: (
                item.agent_type.value,
                item.mode.value,
                item.version.root,
                item.agent_id.root,
            ),
            identity=lambda item: item.agent_id,
            label="AgentSpec",
        )
        prompts = self._unique_sorted(
            prompt_contracts,
            key=lambda item: (item.contract_id.root, item.version.root, item.content_hash.root),
            identity=lambda item: (item.contract_id, item.version, item.content_hash),
            label="prompt contract",
        )
        skills = self._unique_sorted(
            skill_contracts,
            key=lambda item: (item.contract_id.root, item.version.root, item.content_hash.root),
            identity=lambda item: (item.contract_id, item.version, item.content_hash),
            label="skill contract",
        )
        policies = self._unique_sorted(
            tool_policies,
            key=lambda item: (item.policy_id.root, item.version.root, item.content_hash.root),
            identity=lambda item: (item.policy_id, item.version, item.content_hash),
            label="ToolPolicy",
        )
        schemas = self._unique_sorted(
            schema_artifacts,
            key=lambda item: item.artifact_id.root,
            identity=lambda item: item.artifact_id,
            label="schema artifact",
        )
        payload = {
            "manifest_id": manifest_id.root,
            "schema_version": schema_version.root,
            "agent_specs": [item.model_dump(mode="json") for item in agents],
            "prompt_contracts": [item.model_dump(mode="json") for item in prompts],
            "skill_contracts": [item.model_dump(mode="json") for item in skills],
            "tool_policies": [item.model_dump(mode="json") for item in policies],
            "schema_artifacts": [item.model_dump(mode="json") for item in schemas],
        }
        return Stage2ConfigurationManifest(
            manifest_id=manifest_id,
            schema_version=schema_version,
            agent_specs=agents,
            prompt_contracts=prompts,
            skill_contracts=skills,
            tool_policies=policies,
            schema_artifacts=schemas,
            configuration_fingerprint=content_id(payload),
        )

    @staticmethod
    def _unique_sorted(
        items: Iterable[T],
        *,
        key: Callable[[T], Any],
        identity: Callable[[T], Hashable],
        label: str,
    ) -> tuple[T, ...]:
        materialized = tuple(items)
        indexed: dict[Hashable, T] = {}
        for item in materialized:
            item_identity = identity(item)
            existing = indexed.get(item_identity)
            if existing is not None and existing != item:
                raise ValueError(f"configuration {label} identity has conflicting content")
            indexed[item_identity] = item
        return tuple(sorted(indexed.values(), key=key))
