"""Startup dependency probing.

Business tables and MySQL are required: if that check fails the application must
not report ready. MinIO, Elasticsearch and RabbitMQ only serve knowledge/RAG
features; when they are unavailable, login/orders/devices/tickets still work and
the knowledge endpoint reports a clear failure.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import aio_pika

from app.core.config import settings
from app.db.mysql_schema import create_mysql_schema
from app.messaging.topology import declare_topology
from app.storage.elasticsearch_chunks import ChunkStore
from app.storage.minio_storage import MinioStorage

logger = logging.getLogger(__name__)


async def _probe(name: str, components: dict[str, bool], action: Callable[[], Awaitable[None]]):
    try:
        await action()
    except Exception:
        # Log only the component name; exception text can contain endpoints/keys.
        logger.exception("RAG dependency unavailable: %s", name)
        components[name] = False
    else:
        components[name] = True


async def initialize_infrastructure() -> dict:
    await create_mysql_schema()
    components: dict[str, bool] = {}
    await _probe("minio", components, lambda: MinioStorage().ensure_buckets())

    async def ensure_chunks() -> None:
        store = ChunkStore()
        try:
            await store.ensure_index()
        finally:
            await store.close()

    await _probe("elasticsearch", components, ensure_chunks)

    async def ensure_rabbit() -> None:
        connection = await aio_pika.connect_robust(settings.rabbitmq_url)
        try:
            channel = await connection.channel()
            await declare_topology(channel)
        finally:
            await connection.close()

    await _probe("rabbitmq", components, ensure_rabbit)
    ready = all(components.values())
    if not ready:
        logger.warning(
            "Knowledge/RAG dependencies are unavailable: %s",
            ", ".join(name for name, ok in components.items() if not ok),
        )
    return {
        "business": {"ready": True},
        "rag": {
            "ready": ready,
            "components": components,
            "message": "知识检索依赖就绪" if ready else "知识检索依赖不可用",
        },
    }


if __name__ == "__main__":
    import asyncio

    print(asyncio.run(initialize_infrastructure()))
