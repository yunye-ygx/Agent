from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.business.utils import utc_now


class SessionFactory:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def __call__(self):
        yield self.db


@pytest.mark.asyncio
async def test_expired_lease_is_fenced_and_completion_releases_conversation(business_db):
    from app.chat import lifecycle, store
    from app.chat.contracts import ExecutionLost
    from app.chat.models import Run, RunEvent, Turn

    conversation = await store.create_conversation(business_db, "alice")
    submission = await store.create_turn_and_initial_run(
        business_db,
        "alice",
        conversation.id,
        "查询订单",
        client_request_id="request-lease",
    )

    first = await lifecycle.claim_run(
        business_db,
        submission.run_id,
        worker_id="worker-a",
        dispatch_generation=0,
        lease_seconds=30,
    )
    assert first is not None
    assert first.status == "running" and first.lease_generation == 1
    assert (
        await lifecycle.claim_run(
            business_db,
            submission.run_id,
            worker_id="worker-b",
            dispatch_generation=0,
            lease_seconds=30,
        )
        is None
    )

    await business_db.execute(
        update(Run)
        .where(Run.id == submission.run_id)
        .values(lease_expires_at=utc_now() - timedelta(seconds=1))
    )
    await business_db.commit()
    replacement = await lifecycle.claim_run(
        business_db,
        submission.run_id,
        worker_id="worker-b",
        dispatch_generation=0,
        lease_seconds=30,
    )
    assert replacement is not None
    assert replacement.status == "recovering" and replacement.lease_generation == 2

    with pytest.raises(ExecutionLost):
        await lifecycle.assert_run_ownership(
            business_db, submission.run_id, worker_id="worker-a", lease_generation=1
        )
    await lifecycle.renew_run_lease(
        business_db,
        submission.run_id,
        worker_id="worker-b",
        lease_generation=2,
        lease_seconds=30,
    )
    await lifecycle.finish_run(
        business_db,
        submission.run_id,
        worker_id="worker-b",
        lease_generation=2,
        status="completed",
    )

    run = await business_db.get(Run, submission.run_id)
    turn = await business_db.get(Turn, submission.turn_id)
    refreshed = await store.get_conversation(business_db, "alice", conversation.id)
    assert run.status == turn.status == "completed"
    assert run.lease_owner is None and run.finished_at is not None
    assert turn.active_run_id is None and turn.latest_run_id == run.id
    assert refreshed.active_turn is None
    events = list(
        (
            await business_db.execute(
                select(RunEvent)
                .where(RunEvent.run_id == run.id)
                .order_by(RunEvent.seq)
            )
        ).scalars()
    )
    assert [event.event_type for event in events] == [
        "run.queued",
        "run.started",
        "run.recovering",
        "run.completed",
    ]


async def _submission(db, key):
    from app.chat import store

    conversation = await store.create_conversation(db, "alice")
    submission = await store.create_turn_and_initial_run(
        db,
        "alice",
        conversation.id,
        "查订单",
        client_request_id=key,
    )
    return conversation, submission


@pytest.mark.asyncio
async def test_cancel_queued_run_releases_turn_without_claim(business_db):
    from app.chat import lifecycle, store
    from app.chat.models import RunEvent, Turn

    conversation, submission = await _submission(business_db, "cancel-queued")
    run = await lifecycle.request_cancel(business_db, submission.run_id)
    assert run.status == "cancelled"
    assert run.error_code == "CHAT_CANCELLED_BY_USER"
    turn = await business_db.get(Turn, submission.turn_id)
    assert turn.status == "cancelled" and turn.active_run_id is None
    refreshed = await store.get_conversation(business_db, "alice", conversation.id)
    assert refreshed.active_turn is None
    events = list(
        (
            await business_db.execute(
                select(RunEvent)
                .where(RunEvent.run_id == submission.run_id)
                .order_by(RunEvent.seq)
            )
        ).scalars()
    )
    assert [event.event_type for event in events] == ["run.queued", "run.cancelled"]


@pytest.mark.asyncio
async def test_cancel_running_run_waits_for_worker_safe_boundary(business_db):
    from app.chat import lifecycle
    from app.chat.models import Run, Turn

    _, submission = await _submission(business_db, "cancel-running")
    claimed = await lifecycle.claim_run(
        business_db,
        submission.run_id,
        worker_id="worker-a",
        dispatch_generation=0,
        lease_seconds=60,
    )
    await lifecycle.request_cancel(business_db, submission.run_id)
    current = await business_db.get(Run, submission.run_id)
    await business_db.refresh(current)
    assert current.status == "cancelling"
    assert current.lease_owner == "worker-a"

    await lifecycle.finish_run(
        business_db,
        submission.run_id,
        worker_id="worker-a",
        lease_generation=claimed.lease_generation,
        status="cancelled",
        error_code="CHAT_CANCELLED_BY_USER",
    )
    current = await business_db.get(Run, submission.run_id)
    await business_db.refresh(current)
    turn = await business_db.get(Turn, submission.turn_id)
    assert current.status == "cancelled" and turn.status == "cancelled"


@pytest.mark.asyncio
async def test_cancelling_run_survives_recovery_claim(business_db):
    from app.chat import lifecycle
    from app.chat.models import Run

    _, submission = await _submission(business_db, "cancel-recovery")
    await lifecycle.claim_run(
        business_db,
        submission.run_id,
        worker_id="worker-a",
        dispatch_generation=0,
        lease_seconds=30,
    )
    await lifecycle.request_cancel(business_db, submission.run_id)
    await business_db.execute(
        update(Run)
        .where(Run.id == submission.run_id)
        .values(lease_expires_at=utc_now() - timedelta(seconds=1))
    )
    await business_db.commit()

    replacement = await lifecycle.claim_run(
        business_db,
        submission.run_id,
        worker_id="worker-b",
        dispatch_generation=0,
        lease_seconds=30,
    )
    assert replacement is not None
    assert replacement.status == "cancelling"
    assert replacement.lease_generation == 2


@pytest.mark.asyncio
async def test_requires_attention_releases_conversation(business_db):
    from app.chat import lifecycle, store
    from app.chat.models import Run, Turn

    conversation, submission = await _submission(business_db, "attention")
    claimed = await lifecycle.claim_run(
        business_db,
        submission.run_id,
        worker_id="worker-a",
        dispatch_generation=0,
        lease_seconds=60,
    )
    await lifecycle.mark_requires_attention(
        business_db,
        submission.run_id,
        worker_id="worker-a",
        lease_generation=claimed.lease_generation,
        error_code="CHAT_TOOL_REQUIRES_ATTENTION",
        error_message="工具结果未知",
    )
    run = await business_db.get(Run, submission.run_id)
    await business_db.refresh(run)
    turn = await business_db.get(Turn, submission.turn_id)
    assert run.status == "requires_attention"
    assert turn.status == "requires_attention" and turn.active_run_id is None
    refreshed = await store.get_conversation(business_db, "alice", conversation.id)
    assert refreshed.active_turn is None


@pytest.mark.asyncio
async def test_recovery_scanner_reschedules_expired_lease(business_db):
    from app.chat import lifecycle
    from app.chat.models import ChatOutboxEvent, Run
    from app.chat.recovery import scan_once

    _, submission = await _submission(business_db, "recovery-scan")
    await lifecycle.claim_run(
        business_db,
        submission.run_id,
        worker_id="worker-a",
        dispatch_generation=0,
        lease_seconds=30,
    )
    await business_db.execute(
        update(Run)
        .where(Run.id == submission.run_id)
        .values(lease_expires_at=utc_now() - timedelta(seconds=1))
    )
    await business_db.commit()

    assert await scan_once(SessionFactory(business_db)) == 1
    run = await business_db.get(Run, submission.run_id)
    await business_db.refresh(run)
    assert run.status == "recovering" and run.recovery_count == 1
    outbox = list(
        (
            await business_db.execute(
                select(ChatOutboxEvent).where(
                    ChatOutboxEvent.aggregate_id == submission.run_id
                )
            )
        ).scalars()
    )
    assert len(outbox) == 2
    recovery = next(item for item in outbox if item.event_type == "run.recovery_requested")
    assert recovery.payload_json["dispatch_generation"] == 1
    assert recovery.deduplication_key == f"run:{submission.run_id}:recovery:1"

    replacement = await lifecycle.claim_run(
        business_db,
        submission.run_id,
        worker_id="worker-b",
        dispatch_generation=1,
        lease_seconds=30,
    )
    assert replacement is not None
    assert replacement.lease_generation == 2


@pytest.mark.asyncio
async def test_recovery_exhaustion_settles_failed(business_db):
    from app.chat import store
    from app.chat.models import Run, Turn
    from app.chat.recovery import scan_once

    conversation, submission = await _submission(business_db, "recovery-exhausted")
    await business_db.execute(
        update(Run)
        .where(Run.id == submission.run_id)
        .values(
            status="running",
            recovery_count=3,
            max_recovery_count=3,
            lease_expires_at=utc_now() - timedelta(seconds=1),
        )
    )
    await business_db.commit()

    assert await scan_once(SessionFactory(business_db)) == 0
    run = await business_db.get(Run, submission.run_id)
    await business_db.refresh(run)
    turn = await business_db.get(Turn, submission.turn_id)
    assert run.status == "failed"
    assert run.error_code == "CHAT_RECOVERY_EXHAUSTED"
    assert turn.status == "failed"
    refreshed = await store.get_conversation(business_db, "alice", conversation.id)
    assert refreshed.active_turn is None


@pytest.mark.asyncio
async def test_only_latest_turn_can_be_re_executed(business_db):
    from app.business.errors import Conflict
    from app.chat import lifecycle, store

    conversation, first = await _submission(business_db, "retry-latest-1")
    claimed = await lifecycle.claim_run(
        business_db,
        first.run_id,
        worker_id="worker-a",
        dispatch_generation=0,
        lease_seconds=60,
    )
    await lifecycle.finish_run(
        business_db,
        first.run_id,
        worker_id="worker-a",
        lease_generation=claimed.lease_generation,
        status="completed",
    )
    second = await store.create_turn_and_initial_run(
        business_db,
        "alice",
        conversation.id,
        "第二个问题",
        client_request_id="retry-latest-2",
    )
    with pytest.raises(Conflict) as error:
        await store.create_retry_run(
            business_db, "alice", first.turn_id, trigger_reason="regenerate"
        )
    assert error.value.code == "CHAT_TURN_NOT_LATEST"
    assert second.turn_id != first.turn_id


@pytest.mark.asyncio
async def test_retry_requires_no_active_run_and_respects_attempt_limit(business_db):
    from app.business.errors import Conflict
    from app.chat import lifecycle, store
    from app.chat.models import Run

    _, submission = await _submission(business_db, "retry-limit")
    claimed = await lifecycle.claim_run(
        business_db,
        submission.run_id,
        worker_id="worker-a",
        dispatch_generation=0,
        lease_seconds=60,
    )
    await lifecycle.finish_run(
        business_db,
        submission.run_id,
        worker_id="worker-a",
        lease_generation=claimed.lease_generation,
        status="failed",
    )

    second = await store.create_retry_run(
        business_db, "alice", submission.turn_id, trigger_reason="failure_retry"
    )
    assert second.attempt_no == 2
    with pytest.raises(Conflict) as active_error:
        await store.create_retry_run(
            business_db, "alice", submission.turn_id, trigger_reason="failure_retry"
        )
    assert active_error.value.code == "CHAT_RUN_ACTIVE"

    await lifecycle.request_cancel(business_db, second.id)
    third = await store.create_retry_run(
        business_db, "alice", submission.turn_id, trigger_reason="failure_retry"
    )
    assert third.attempt_no == 3
    await lifecycle.request_cancel(business_db, third.id)
    with pytest.raises(Conflict) as limit_error:
        await store.create_retry_run(
            business_db, "alice", submission.turn_id, trigger_reason="failure_retry"
        )
    assert limit_error.value.code == "CHAT_RUN_LIMIT"
    assert (await business_db.get(Run, third.id)).status == "cancelled"

