"""Local filesystem adapters."""

from novel_agent.adapters.filesystem.evolution_campaign import (
    EvolutionManifestAlreadyFrozen,
    FilesystemEvolutionCampaignRepository,
)
from novel_agent.adapters.filesystem.evolution_versions import FilesystemEvolutionVersionRegistry
from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.filesystem.sealed_acceptance import FilesystemSealedAcceptanceLedger

__all__ = [
    "EvolutionManifestAlreadyFrozen",
    "FilesystemEvolutionCampaignRepository",
    "FilesystemEvolutionVersionRegistry",
    "FilesystemObjectStore",
    "FilesystemSealedAcceptanceLedger",
]
