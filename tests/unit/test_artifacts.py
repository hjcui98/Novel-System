from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.ids import ArtifactId, SchemaVersion
from novel_agent.ports.object_store import ObjectMetadataError, ObjectNotFoundError
from novel_agent.services.artifacts import (
    ArtifactIntegrityError,
    ArtifactRepository,
    object_key,
    sha256_id,
)


def test_artifact_round_trip_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    store = FilesystemObjectStore(tmp_path)
    repository = ArtifactRepository(store)
    content = "不可变正文".encode()

    first = repository.put(content, "text/plain; charset=utf-8", SchemaVersion("0.1.0"))
    second = repository.put(content, "text/plain; charset=utf-8", SchemaVersion("0.1.0"))

    assert first == second
    assert first.artifact_id == sha256_id(content)
    assert repository.read_verified(first) == content
    assert object_key(first.artifact_id).startswith("sha256/")


def test_artifact_projection_reuses_existing_media_type_for_identical_content(
    tmp_path: Path,
) -> None:
    store = FilesystemObjectStore(tmp_path)
    repository = ArtifactRepository(store)
    content = b"projected prose"
    version = SchemaVersion("0.1.0")

    existing = repository.put(content, "application/vnd.novel-agent.draft-text+plain", version)
    projected = repository.put_or_reuse_existing(
        content,
        "application/vnd.novel-agent.recent-chapter-text+plain",
        version,
    )

    assert projected.artifact_id == existing.artifact_id
    assert projected.media_type == existing.media_type
    assert repository.read_verified(projected) == content


def test_artifact_verification_detects_content_tampering(tmp_path: Path) -> None:
    store = FilesystemObjectStore(tmp_path)
    repository = ArtifactRepository(store)
    artifact = repository.put(b"canonical", "text/plain", SchemaVersion("0.1.0"))
    content_path = tmp_path / object_key(artifact.artifact_id)
    content_path.write_bytes(b"tampered!")

    with pytest.raises(ArtifactIntegrityError, match="content hash"):
        repository.read_verified(artifact)


def test_artifact_verification_detects_length_and_media_type_mismatch(tmp_path: Path) -> None:
    store = FilesystemObjectStore(tmp_path)
    repository = ArtifactRepository(store)
    artifact = repository.put(b"canonical", "text/plain", SchemaVersion("0.1.0"))
    metadata_path = (tmp_path / object_key(artifact.artifact_id)).with_suffix(".metadata.json")

    metadata_path.write_text(
        json.dumps({"byte_length": 1, "media_type": "text/plain"}), encoding="utf-8"
    )
    with pytest.raises(ArtifactIntegrityError, match="byte length"):
        repository.read_verified(artifact)

    metadata_path.write_text(
        json.dumps({"byte_length": artifact.byte_length, "media_type": "image/png"}),
        encoding="utf-8",
    )
    with pytest.raises(ArtifactIntegrityError, match="media type"):
        repository.read_verified(artifact)


def test_artifact_verification_reports_missing_or_corrupt_metadata(tmp_path: Path) -> None:
    store = FilesystemObjectStore(tmp_path)
    repository = ArtifactRepository(store)
    artifact = repository.put(b"canonical", "text/plain", SchemaVersion("0.1.0"))
    content_path = tmp_path / object_key(artifact.artifact_id)
    metadata_path = content_path.with_suffix(".metadata.json")

    content_path.unlink()
    with pytest.raises(ArtifactIntegrityError, match="missing"):
        repository.read_verified(artifact)

    content_path.write_bytes(b"canonical")
    metadata_path.write_text("not-json", encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError, match="missing"):
        repository.read_verified(artifact)


def test_filesystem_store_rejects_unsafe_keys_and_missing_objects(tmp_path: Path) -> None:
    store = FilesystemObjectStore(tmp_path)

    with pytest.raises(ValueError, match="safe relative"):
        store.get("../outside")
    with pytest.raises(ObjectNotFoundError):
        store.get("sha256/aa/missing")
    with pytest.raises(ObjectNotFoundError):
        store.stat("sha256/aa/missing")


def test_filesystem_store_rejects_incomplete_metadata(tmp_path: Path) -> None:
    store = FilesystemObjectStore(tmp_path)
    key = "sha256/aa/object"
    content_path = tmp_path / key
    content_path.parent.mkdir(parents=True)
    content_path.write_bytes(b"data")
    metadata_path = content_path.with_suffix(".metadata.json")
    metadata_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ObjectMetadataError):
        store.stat(key)


def test_repository_rejects_inconsistent_store_metadata() -> None:
    class InconsistentStore:
        def put_if_absent(self, key: str, data: bytes, media_type: str) -> object:
            from novel_agent.ports.object_store import ObjectStat

            return ObjectStat(key=key, byte_length=len(data) + 1, media_type=media_type)

        def get(self, key: str) -> bytes:
            raise AssertionError(key)

        def stat(self, key: str) -> object:
            raise AssertionError(key)

    repository = ArtifactRepository(InconsistentStore())  # type: ignore[arg-type]
    with pytest.raises(ArtifactIntegrityError, match="metadata differs"):
        repository.put(b"data", "text/plain", SchemaVersion("0.1.0"))


def test_object_key_uses_validated_sha256_identity() -> None:
    artifact_id = ArtifactId("sha256:" + "a" * 64)
    assert object_key(artifact_id) == f"sha256/aa/{'a' * 64}"


@given(st.binary())
def test_sha256_identity_is_deterministic_and_maps_to_its_content_key(content: bytes) -> None:
    first = sha256_id(content)
    second = sha256_id(bytes(content))

    assert first == second
    digest = first.root.removeprefix("sha256:")
    assert object_key(first) == f"sha256/{digest[:2]}/{digest}"
