"""Transactional Chat Outbox publisher with confirms and retryable locks."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta

import aio_pika
from pamqp.commands import Basic
from sqlalchemy import and_, or_, select, update

from app.business.utils import utc_now
from app.chat.models import ChatOutboxEvent
from app.core.config import settings
from app.db.mysql_session import AsyncSessionFactory
from app.messaging.chat_topology import build_chat_message, declare_chat_topology

logger = logging.getLogger(__name__)


class ChatOutboxPublisher:
    def __init__(self, *, session_factory=None, publisher_id: str | None = None):
        self.session_factory = session_factory or AsyncSessionFactory
        self.publisher_id = publisher_id or f"publisher-{uuid.uuid4()}"

    async def _candidate_ids(self) -> list[str]:
        now = utc_now()
        stale = now - timedelta(seconds=settings.chat_outbox_lock_seconds)
        async with self.session_factory() as session:
            return list(
                (
                    await session.execute(
                        select(ChatOutboxEvent.id)
                        .where(
                            ChatOutboxEvent.attempt_count
                            < settings.chat_outbox_max_attempts,
                            or_(
                                and_(
                                    ChatOutboxEvent.status == "pending",
                                    ChatOutboxEvent.available_at <= now,
                                ),
                                and_(
                                    ChatOutboxEvent.status == "publishing",
                                    ChatOutboxEvent.locked_at < stale,
                                ),
                            ),
                        )
                        .order_by(ChatOutboxEvent.available_at, ChatOutboxEvent.id)
                        .limit(100)
                    )
                ).scalars()
            )

    async def _reserve(self, event_id: str) -> dict | None:
        now = utc_now()
        stale = now - timedelta(seconds=settings.chat_outbox_lock_seconds)
        async with self.session_factory() as session:
            result = await session.execute(
                update(ChatOutboxEvent)
                .where(
                    ChatOutboxEvent.id == event_id,
                    ChatOutboxEvent.attempt_count < settings.chat_outbox_max_attempts,
                    or_(
                        and_(
                            ChatOutboxEvent.status == "pending",
                            ChatOutboxEvent.available_at <= now,
                        ),
                        and_(
                            ChatOutboxEvent.status == "publishing",
                            ChatOutboxEvent.locked_at < stale,
                        ),
                    ),
                )
                .values(status="publishing", locked_by=self.publisher_id, locked_at=now)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await session.rollback()
                return None
            await session.commit()
            event = await session.get(ChatOutboxEvent, event_id)
            await session.refresh(event)
            return dict(event.payload_json)

    async def _mark_published(self, event_id: str) -> bool:
        now = utc_now()
        async with self.session_factory() as session:
            result = await session.execute(
                update(ChatOutboxEvent)
                .where(
                    ChatOutboxEvent.id == event_id,
                    ChatOutboxEvent.status == "publishing",
                    ChatOutboxEvent.locked_by == self.publisher_id,
                )
                .values(
                    status="published",
                    published_at=now,
                    locked_by=None,
                    locked_at=None,
                    last_error=None,
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            return result.rowcount == 1

    async def _mark_failed(self, event_id: str, exc: Exception) -> None:
        async with self.session_factory() as session:
            event = await session.get(ChatOutboxEvent, event_id)
            await session.refresh(event)
            if event.status != "publishing" or event.locked_by != self.publisher_id:
                await session.rollback()
                return
            event.attempt_count += 1
            event.status = (
                "dead"
                if event.attempt_count >= settings.chat_outbox_max_attempts
                else "pending"
            )
            event.available_at = utc_now() + timedelta(
                seconds=min(300, 2**event.attempt_count)
            )
            event.locked_by = None
            event.locked_at = None
            event.last_error = type(exc).__name__
            await session.commit()

    async def publish_once(self) -> int:
        candidate_ids = await self._candidate_ids()
        if not candidate_ids:
            return 0
        connection = await aio_pika.connect_robust(settings.rabbitmq_url)
        try:
            channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            exchange, _queue = await declare_chat_topology(channel)
            published = 0
            for event_id in candidate_ids:
                payload = await self._reserve(event_id)
                if payload is None:
                    continue
                try:
                    confirm = await exchange.publish(
                        build_chat_message(payload),
                        routing_key=settings.chat_rabbitmq_routing_key,
                        mandatory=True,
                        timeout=settings.model_timeout_seconds,
                    )
                    if (
                        confirm is None
                        or confirm is False
                        or isinstance(confirm, (Basic.Nack, Basic.Reject))
                    ):
                        raise RuntimeError("RabbitMQ did not confirm chat run routing")
                except Exception as exc:
                    logger.warning(
                        "chat outbox publish failed",
                        extra={"outbox_id": event_id, "error_type": type(exc).__name__},
                    )
                    await self._mark_failed(event_id, exc)
                    continue
                published += int(await self._mark_published(event_id))
            return published
        finally:
            await connection.close()


async def run_chat_outbox() -> None:
    publisher = ChatOutboxPublisher()
    while True:
        try:
            await publisher.publish_once()
        except Exception:
            logger.exception("chat outbox batch failed")
        await asyncio.sleep(max(0.1, settings.dispatch_interval_seconds))

