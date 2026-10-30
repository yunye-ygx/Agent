"""RabbitMQ consumer whose ACK boundary is the committed MySQL lease claim."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import aio_pika

from app.chat import lifecycle
from app.chat.contracts import RunLease
from app.core.config import settings
from app.db.mysql_session import AsyncSessionFactory
from app.messaging.chat_topology import declare_chat_topology

logger = logging.getLogger(__name__)


def parse_dispatch_payload(body: bytes) -> dict:
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("notification must be an object")
    if not isinstance(payload.get("run_id"), str) or not payload["run_id"]:
        raise ValueError("invalid run id")
    if payload.get("dispatch_reason") not in {
        "initial",
        "recovery",
        "cancel_requested",
        "failure_retry",
        "cancel_retry",
        "regenerate",
    }:
        raise ValueError("invalid dispatch reason")
    if type(payload.get("dispatch_generation")) is not int:
        raise ValueError("invalid dispatch generation")
    if payload["dispatch_generation"] < 0 or payload.get("schema_version") != 1:
        raise ValueError("unsupported dispatch schema")
    return payload


class ChatRunProcessor:
    def __init__(self, *, session_factory=None, worker_id: str | None = None):
        self.session_factory = session_factory or AsyncSessionFactory
        self.worker_id = worker_id or f"chat-worker-{uuid.uuid4()}"

    async def claim(self, payload: dict) -> RunLease | None:
        async with self.session_factory() as session:
            run = await lifecycle.claim_run(
                session,
                payload["run_id"],
                worker_id=self.worker_id,
                dispatch_generation=payload["dispatch_generation"],
                lease_seconds=settings.chat_lease_seconds,
            )
        if run is None:
            return None
        return RunLease(run.id, run.turn_id, self.worker_id, run.lease_generation)

    async def execute(self, lease: RunLease) -> None:
        from app.chat.executor import execute_run

        await execute_run(lease, session_factory=self.session_factory)


async def handle_chat_message(message, processor: ChatRunProcessor) -> None:
    try:
        payload = parse_dispatch_payload(message.body)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError):
        await message.reject(requeue=False)
        return
    try:
        lease = await processor.claim(payload)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("chat run claim failed")
        await message.nack(requeue=True)
        return

    # A committed claim is now durable. RabbitMQ is notification transport only;
    # execution recovery belongs to the lease scanner and PostgreSQL checkpoint.
    await message.ack()
    if lease is None:
        return
    try:
        await processor.execute(lease)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "chat run execution stopped after ACK",
            extra={
                "run_id": lease.run_id,
                "worker_id": lease.worker_id,
                "lease_generation": lease.lease_generation,
            },
        )


async def run_chat_worker() -> None:
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    processor = ChatRunProcessor()
    gate = asyncio.Semaphore(max(1, settings.chat_worker_concurrency))
    in_flight: set[asyncio.Task] = set()

    async def consume(message) -> None:
        try:
            await handle_chat_message(message, processor)
        finally:
            gate.release()

    try:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=max(1, settings.chat_worker_prefetch_count))
        _exchange, queue = await declare_chat_topology(channel)
        async with queue.iterator() as messages:
            async for message in messages:
                await gate.acquire()
                task = asyncio.create_task(consume(message))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
    finally:
        for task in tuple(in_flight):
            task.cancel()
        await asyncio.gather(*in_flight, return_exceptions=True)
        await connection.close()

