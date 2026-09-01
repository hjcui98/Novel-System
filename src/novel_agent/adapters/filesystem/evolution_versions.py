"""Durable task-local active identities for offline U8 evolution candidates."""

from __future__ import annotations

import json
import os
from pathlib import Path

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import StableId
from novel_agent.services.evolution import EvolutionPromotionError


class FilesystemEvolutionVersionRegistry:
    """Persist one active ArtifactRef per target without touching production pins."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def initialize(self, target_id: StableId, active: ArtifactRef) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        target = self._path(target_id)
        payload = self._payload(active)
        try:
            with target.open("x", encoding="utf-8") as handle:
                handle.write(payload)
        except FileExistsError:
            self.require_active(target_id, active)

    def active(self, target_id: StableId) -> ArtifactRef | None:
        target = self._path(target_id)
        if not target.exists():
            return None
        return ArtifactRef.model_validate_json(target.read_bytes(), strict=True)

    def require_active(self, target_id: StableId, expected: ArtifactRef) -> None:
        if self.active(target_id) != expected:
            raise EvolutionPromotionError("active evolution version changed before promotion")

    def compare_and_swap(
        self, target_id: StableId, expected: ArtifactRef, replacement: ArtifactRef
    ) -> None:
        self.require_active(target_id, expected)
        target = self._path(target_id)
        temporary = target.with_suffix(".next")
        temporary.write_text(self._payload(replacement), encoding="utf-8")
        os.replace(temporary, target)

    def _path(self, target_id: StableId) -> Path:
        return self._root / f"{target_id.root}.active.json"

    @staticmethod
    def _payload(artifact: ArtifactRef) -> str:
        return (
            json.dumps(
                artifact.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True
            )
            + "\n"
        )


__all__ = ["FilesystemEvolutionVersionRegistry"]
