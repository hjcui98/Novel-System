"""MinIO/S3-compatible implementation of ObjectStorePort."""

from io import BytesIO

from minio import Minio
from minio.error import S3Error

from novel_agent.ports.object_store import ObjectMetadataError, ObjectNotFoundError, ObjectStat


class MinioObjectStore:
    def __init__(self, client: Minio, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def ensure_bucket(self) -> None:
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)

    def put_if_absent(self, key: str, data: bytes, media_type: str) -> ObjectStat:
        try:
            return self.stat(key)
        except ObjectNotFoundError:
            self._client.put_object(
                self._bucket,
                key,
                BytesIO(data),
                length=len(data),
                content_type=media_type,
            )
            return ObjectStat(key=key, byte_length=len(data), media_type=media_type)

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(self._bucket, key)
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject"}:
                raise ObjectNotFoundError(key) from error
            raise
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def stat(self, key: str) -> ObjectStat:
        try:
            result = self._client.stat_object(self._bucket, key)
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
                raise ObjectNotFoundError(key) from error
            raise
        if result.size is None:
            raise ObjectMetadataError(f"object {key!r} has no size")
        return ObjectStat(
            key=key,
            byte_length=result.size,
            media_type=result.content_type or "application/octet-stream",
        )
