from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from app.chat.contracts import RunLease


class SessionFactory:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def __call__(self):
        yield self.db


async def _claimed(business_db, key="events"):
    from app.chat import lifecycle, store

    conversation = await store.create_conversation(business_db, "alice")
    submission = await store.create_turn_and_initial_run(
        business_db,
        "alice",
        conversation.id,
        "查订单",
        client_request_id=key,
    )
    run = await lifecycle.claim_run(
        business_db,
        submission.run_id,
        worker_id="worker-a",
        dispatch_generation=0,
        lease_seconds=60,
    )
    lease = RunLease(
        run_id=run.id,
        turn_id=run.turn_id,
        worker_id="worker-a",
        lease_generation=run.lease_generation,
    )
    return submission, lease


@pytest.mark.asyncio
async def test_event_seq_is_monotonic_and_replay_cursor_is_exclusive(business_db):
    from app.chat import events

    _, lease = await _claimed(business_db)
    await events.emit_event(
        SessionFactory(business_db),
        run_id=lease.run_id,
        event_type="response.started",
        payload={"response_segment_id": "s1"},
        lease=lease,
        response_segment_id="s1",
    )
    await events.emit_event(
        SessionFactory(business_db),
        run_id=lease.run_id,
        event_type="text.delta",
        payload={"delta": "正在查询"},
        lease=lease,
        response_segment_id="s1",
    )
    await events.emit_event(
        SessionFactory(business_db),
        run_id=lease.run_id,
        event_type="run.completed",
        payload={"status": "completed"},
        lease=lease,
    )

    async with SessionFactory(business_db)() as db:
        rows = await events.read_events(db, lease.run_id, after=0)
        assert [row.seq for row in rows] == [1, 2, 3, 4, 5]
        assert [row.event_type for row in rows] == [
            "run.queued",
            "run.started",
            "response.started",
            "text.delta",
            "run.completed",
        ]
        page = await events.read_events(db, lease.run_id, after=2, limit=2)
        assert [row.seq for row in page] == [3, 4]
        assert await events.last_event_seq(db, lease.run_id) == 5
        assert events.sse_frame(page[0]).startswith(
            "id: 3\nevent: response.started\ndata: "
        )
        assert events.sse_frame(page[0]).endswith("\n\n")


@pytest.mark.asyncio
async def test_event_writer_rejects_a_lost_lease(business_db):
    from app.chat import events
    from app.chat.contracts import ExecutionLost

    _, lease = await _claimed(business_db, key="lost")
    stale = RunLease(
        run_id=lease.run_id,
        turn_id=lease.turn_id,
        worker_id="worker-b",
        lease_generation=lease.lease_generation + 1,
    )
    with pytest.raises(ExecutionLost):
        await events.emit_event(
            SessionFactory(business_db),
            run_id=stale.run_id,
            event_type="text.delta",
            payload={"delta": "bad"},
            lease=stale,
        )
    async with SessionFactory(business_db)() as db:
        rows = await events.read_events(db, lease.run_id)
        assert [row.event_type for row in rows] == ["run.queued", "run.started"]


@pytest.mark.asyncio
async def test_run_completion_event_carries_terminal_status_once(business_db):
    from app.chat import lifecycle
    from app.chat.models import RunEvent

    submission, lease = await _claimed(business_db, key="terminal")
    await lifecycle.finish_run(
        business_db,
        lease.run_id,
        worker_id=lease.worker_id,
        lease_generation=lease.lease_generation,
        status="failed",
        error_code="CHAT_EXECUTION_FAILED",
        error_message="boom",
    )
    events = list(
        (
            await business_db.execute(
                select(RunEvent)
                .where(RunEvent.run_id == lease.run_id)
                .order_by(RunEvent.seq)
            )
        ).scalars()
    )
    terminal = [event for event in events if event.event_type == "run.failed"]
    assert len(terminal) == 1
    assert terminal[0].payload_json["status"] == "failed"
    assert terminal[0].payload_json["turn_id"] == submission.turn_id


@pytest.mark.asyncio
async def test_retention_purge_removes_only_expired_events(business_db):
    from datetime import timedelta

    from sqlalchemy import update

    from app.business.utils import utc_now
    from app.chat import events
    from app.chat.models import RunEvent

    _, lease = await _claimed(business_db, key="retention")
    fresh = await events.emit_event(
        SessionFactory(business_db),
        run_id=lease.run_id,
        event_type="text.delta",
        payload={"delta": "保留"},
        lease=lease,
    )
    expired = await events.emit_event(
        SessionFactory(business_db),
        run_id=lease.run_id,
        event_type="text.delta",
        payload={"delta": "过期"},
        lease=lease,
    )
    await business_db.execute(
        update(RunEvent)
        .where(RunEvent.id == expired.id)
        .values(expires_at=utc_now() - timedelta(seconds=1))
    )
    await business_db.commit()

    assert await events.purge_expired_events(SessionFactory(business_db)) == 1
    remaining = list(
        (
            await business_db.execute(
                select(RunEvent)
                .where(RunEvent.run_id == lease.run_id)
                .order_by(RunEvent.seq)
            )
        ).scalars()
    )
    assert expired.id not in {row.id for row in remaining}
    assert fresh.id in {row.id for row in remaining}