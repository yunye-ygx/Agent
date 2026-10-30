"""Bounded, owner-isolated MinIO snapshots; expiration is enforced on every read."""

import asyncio
import json
import re
import time

from minio import Minio
from minio.error import S3Error
from urllib3 import PoolManager, Timeout

from app.core.config import settings
from app.storage.minio_storage import MinioStorage

MAX_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
PREFIX = "a2-experiments/"


class SnapshotStore:
    def __init__(self, storage=None, clock=time.time):
        self.storage = storage or MinioStorage(
            client=Minio(
                settings.minio_endpoint,
                access_key=settings.minio_access_key,
                secret_key=settings.minio_secret_key,
                secure=settings.minio_secure,
                http_client=PoolManager(timeout=Timeout(connect=3, read=10), retries=False),
            )
        )
        self.bucket = settings.minio_artifacts_bucket
        self.clock = clock
        self.lock = asyncio.Lock()

    def key(self, owner, identity):
        if not re.fullmatch(r"[a-f0-9]{64}", owner) or not re.fullmatch(r"[a-f0-9-]{36}", identity):
            raise ValueError("实验标识不合法")
        return f"{PREFIX}{owner}/{identity}.json"

    async def get(self, owner, identity):
        key = self.key(owner, identity)
        try:

            def read():
                response = self.storage.client.get_object(self.bucket, key)
                try:
                    data = response.read(MAX_BYTES + 1)
                    if len(data) > MAX_BYTES:
                        raise ValueError("快照超过资源上限")
                    return json.loads(data)
                finally:
                    response.close()
                    response.release_conn()

            value = await asyncio.to_thread(read)
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject"):
                return None
            raise
        if value["report"]["expires_at"] <= self.clock():
            return None
        return value

    async def put(self, owner, value):
        key = self.key(owner, value["report"]["experiment_id"])
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        if len(data) > MAX_BYTES:
            raise ValueError("快照超过 20 MiB 上限")
        async with self.lock:
            # Garbage collection is confined to A2's prefix. During idle periods
            # expired objects remain inaccessible and are reclaimed on the next write.
            def budget_and_cleanup():
                total, count = 0, 0
                for obj in self.storage.client.list_objects(
                    self.bucket, prefix=PREFIX, recursive=True
                ):
                    if obj.last_modified.timestamp() + 1800 <= self.clock():
                        self.storage.client.remove_object(self.bucket, obj.object_name)
                    elif obj.object_name != key:
                        total += obj.size
                        count += 1
                    if count >= 128 or total + len(data) > MAX_TOTAL_BYTES:
                        raise ValueError("实验存储容量已满，请等待过期清理")

            await asyncio.to_thread(budget_and_cleanup)
            await self.storage.put_bytes(self.bucket, key, data, "application/json")

    async def delete_knowledge_base_snapshots(self, knowledge_base_id):
        def remove_matching() -> None:
            for item in self.storage.client.list_objects(
                self.bucket, prefix=PREFIX, recursive=True
            ):
                response = self.storage.client.get_object(self.bucket, item.object_name)
                try:
                    value = json.loads(response.read(MAX_BYTES + 1))
                finally:
                    response.close()
                    response.release_conn()
                scope = value.get("report", {}).get("scope", {})
                if scope.get("kb_id") == knowledge_base_id:
                    self.storage.client.remove_object(self.bucket, item.object_name)

        await asyncio.to_thread(remove_matching)
