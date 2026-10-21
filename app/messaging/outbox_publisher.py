from __future__ import annotations

import asyncio
import logging

import aio_pika
from pamqp.commands import Basic
from sqlalchemy import select

from app.core.config import settings
from app.db import mysql_repository as repository
from app.db.mysql_models import (
    Document,
    DocumentStatus,
    Generation,
    GenerationStatus,
    OutboxStatus,
    Task,
    TaskDispatchOutbox,
    TaskStatus,
)
from app.db.mysql_session import AsyncSessionFactory
from app.messaging.topology import build_message, declare_topology

logger = logging.getLogger(__name__)


class OutboxPublisher:
    def __init__(self, *, session_factory=None):
        self.session_factory = session_factory or AsyncSessionFactory

    async def publish_once(self):
        async with self.session_factory() as session:
            candidates = list(
                (
                    await session.execute(
                        select(Task.id, Task.dispatch_seq)
                        .join(TaskDispatchOutbox, TaskDispatchOutbox.task_id == Task.id)
                        .join(Generation, Generation.id == Task.generation_id)
                        .join(Document, Document.id == Task.document_id)
                        .where(
                            TaskDispatchOutbox.status == OutboxStatus.PENDING,
                            Task.status == TaskStatus.WAITING_PUBLISH,
                            Task.attempt_count < Task.max_attempts,
                            Task.next_attempt_at <= repository.utc_now(),
                            Generation.status == GenerationStatus.RUNNING,
                            Document.status == DocumentStatus.ACTIVE,
                            Document.current_run_id == Generation.id,
                            TaskDispatchOutbox.next_attempt_at <= repository.utc_now(),
                        )
                        .order_by(TaskDispatchOutbox.next_attempt_at)
                        .limit(100)
                    )
                ).all()
            )
        if not candidates:
            return 0
        connection = await aio_pika.connect_robust(settings.rabbitmq_url)
        try:
            channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            exchange, _queue = await declare_topology(channel)
            published = 0
            for task_id, seq in candidates:
                async with self.session_factory() as session:
                    payload = await repository.reserve_dispatch(session, task_id, seq)
                    await session.commit()
                if payload is None:
                    continue
                try:
                    confirm = await exchange.publish(
                        build_message(payload),
                        routing_key=settings.rabbitmq_routing_key,
                        mandatory=True,
                        timeout=settings.model_timeout_seconds,
                    )
                    if (
                        confirm is None
                        or confirm is False
                        or isinstance(confirm, (Basic.Nack, Basic.Reject))
                    ):
                        raise RuntimeError("RabbitMQ 未确认消息已可靠路由")
                except Exception:
                    # Reservation already saved a resend deadline. No execution
                    # attempts are spent on a publish error or uncertain confirm.
                    logger.exception(
                        "Task %s dispatch %s failed; compensation remains durable", task_id, seq
                    )
                    continue
                async with self.session_factory() as session:
                    accepted = await repository.confirm_dispatch(session, task_id, seq)
                    await session.commit()
                    published += int(accepted)
            return published
        finally:
            await connection.close()


async def run_publisher():
    while True:
        try:
            await OutboxPublisher().publish_once()
        except Exception:
            logger.exception("Outbox publish failed")
        await asyncio.sleep(max(0.1, settings.dispatch_interval_seconds))


if __name__ == "__main__":
    asyncio.run(run_publisher())
