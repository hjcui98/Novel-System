from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import pytest
from minio import Minio
from minio.error import S3Error
from urllib3.response import BaseHTTPResponse

from novel_agent.adapters.minio import MinioObjectStore
from novel_agent.ports.object_store import ObjectMetadataError, ObjectNotFoundError


def s3_error(code: str) -> S3Error:
    return S3Error(cast(BaseHTTPResponse, object()), code, "message", None, None, None)


def test_minio_adapter_creates_bucket_only_when_missing() -> None:
    client = MagicMock(spec=Minio)
    store = MinioObjectStore(cast(Minio, client), "artifacts")

    client.bucket_exists.return_value = False
    store.ensure_bucket()
    client.make_bucket.assert_called_once_with("artifacts")

    client.reset_mock()
    client.bucket_exists.return_value = True
    store.ensure_bucket()
    client.make_bucket.assert_not_called()


def test_minio_adapter_returns_existing_object_without_rewriting() -> None:
    client = MagicMock(spec=Minio)
    stat = MagicMock(size=4, content_type="text/plain")
    client.stat_object.return_value = stat
    store = MinioObjectStore(cast(Minio, client), "artifacts")

    result = store.put_if_absent("key", b"data", "text/plain")

    assert result.byte_length == 4
    client.put_object.assert_not_called()


def test_minio_adapter_writes_missing_object() -> None:
    client = MagicMock(spec=Minio)
    client.stat_object.side_effect = s3_error("NoSuchKey")
    store = MinioObjectStore(cast(Minio, client), "artifacts")

    result = store.put_if_absent("key", b"data", "text/plain")

    assert result.media_type == "text/plain"
    client.put_object.assert_called_once()


def test_minio_adapter_reads_and_closes_response() -> None:
    client = MagicMock(spec=Minio)
    response = MagicMock()
    response.read.return_value = b"data"
    client.get_object.return_value = response
    store = MinioObjectStore(cast(Minio, client), "artifacts")

    assert store.get("key") == b"data"
    response.close.assert_called_once()
    response.release_conn.assert_called_once()


def test_minio_adapter_maps_missing_objects_and_preserves_other_errors() -> None:
    client = MagicMock(spec=Minio)
    store = MinioObjectStore(cast(Minio, client), "artifacts")

    client.get_object.side_effect = s3_error("NoSuchObject")
    with pytest.raises(ObjectNotFoundError):
        store.get("missing")

    client.get_object.side_effect = s3_error("AccessDenied")
    with pytest.raises(S3Error, match="message"):
        store.get("private")

    client.stat_object.side_effect = s3_error("NoSuchBucket")
    with pytest.raises(ObjectNotFoundError):
        store.stat("missing")

    client.stat_object.side_effect = s3_error("AccessDenied")
    with pytest.raises(S3Error, match="message"):
        store.stat("private")


def test_minio_stat_uses_binary_default_media_type() -> None:
    client = MagicMock(spec=Minio)
    client.stat_object.return_value = MagicMock(size=3, content_type=None)
    store = MinioObjectStore(cast(Minio, client), "artifacts")

    result = store.stat("key")

    assert result.byte_length == 3
    assert result.media_type == "application/octet-stream"


def test_minio_stat_rejects_missing_size_metadata() -> None:
    client = MagicMock(spec=Minio)
    client.stat_object.return_value = MagicMock(size=None, content_type="text/plain")
    store = MinioObjectStore(cast(Minio, client), "artifacts")

    with pytest.raises(ObjectMetadataError, match="has no size"):
        store.stat("key")
