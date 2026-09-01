"""Focused contracts for the isolated U4-S readout runner namespace."""

from pathlib import Path

from scripts.run_u4s_readout_campaign import _copy_canonical_refs

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.ids import SchemaVersion
from novel_agent.services.artifacts import ArtifactRepository


def test_copy_canonical_refs_preserves_content_identity(tmp_path: Path) -> None:
    source = ArtifactRepository(FilesystemObjectStore(tmp_path / "source"))
    destination = ArtifactRepository(FilesystemObjectStore(tmp_path / "destination"))
    ref = source.put(b"canonical-root", "application/json", SchemaVersion("1.0.0"))

    _copy_canonical_refs(source, destination, (ref,))

    assert destination.read_verified(ref) == b"canonical-root"
