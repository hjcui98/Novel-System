from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.ids import ArtifactId, RunId, SchemaVersion, StableId, TaskId
from novel_agent.domain.stage2 import (
    FailureLedgerDocument,
    FailureLedgerEntry,
    FailureLedgerType,
    FailureSeverity,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.failure_ledger import FailureLedgerService

VERSION = SchemaVersion("2.0.0")
HASH = ArtifactId("sha256:" + "a" * 64)
NOW = datetime(2026, 7, 21, tzinfo=UTC)


def entry(identity: str = "failure.1") -> FailureLedgerEntry:
    return FailureLedgerEntry(
        failure_id=StableId(identity),
        code="MANDATORY_GAP",
        severity=FailureSeverity.BLOCKER,
        message="mandatory memory gap remained unresolved",
        run_id=RunId("run.failure"),
        task_id=TaskId("task.failure"),
        occurred_at=NOW,
    )


def test_failure_ledger_service_persists_all_three_typed_ledgers(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    service = FailureLedgerService(repository, VERSION)
    refs = tuple(
        service.persist(kind, HASH, (entry(f"failure.{kind.value}"),)) for kind in FailureLedgerType
    )

    assert {item.ledger_type for item in refs} == set(FailureLedgerType)
    assert all(item.entry_count == 1 for item in refs)
    assert all(repository.read_verified(item.artifact) for item in refs)


def test_failure_ledger_contract_rejects_duplicate_failure_identity() -> None:
    with pytest.raises(ValidationError, match="entry ids must be unique"):
        FailureLedgerDocument(
            ledger_id=StableId("failure-ledger.controller"),
            ledger_type=FailureLedgerType.CONTROLLER,
            configuration_fingerprint=HASH,
            entries=(entry(), entry()),
        )
