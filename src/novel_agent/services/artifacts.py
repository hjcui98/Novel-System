"""Content-addressed Artifact creation and integrity verification."""

import hashlib

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, SchemaVersion
from novel_agent.ports.object_store import (
    ObjectMetadataError,
    ObjectNotFoundError,
    ObjectStorePort,
)


class ArtifactIntegrityError(RuntimeError):
    """Stored content or metadata does not match its ArtifactRef."""


def sha256_id(data: bytes) -> ArtifactId:
    return ArtifactId(f"sha256:{hashlib.sha256(data).hexdigest()}")


def object_key(artifact_id: ArtifactId) -> str:
    digest = artifact_id.root.removeprefix("sha256:")
    return f"sha256/{digest[:2]}/{digest}"


class ArtifactRepository:
    """Store immutable bytes under their SHA-256 identity."""

    def __init__(self, object_store: ObjectStorePort) -> None:
        self._object_store = object_store

    def put(self, data: bytes, media_type: str, schema_version: SchemaVersion) -> ArtifactRef:
        artifact_id = sha256_id(data)
        stored = self._object_store.put_if_absent(object_key(artifact_id), data, media_type)
        if stored.byte_length != len(data) or stored.media_type != media_type:
            raise ArtifactIntegrityError("object store metadata differs from produced artifact")
        return ArtifactRef(
            artifact_id=artifact_id,
            media_type=media_type,
            byte_length=len(data),
            schema_version=schema_version,
        )

    def read_verified(self, artifact: ArtifactRef) -> bytes:
        key = object_key(artifact.artifact_id)
        try:
            stored = self._object_store.stat(key)
            data = self._object_store.get(key)
        except (ObjectNotFoundError, ObjectMetadataError) as error:
            raise ArtifactIntegrityError("artifact object is missing") from error
        if stored.byte_length != artifact.byte_length or len(data) != artifact.byte_length:
            raise ArtifactIntegrityError("artifact byte length does not match metadata")
        if stored.media_type != artifact.media_type:
            raise ArtifactIntegrityError("artifact media type does not match metadata")
        if sha256_id(data) != artifact.artifact_id:
            raise ArtifactIntegrityError("artifact content hash verification failed")
        return data
