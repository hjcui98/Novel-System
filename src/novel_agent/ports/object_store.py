"""Object storage boundary used by the application core."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ObjectStat:
    key: str
    byte_length: int
    media_type: str


class ObjectNotFoundError(LookupError):
    """The requested object key does not exist."""


class ObjectMetadataError(RuntimeError):
    """Stored object metadata is absent or malformed."""


class ObjectStorePort(Protocol):
    """Minimal replaceable S3-compatible object store contract."""

    def put_if_absent(self, key: str, data: bytes, media_type: str) -> ObjectStat: ...

    def get(self, key: str) -> bytes: ...

    def stat(self, key: str) -> ObjectStat: ...
