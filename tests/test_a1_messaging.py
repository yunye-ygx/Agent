from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from test_a1_lifecycle import seed
from test_a1_pipeline import SessionFactory

from app import worker
from app.db.mysql_models import TaskDispatchOutbox
from app.messaging.outbox_publisher import OutboxPublisher


@pytest.mark.asyncio
async def test_unroutable_publish_is_not_recorded_as_pending_execution(db):
    _doc, _run, task = await seed(db)
    exchange = AsyncMock()
    exchange.publish.side_effect = RuntimeError("unroutable")
    connection = AsyncMock()
    with (
        patch(
            "app.messaging.outbox_publisher.aio_pika.connect_robust",
            AsyncMock(return_value=connection),
        ),
        patch(
            "app.messaging.outbox_publisher.declare_topology",
            AsyncMock(return_value=(exchange, None)),
        ),
    ):
        publisher = OutboxPublisher(session_factory=SessionFactory(db))
        assert await publisher.publish_once() == 0
    assert task.status == "WAITING_PUBLISH" and task.attempt_count == 0
    record = (await db.execute(select(TaskDispatchOutbox))).scalar_one()
    assert record.status == "PENDING"
    assert record.attempt_count == 1
    connection.channel.assert_awaited_once_with(publisher_confirms=True, on_return_raises=True)
    assert exchange.publish.call_args.kwargs["mandatory"] is True


@pytest.mark.asyncio
async def test_worker_ack_after_persisted_success_and_nack_on_db_failure():
    from types import SimpleNamespace

    message = AsyncMock()
    message.body = b'{"task_id":"t","doc_id":"d","run_id":"r","dispatch_seq":1}'
    p = AsyncMock()
    p.process.return_value = SimpleNamespace(acknowledged=True)
    await worker.handle_message(message, p)
    p.process.assert_awaited_once_with("t", doc_id="d", run_id="r", dispatch_seq=1)
    message.ack.assert_awaited_once()
    message.nack.assert_not_awaited()
    message.reset_mock()
    p.process.side_effect = RuntimeError("database down")
    with patch("app.worker.asyncio.sleep", AsyncMock()):
        await worker.handle_message(message, p)
    message.ack.assert_not_awaited()
    message.nack.assert_awaited_once_with(requeue=True)


@pytest.mark.asyncio
async def test_old_format_message_is_rejected_without_executing():
    message, p = AsyncMock(), AsyncMock()
    message.body = b'{"task_id":"t","attempt":1}'
    await worker.handle_message(message, p)
    p.process.assert_not_awaited()
    message.reject.assert_awaited_once_with(requeue=False)
