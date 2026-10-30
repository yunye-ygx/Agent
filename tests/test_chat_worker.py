import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest


class SessionFactory:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def __call__(self):
        yield self.db


@pytest.mark.asyncio
async def test_malformed_notification_is_rejected_without_claiming():
    from app.chat.worker import handle_chat_message

    message = AsyncMock()
    message.body = b'{"run_id":"run-1"}'
    processor = AsyncMock()
    await handle_chat_message(message, processor)
    processor.claim.assert_not_awaited()
    message.reject.assert_awaited_once_with(requeue=False)


@pytest.mark.asyncio
async def test_worker_acks_after_claim_and_before_execution():
    from app.chat.worker import handle_chat_message

    order = []
    message = AsyncMock()
    message.body = (
        b'{"run_id":"run-1","dispatch_reason":"initial",'
        b'"dispatch_generation":0,"schema_version":1}'
    )
    processor = AsyncMock()
    processor.claim.return_value = object()
    message.ack.side_effect = lambda: order.append("ack")
    processor.execute.side_effect = lambda lease: order.append("execute")

    await handle_chat_message(message, processor)

    assert order == ["ack", "execute"]
    message.nack.assert_not_awaited()


@pytest.mark.asyncio
async def test_db_claim_failure_is_requeued_but_noop_is_acked():
    from app.chat.worker import handle_chat_message

    payload = (
        b'{"run_id":"run-1","dispatch_reason":"recovery",'
        b'"dispatch_generation":2,"schema_version":1}'
    )
    message = AsyncMock()
    message.body = payload
    processor = AsyncMock()
    processor.claim.side_effect = RuntimeError("database unavailable")
    await handle_chat_message(message, processor)
    message.nack.assert_awaited_once_with(requeue=True)
    message.ack.assert_not_awaited()

    message.reset_mock()
    processor.reset_mock()
    processor.claim.side_effect = None
    processor.claim.return_value = None
    await handle_chat_message(message, processor)
    message.ack.assert_awaited_once()
    processor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_ten_duplicate_deliveries_claim_and_execute_once(business_db):
    from app.chat import store
    from app.chat.worker import ChatRunProcessor, handle_chat_message

    conversation = await store.create_conversation(business_db, "alice")
    submission = await store.create_turn_and_initial_run(
        business_db,
        "alice",
        conversation.id,
        "查订单",
        client_request_id="duplicate-delivery",
    )
    processor = ChatRunProcessor(
        session_factory=SessionFactory(business_db), worker_id="worker-a"
    )
    processor.execute = AsyncMock()
    payload = json.dumps(
        {
            "run_id": submission.run_id,
            "dispatch_reason": "initial",
            "dispatch_generation": 0,
            "schema_version": 1,
        }
    ).encode()

    messages = []
    for _ in range(10):
        message = AsyncMock()
        message.body = payload
        messages.append(message)
        await handle_chat_message(message, processor)

    assert processor.execute.await_count == 1
    assert all(message.ack.await_count == 1 for message in messages)
    assert all(message.nack.await_count == 0 for message in messages)

    from app.chat.models import Run

    run = await business_db.get(Run, submission.run_id)
    assert run.status == "running"
    assert run.lease_generation == 1

