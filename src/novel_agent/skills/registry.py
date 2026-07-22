"""Read-only content-addressed workflow skills."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.stage2 import SkillContractRef
from novel_agent.prompts.registry import content_hash


class SkillRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class SkillTemplate:
    skill_id: StableId
    version: SchemaVersion
    path: Path
    expected_hash: ArtifactId


class SkillRegistry:
    def __init__(self, skills: Iterable[SkillTemplate]) -> None:
        indexed: dict[tuple[StableId, SchemaVersion], SkillTemplate] = {}
        for skill in skills:
            key = (skill.skill_id, skill.version)
            if key in indexed:
                raise SkillRegistryError(f"duplicate skill contract: {key}")
            indexed[key] = skill
        self._skills = indexed

    def resolve(self, skill_id: StableId, version: SchemaVersion) -> tuple[str, SkillContractRef]:
        try:
            skill = self._skills[(skill_id, version)]
        except KeyError as error:
            raise SkillRegistryError("skill version is not explicitly registered") from error
        content = skill.path.read_bytes()
        actual_hash = content_hash(content)
        if actual_hash != skill.expected_hash:
            raise SkillRegistryError(f"skill content hash mismatch: {skill.path}")
        return content.decode("utf-8"), SkillContractRef(
            contract_id=skill_id,
            version=version,
            content_hash=actual_hash,
        )
