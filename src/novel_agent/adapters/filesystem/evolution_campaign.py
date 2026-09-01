"""Write-once filesystem persistence for U8-E campaign manifests."""

from __future__ import annotations

import json
from pathlib import Path

from novel_agent.domain.evolution import EvolutionCampaignManifest


class EvolutionManifestAlreadyFrozen(RuntimeError):
    """The campaign identity already has a frozen manifest."""


class FilesystemEvolutionCampaignRepository:
    def __init__(self, root: Path) -> None:
        self._root = root

    def freeze(self, manifest: EvolutionCampaignManifest) -> Path:
        directory = self._root / manifest.campaign_id.root
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "manifest.json"
        payload = (
            json.dumps(
                manifest.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
        try:
            with target.open("x", encoding="utf-8") as handle:
                handle.write(payload)
        except FileExistsError as error:
            raise EvolutionManifestAlreadyFrozen(
                f"evolution campaign {manifest.campaign_id.root} is already frozen"
            ) from error
        return target

    def load(self, campaign_id: str) -> EvolutionCampaignManifest:
        target = self._root / campaign_id / "manifest.json"
        return EvolutionCampaignManifest.model_validate_json(target.read_bytes(), strict=True)


__all__ = ["EvolutionManifestAlreadyFrozen", "FilesystemEvolutionCampaignRepository"]
