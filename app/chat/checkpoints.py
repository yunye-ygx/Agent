"""LangGraph checkpoint lifecycle backed by the dedicated PostgreSQL service."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# The LangGraph serializer reads this flag while loading checkpoint values. Keep
# strict deserialization enabled even when a caller forgot to export it.
os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


@asynccontextmanager
async def open_postgres_checkpointer(
    connection_string: str,
    *,
    setup: bool = False,
) -> AsyncIterator[AsyncPostgresSaver]:
    """Open a saver and optionally install/update its private checkpoint schema."""

    async with AsyncPostgresSaver.from_conn_string(connection_string) as saver:
        if setup:
            await saver.setup()
        yield saver

