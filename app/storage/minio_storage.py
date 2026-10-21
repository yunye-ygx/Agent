from __future__ import annotations

import asyncio
import io
from datetime import timedelta

from minio import Minio
from minio.error import S3Error

from app.core.config import Settings, settings


class MinioStorageError(RuntimeError):
    pass


class MinioStorage:
    def __init__(self, config: Settings = settings, client: Minio | None = None) -> None:
        self.config = config
        self.client = client or Minio(
            config.minio_endpoint,
            access_key=config.minio_access_key,
            secret_key=config.minio_secret_key,
            secure=config.minio_secure,
        )

    async def ensure_buckets(self) -> None:
        await asyncio.to_thread(self._ensure_buckets)

    def _ensure_buckets(self) -> None:
        for bucket in (self.config.minio_files_bucket, self.config.minio_artifacts_bucket):
            if not self.client.bucket_exists(bucket):
                self.client.make_bucket(bucket)

    async def put_bytes(
        self, bucket: str, object_key: str, content: bytes, content_type: str
    ) -> None:
        await asyncio.to_thread(
            self.client.put_object,
            bucket,
            object_key,
            io.BytesIO(content),
            length=len(content),
            content_type=content_type,
        )

    async def get_bytes(self, bucket: str, object_key: str) -> bytes:
        def read() -> bytes:
            response = self.client.get_object(bucket, object_key)
            try:
                data = response.read(self.config.chunking_max_file_bytes + 1)
                if len(data) > self.config.chunking_max_file_bytes:
                    raise MinioStorageError("原文件超过配置的资源上限")
                return data
            finally:
                response.close()
                response.release_conn()

        try:
            return await asyncio.to_thread(read)
        except S3Error as exc:
            raise MinioStorageError(f"MinIO 对象不存在：{bucket}/{object_key}") from exc

    async def presigned_get_url(
        self, bucket: str, object_key: str, expires_seconds: int = 3600
    ) -> str:
        return await asyncio.to_thread(
            self.client.presigned_get_object,
            bucket,
            object_key,
            expires=timedelta(seconds=expires_seconds),
        )

    async def delete_object(self, bucket: str, object_key: str) -> None:
        await asyncio.to_thread(self.client.remove_object, bucket, object_key)
