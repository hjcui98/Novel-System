"""Atomic filesystem ledger for one-shot U8-E sealed checkpoints."""

from __future__ import annotations

import json
from pathlib import Path

from novel_agent.domain.ids import StableId
from novel_agent.services.evolution import SealedAcceptanceAlreadyOpened


class FilesystemSealedAcceptanceLedger:
    def __init__(self, root: Path) -> None:
        self._root = root

    def claim(self, campaign_id: StableId, checkpoint_id: StableId) -> None:
        directory = self._root / campaign_id.root
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{checkpoint_id.root}.opened.json"
        payload = (
            json.dumps(
                {
                    "campaign_id": campaign_id.root,
                    "checkpoint_id": checkpoint_id.root,
                    "status": "OPENED",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        try:
            with target.open("x", encoding="utf-8") as handle:
                handle.write(payload)
        except FileExistsError as error:
            raise SealedAcceptanceAlreadyOpened(
                f"sealed checkpoint {checkpoint_id.root} was already opened"
            ) from error


__all__ = ["FilesystemSealedAcceptanceLedger"]
