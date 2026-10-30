"""Append-only run event log and the SSE projection.

`seq` is allocated while the run row is locked, so concurrent tool calls cannot
duplicate or reorder observable events. Observation never mutates run state.
"""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import delete, select

from app.business.utils import utc_now
from app.chat.contracts import ExecutionLost, RunStatus
from app.chat.models import Run, RunEvent
from app.core.config import settings
from app.db.mysql_session import AsyncSessionFactory

ACTIVE_RUN_STATUSES = (
    RunStatus.RUNNING,
    RunStatus.RECOVERING,
    RunStatus.CANCELLING,
)


async def lock_run(db, run_id: str, *, lease=None) -> Run:
    run = (
        await db.execute(
            select(Run)
            .where(Run.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if run is None:
        raise ExecutionLost(f"run no longer exists: {run_id}")
    if lease is not None:
        if (
            run.lease_owner != lease.worker_id
            or run.lease_generation != lease.lease_generation
        ):
            raise ExecutionLost(f"worker no longer owns run {run_id}")
        if run.status not in ACTIVE_RUN_STATUSES:
            raise ExecutionLost(f"run is no longer executable: {run.status}")
    return run


async def append_event(
    db,
    *,
    run: Run,
    event_type: str,
    payload: dict,
    model_response_id: str | None = None,
    response_segment_id: str | None = None,
    lease=None,
    now=None,
) -> RunEvent:
    """Append one event to an already locked run inside the caller's transaction."""

    if lease is not None:
        if (
            run.lease_owner != lease.worker_id
            or run.lease_generation != lease.lease_generation
        ):
            raise ExecutionLost(f"worker no longer owns run {run.id}")
        if run.status not in ACTIVE_RUN_STATUSES:
            raise ExecutionLost(f"run is no longer executable: {run.status}")
    moment = now or utc_now()
    event = RunEvent(
        run_id=run.id,
        seq=run.next_event_seq,
        event_type=event_type,
        payload_json=payload,
        model_response_id=model_response_id,
        response_segment_id=response_segment_id,
        created_at=moment,
        expires_at=moment
        + timedelta(seconds=settings.chat_event_retention_seconds),
    )
    run.next_event_seq += 1
    db.add(event)
    return event


async def emit_event(
    session_factory,
    *,
    run_id: str,
    event_type: str,
    payload: dict,
    lease=None,
    model_response_id: str | None = None,
    response_segment_id: str | None = None,
) -> RunEvent:
    """Convenience writer for a standalone event in its own short transaction."""

    async with session_factory() as db:
        run = await lock_run(db, run_id, lease=lease)
        event = await append_event(
            db,
            run=run,
            event_type=event_type,
            payload=payload,
            lease=lease,
            model_response_id=model_response_id,
            response_segment_id=response_segment_id,
        )
        await db.commit()
        return event


async def read_events(db, run_id: str, *, after: int = 0, limit: int = 200):
    return list(
        (
            await db.execute(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.seq > after)
                .order_by(RunEvent.seq)
                .limit(limit)
            )
        ).scalars()
    )


async def last_event_seq(db, run_id: str) -> int:
    value = (
        await db.execute(
            select(RunEvent.seq)
            .where(RunEvent.run_id == run_id)
            .order_by(RunEvent.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return int(value or 0)


def sse_frame(event: RunEvent) -> str:
    data = json.dumps(
        event.payload_json, ensure_ascii=False, separators=(",", ":")
    )
    return f"id: {event.seq}\nevent: {event.event_type}\ndata: {data}\n\n"


SSE_HEARTBEAT = ": ping\n\n"
SSE_RETRY = "retry: 2000\n\n"


async def purge_expired_events(session_factory=None) -> int:
    """Drop observation events past their retention window; messages/Runs remain."""

    async with (session_factory or AsyncSessionFactory)() as db:
        result = await db.execute(
            delete(RunEvent).where(
                RunEvent.expires_at.is_not(None),
                RunEvent.expires_at <= utc_now(),
            )
        )
        await db.commit()
        return int(result.rowcount or 0)