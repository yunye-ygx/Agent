from __future__ import annotations

import asyncio
import json
import logging

import aio_pika

from app.core.config import settings
from app.messaging.topology import declare_topology
from app.rag.task_processor_service import TaskProcessor

logger = logging.getLogger(__name__)


async def handle_message(message, processor):
    try:
        payload = json.loads(message.body.decode("utf-8"))
        for field in ("task_id", "doc_id", "run_id"):
            if not isinstance(payload[field], str) or not payload[field]:
                raise ValueError("invalid task notification")
        if type(payload["dispatch_seq"]) is not int or payload["dispatch_seq"] < 1:
            raise ValueError("invalid dispatch sequence")
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        await message.reject(requeue=False)
        return
    try:
        result = await processor.process(
            payload["task_id"],
            doc_id=payload["doc_id"],
            run_id=payload["run_id"],
            dispatch_seq=payload["dispatch_seq"],
        )
        if not result.acknowledged:
            raise RuntimeError("任务结果或恢复决定尚未可靠保存")
    except asyncio.CancelledError:
        # Closing the channel re-delivers unacked notifications. The durable
        # lease scanner recovers any attempt that was claimed before shutdown.
        raise
    except Exception:
        logger.exception("Task notification processing failed; not acknowledging")
        await asyncio.sleep(max(0.1, settings.dispatch_interval_seconds))
        await message.nack(requeue=True)
        return
    await message.ack()


async def run_worker():
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    processor = TaskProcessor()
    in_flight = set()
    gate = asyncio.Semaphore(max(1, settings.worker_concurrency))

    async def consume(message):
        try:
            await handle_message(message, processor)
        finally:
            gate.release()

    try:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=max(1, settings.worker_prefetch_count))
        _, queue = await declare_topology(channel)
        async with queue.iterator() as messages:
            async for message in messages:
                await gate.acquire()
                work = asyncio.create_task(consume(message))
                in_flight.add(work)

                def done(task):
                    in_flight.discard(task)
                    if not task.cancelled() and task.exception():
                        logger.error("Worker delivery handler stopped: %s", task.exception())

                work.add_done_callback(done)
    finally:
        for work in tuple(in_flight):
            work.cancel()
        await asyncio.gather(*in_flight, return_exceptions=True)
        await processor.chunk_store.close()
        await connection.close()


if __name__ == "__main__":
    asyncio.run(run_worker())
