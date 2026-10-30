"""RabbitMQ topology dedicated to durable chat run notifications."""

import json

from aio_pika import DeliveryMode, ExchangeType, Message

from app.core.config import settings


async def declare_chat_topology(channel):
    dead_exchange = await channel.declare_exchange(
        settings.chat_rabbitmq_dlx, ExchangeType.TOPIC, durable=True
    )
    dead_queue = await channel.declare_queue(
        settings.chat_rabbitmq_dead_queue,
        durable=True,
        arguments={"x-queue-type": "quorum"},
    )
    await dead_queue.bind(dead_exchange, routing_key="#")

    exchange = await channel.declare_exchange(
        settings.chat_rabbitmq_exchange, ExchangeType.TOPIC, durable=True
    )
    queue = await channel.declare_queue(
        settings.chat_rabbitmq_queue,
        durable=True,
        arguments={
            "x-queue-type": "quorum",
            "x-dead-letter-exchange": settings.chat_rabbitmq_dlx,
        },
    )
    await queue.bind(exchange, routing_key=settings.chat_rabbitmq_routing_key)
    return exchange, queue


def build_chat_message(payload: dict[str, object]) -> Message:
    return Message(
        body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        content_type="application/json",
        delivery_mode=DeliveryMode.PERSISTENT,
        message_id=f"chat-run:{payload['run_id']}:{payload['dispatch_generation']}",
    )

