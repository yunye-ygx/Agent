from aio_pika import DeliveryMode, ExchangeType, Message

from app.core.config import settings


async def declare_topology(channel):
    dead_exchange = await channel.declare_exchange(
        settings.rabbitmq_dlx, ExchangeType.TOPIC, durable=True
    )
    dead_queue = await channel.declare_queue(settings.rabbitmq_dead_queue, durable=True)
    await dead_queue.bind(dead_exchange, routing_key="#")

    exchange = await channel.declare_exchange(
        settings.rabbitmq_exchange, ExchangeType.TOPIC, durable=True
    )
    queue = await channel.declare_queue(
        settings.rabbitmq_queue,
        durable=True,
        arguments={
            "x-dead-letter-exchange": settings.rabbitmq_dlx,
        },
    )
    await queue.bind(exchange, routing_key=settings.rabbitmq_routing_key)
    return exchange, queue


def build_message(payload: dict[str, object]) -> Message:
    import json

    return Message(
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        content_type="application/json",
        delivery_mode=DeliveryMode.PERSISTENT,
    )
