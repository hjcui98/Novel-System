"""Local deterministic ObjectStore adapter for unit tests and offline development."""

import json
import os
import tempfile
from pathlib import Path

from novel_agent.ports.object_store import ObjectMetadataError, ObjectNotFoundError, ObjectStat


class FilesystemObjectStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def put_if_absent(self, key: str, data: bytes, media_type: str) -> ObjectStat:
        content_path, metadata_path = self._paths(key)
        if content_path.exists() and metadata_path.exists():
            return self.stat(key)
        content_path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(content_path, data)
        metadata = json.dumps(
            {"byte_length": len(data), "media_type": media_type},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        self._atomic_write(metadata_path, metadata)
        return ObjectStat(key=key, byte_length=len(data), media_type=media_type)

    def get(self, key: str) -> bytes:
        content_path, _ = self._paths(key)
        try:
            return content_path.read_bytes()
        except FileNotFoundError as error:
            raise ObjectNotFoundError(key) from error

    def stat(self, key: str) -> ObjectStat:
        content_path, metadata_path = self._paths(key)
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            content_path.stat()
            byte_length = int(metadata["byte_length"])
            media_type = str(metadata["media_type"])
        except FileNotFoundError as error:
            raise ObjectNotFoundError(key) from error
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ObjectMetadataError(key) from error
        return ObjectStat(
            key=key,
            byte_length=byte_length,
            media_type=media_type,
        )

    def _paths(self, key: str) -> tuple[Path, Path]:
        if key.startswith("/") or ".." in Path(key).parts:
            raise ValueError("object key must be a safe relative path")
        content_path = self._root / key
        return content_path, content_path.with_suffix(content_path.suffix + ".metadata.json")

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)
