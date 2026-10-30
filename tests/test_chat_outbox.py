import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select


class SessionFactory:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def __call__(self):
        yield self.db


@pytest.mark.asyncio
async def test_publish_failure_keeps_outbox_retryable(business_db):
    from app.chat import store
    from app.chat.models import ChatOutboxEvent
    from app.chat.outbox_publisher import ChatOutboxPublisher

    conversation = await store.create_conversation(business_db, "alice")
    submission = await store.create_turn_and_initial_run(
        business_db,
        "alice",
        conversation.id,
        "查询订单",
        client_request_id="outbox-request",
    )
    exchange = AsyncMock()
    exchange.publish.side_effect = RuntimeError("broker unavailable secret detail")
    connection = AsyncMock()
    connection.channel.return_value = AsyncMock()
    with (
        patch(
            "app.chat.outbox_publisher.aio_pika.connect_robust",
            AsyncMock(return_value=connection),
        ),
        patch(
            "app.chat.outbox_publisher.declare_chat_topology",
            AsyncMock(return_value=(exchange, None)),
        ),
    ):
        publisher = ChatOutboxPublisher(
            session_factory=SessionFactory(business_db), publisher_id="publisher-a"
        )
        assert await publisher.publish_once() == 0

    event = (
        await business_db.execute(
            select(ChatOutboxEvent).where(ChatOutboxEvent.aggregate_id == submission.run_id)
        )
    ).scalar_one()
    assert event.status == "pending" and event.attempt_count == 1
    assert event.locked_by is None and event.available_at > event.created_at
    assert event.last_error == "RuntimeError"
    exchange.publish.assert_awaited_once()
    assert exchange.publish.call_args.kwargs["mandatory"] is True


@pytest.mark.asyncio
async def test_confirmed_publish_marks_outbox_published(business_db):
    from app.chat import store
    from app.chat.models import ChatOutboxEvent
    from app.chat.outbox_publisher import ChatOutboxPublisher

    conversation = await store.create_conversation(business_db, "alice")
    submission = await store.create_turn_and_initial_run(
        business_db,
        "alice",
        conversation.id,
        "查询设备",
        client_request_id="outbox-success",
    )
    exchange = AsyncMock()
    exchange.publish.return_value = True
    connection = AsyncMock()
    connection.channel.return_value = AsyncMock()
    with (
        patch(
            "app.chat.outbox_publisher.aio_pika.connect_robust",
            AsyncMock(return_value=connection),
        ),
        patch(
            "app.chat.outbox_publisher.declare_chat_topology",
            AsyncMock(return_value=(exchange, None)),
        ),
    ):
        publisher = ChatOutboxPublisher(
            session_factory=SessionFactory(business_db), publisher_id="publisher-b"
        )
        assert await publisher.publish_once() == 1

    event = (
        await business_db.execute(
            select(ChatOutboxEvent).where(ChatOutboxEvent.aggregate_id == submission.run_id)
        )
    ).scalar_one()
    assert event.status == "published" and event.published_at is not None


@pytest.mark.skipif(
    not os.getenv("CHAT_E2E"),
    reason="set CHAT_E2E=1 with MySQL/RabbitMQ running for the real broker recovery test",
)
@pytest.mark.asyncio
async def test_unreachable_broker_then_real_publish_after_recovery(business_db):
    from app.chat import store
    from app.chat.models import ChatOutboxEvent
    from app.chat.outbox_publisher import ChatOutboxPublisher

    conversation = await store.create_conversation(business_db, "alice")
    submission = await store.create_turn_and_initial_run(
        business_db,
        "alice",
        conversation.id,
        "查询订单",
        client_request_id="outbox-outage",
    )
    publisher = ChatOutboxPublisher(
        session_factory=SessionFactory(business_db), publisher_id="publisher-outage"
    )
    with (
        patch(
            "app.chat.outbox_publisher.aio_pika.connect_robust",
            AsyncMock(side_effect=ConnectionError("broker down")),
        ),
        pytest.raises(ConnectionError),
    ):
        await publisher.publish_once()

    event = (
        await business_db.execute(
            select(ChatOutboxEvent).where(
                ChatOutboxEvent.aggregate_id == submission.run_id
            )
        )
    ).scalar_one()
    assert event.status == "pending"
    assert event.published_at is None

    assert await publisher.publish_once() >= 1
    await business_db.refresh(event)
    assert event.status == "published"

    import aio_pika

    from app.core.config import settings
    from app.messaging.chat_topology import declare_chat_topology

    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    try:
        channel = await connection.channel()
        _exchange, queue = await declare_chat_topology(channel)
        while True:
            message = await queue.get(no_ack=False, fail=False)
            if message is None:
                break
            await message.ack()
    finally:
        await connection.close()

