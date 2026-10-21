from __future__ import annotations

import asyncio

import aio_pika

from app.core.config import settings
from app.db.mysql_schema import create_mysql_schema
from app.messaging.topology import declare_topology
from app.storage.elasticsearch_chunks import ChunkStore
from app.storage.minio_storage import MinioStorage


async def initialize_infrastructure() -> None:
    await create_mysql_schema()
    await MinioStorage().ensure_buckets()
    chunk_store = ChunkStore()
    try:
        await chunk_store.ensure_index()
    finally:
        await chunk_store.close()
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    try:
        channel = await connection.channel()
        await declare_topology(channel)
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(initialize_infrastructure())
