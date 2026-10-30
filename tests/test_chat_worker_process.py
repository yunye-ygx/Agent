"""Process-level acceptance: a standalone Worker completes a Run with no API server."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not os.getenv("CHAT_E2E"),
    reason="set CHAT_E2E=1 with MySQL/RabbitMQ running for the worker process test",
)


@pytest_asyncio.fixture(autouse=True)
async def dispose_mysql_pool():
    """The shared engine must not carry a pooled connection across event loops."""

    yield
    from app.db.mysql_session import engine

    await engine.dispose()


@pytest.mark.asyncio
async def test_standalone_worker_completes_run_without_api() -> None:
    import aio_pika

    from app.business.models import BusinessUser
    from app.chat import store
    from app.chat.models import (
        ChatOutboxEvent,
        Conversation,
        Message,
        Run,
        RunEvent,
        ToolExecution,
        Turn,
    )
    from app.chat.outbox_publisher import ChatOutboxPublisher
    from app.core.config import settings
    from app.db.mysql_session import AsyncSessionFactory
    from app.messaging.chat_topology import declare_chat_topology

    async with AsyncSessionFactory() as db:
        user = await db.get(BusinessUser, "user-alice-001")
        if user is None or user.status != "active":
            pytest.skip("user-alice-001 is required for the process-level chat test")
        conversation = await store.create_conversation(db, user.id)
        submission = await store.create_turn_and_initial_run(
            db,
            user.id,
            conversation.id,
            "只由后台 Worker 执行",
            client_request_id=f"process-{uuid.uuid4().hex}",
        )

    published = await ChatOutboxPublisher(session_factory=AsyncSessionFactory).publish_once()
    assert published >= 1

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "tests")])
    process = subprocess.Popen(  # noqa: ASYNC220
        [sys.executable, str(ROOT / "tests" / "chat_worker_fake.py")],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    status = None
    try:
        for _ in range(150):
            async with AsyncSessionFactory() as db:
                run = await db.get(Run, submission.run_id)
                status = run.status if run is not None else None
            if status in ("completed", "failed"):
                break
            if process.poll() is not None:
                break
            await asyncio.sleep(0.2)
        if status != "completed":
            if process.poll() is None:
                process.terminate()
            _stdout, stderr = process.communicate(timeout=15)
            pytest.fail(f"worker did not complete the run: status={status}\n{stderr}")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

    async with AsyncSessionFactory() as db:
        run = await db.get(Run, submission.run_id)
        conversation_row = await db.get(Conversation, conversation.id)
        messages = list(
            (
                await db.execute(
                    select(Message)
                    .where(Message.turn_id == submission.turn_id)
                    .order_by(Message.sequence)
                )
            ).scalars()
        )
        assert run.status == "completed"
        assert conversation_row.active_turn is None
        assert [row.role for row in messages] == ["user", "assistant"]
        assert messages[-1].content == "worker-only answer"

    # Drain any remaining notification, then remove only this test's rows.
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

    async with AsyncSessionFactory() as db:
        run_ids = select(Run.id).where(Run.turn_id == submission.turn_id)
        await db.execute(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
        await db.execute(delete(ToolExecution).where(ToolExecution.run_id.in_(run_ids)))
        await db.execute(
            delete(ChatOutboxEvent).where(ChatOutboxEvent.aggregate_id.in_(run_ids))
        )
        await db.execute(
            update(Turn)
            .where(Turn.conversation_id == conversation.id)
            .values(latest_run_id=None, active_run_id=None)
        )
        await db.execute(
            update(Message)
            .where(Message.conversation_id == conversation.id)
            .values(run_id=None)
        )
        await db.execute(delete(Run).where(Run.turn_id == submission.turn_id))
        await db.execute(delete(Turn).where(Turn.conversation_id == conversation.id))
        await db.execute(
            delete(Message).where(Message.conversation_id == conversation.id)
        )
        await db.execute(delete(Conversation).where(Conversation.id == conversation.id))
        await db.commit()


@pytest.mark.asyncio
async def test_recovery_scanner_restarts_an_expired_run_without_api() -> None:
    from datetime import timedelta

    import aio_pika
    from sqlalchemy import update

    from app.business.models import BusinessUser
    from app.business.utils import utc_now
    from app.chat import lifecycle, store
    from app.chat.models import (
        ChatOutboxEvent,
        Conversation,
        Message,
        Run,
        RunEvent,
        ToolExecution,
        Turn,
    )
    from app.core.config import settings
    from app.db.mysql_session import AsyncSessionFactory
    from app.messaging.chat_topology import declare_chat_topology

    async with AsyncSessionFactory() as db:
        user = await db.get(BusinessUser, "user-alice-001")
        if user is None or user.status != "active":
            pytest.skip("user-alice-001 is required for the process-level chat test")
        conversation = await store.create_conversation(db, user.id)
        submission = await store.create_turn_and_initial_run(
            db,
            user.id,
            conversation.id,
            "由 Scanner 恢复",
            client_request_id=f"recovery-{uuid.uuid4().hex}",
        )
        claimed = await lifecycle.claim_run(
            db,
            submission.run_id,
            worker_id="crashed-worker",
            dispatch_generation=0,
            lease_seconds=30,
        )
        assert claimed is not None
        await db.execute(
            update(Run)
            .where(Run.id == submission.run_id)
            .values(lease_expires_at=utc_now() - timedelta(seconds=1))
        )
        await db.commit()

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "tests")])
    env["CHAT_RECOVERY_INTERVAL_SECONDS"] = "0.5"
    env["DISPATCH_INTERVAL_SECONDS"] = "0.2"
    processes = [
        subprocess.Popen(  # noqa: ASYNC220
            [sys.executable, "-m", "app.cli.chat_outbox"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ),
        subprocess.Popen(  # noqa: ASYNC220
            [sys.executable, "-m", "app.cli.chat_recovery"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ),
        subprocess.Popen(  # noqa: ASYNC220
            [sys.executable, str(ROOT / "tests" / "chat_worker_fake.py")],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ),
    ]
    status = None
    try:
        for _ in range(150):
            async with AsyncSessionFactory() as db:
                run = await db.get(Run, submission.run_id)
                status = run.status if run is not None else None
            if status in ("completed", "failed"):
                break
            if any(process.poll() is not None for process in processes):
                break
            await asyncio.sleep(0.2)
        if status != "completed":
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    _stdout, stderr = process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    _stdout, stderr = process.communicate(timeout=10)
                if stderr:
                    print(stderr)
            pytest.fail(f"scanner did not recover the run: status={status}")
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

    async with AsyncSessionFactory() as db:
        run = await db.get(Run, submission.run_id)
        conversation_row = await db.get(Conversation, conversation.id)
        assert run.status == "completed"
        assert run.recovery_count == 1
        assert conversation_row.active_turn is None

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

    async with AsyncSessionFactory() as db:
        run_ids = select(Run.id).where(Run.turn_id == submission.turn_id)
        await db.execute(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
        await db.execute(delete(ToolExecution).where(ToolExecution.run_id.in_(run_ids)))
        await db.execute(
            delete(ChatOutboxEvent).where(ChatOutboxEvent.aggregate_id.in_(run_ids))
        )
        await db.execute(
            update(Turn)
            .where(Turn.conversation_id == conversation.id)
            .values(latest_run_id=None, active_run_id=None)
        )
        await db.execute(
            update(Message)
            .where(Message.conversation_id == conversation.id)
            .values(run_id=None)
        )
        await db.execute(delete(Run).where(Run.turn_id == submission.turn_id))
        await db.execute(delete(Turn).where(Turn.conversation_id == conversation.id))
        await db.execute(
            delete(Message).where(Message.conversation_id == conversation.id)
        )
        await db.execute(delete(Conversation).where(Conversation.id == conversation.id))
        await db.commit()