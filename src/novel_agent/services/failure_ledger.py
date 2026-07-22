"""Content-addressed Stage 2 Bootstrap/Controller/Curator failure ledgers."""

from __future__ import annotations

from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.stage2 import (
    FailureLedgerDocument,
    FailureLedgerEntry,
    FailureLedgerRef,
    FailureLedgerType,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes


class FailureLedgerService:
    def __init__(self, artifacts: ArtifactRepository, schema_version: SchemaVersion) -> None:
        self._artifacts = artifacts
        self._schema_version = schema_version

    def persist(
        self,
        ledger_type: FailureLedgerType,
        configuration_fingerprint: ArtifactId,
        entries: tuple[FailureLedgerEntry, ...],
    ) -> FailureLedgerRef:
        document = FailureLedgerDocument(
            ledger_id=StableId(f"failure-ledger.{ledger_type.value}"),
            ledger_type=ledger_type,
            configuration_fingerprint=configuration_fingerprint,
            entries=entries,
        )
        artifact = self._artifacts.put(
            canonical_json_bytes(document.model_dump(mode="json")),
            "application/vnd.novel-agent.failure-ledger+json",
            self._schema_version,
        )
        return FailureLedgerRef(
            ledger_type=ledger_type,
            artifact=artifact,
            entry_count=len(entries),
        )
