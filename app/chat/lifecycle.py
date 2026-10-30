"""Run claiming, lease renewal, cancellation and fenced terminal transitions."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, case, or_, select, update

from app.business.utils import utc_now
from app.chat.contracts import (
    TERMINAL_RUN_STATUSES,
    ExecutionLost,
    RunStatus,
    TurnStatus,
)
from app.chat.models import Conversation, Run, RunEvent, Turn


async def _lock_turn_and_conversation(db, run: Run):
    turn = (
        await db.execute(
            select(Turn)
            .where(Turn.id == run.turn_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    conversation = (
        await db.execute(
            select(Conversation)
            .where(Conversation.id == turn.conversation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return turn, conversation


def _release(turn: Turn, conversation: Conversation, status: str, now) -> None:
    turn.status = status
    turn.active_run_id = None
    turn.latest_run_id = turn.latest_run_id
    turn.closed_at = now
    turn.updated_at = now
    if conversation.active_turn == turn.id:
        conversation.active_turn = None
        conversation.updated_at = now


async def claim_run(
    db,
    run_id: str,
    *,
    worker_id: str,
    dispatch_generation: int,
    lease_seconds: int,
):
    now = utc_now()
    claimable = or_(
        and_(
            Run.status.in_([RunStatus.QUEUED, RunStatus.RECOVERING]),
            or_(Run.lease_expires_at.is_(None), Run.lease_expires_at <= now),
        ),
        and_(
            Run.status.in_([RunStatus.RUNNING, RunStatus.CANCELLING]),
            or_(Run.lease_expires_at.is_(None), Run.lease_expires_at <= now),
        ),
    )
    result = await db.execute(
        update(Run)
        .where(
            Run.id == run_id,
            Run.recovery_count == dispatch_generation,
            claimable,
        )
        .values(
            status=case(
                (Run.status == RunStatus.QUEUED, RunStatus.RUNNING),
                (Run.status == RunStatus.CANCELLING, RunStatus.CANCELLING),
                else_=RunStatus.RECOVERING,
            ),
            lease_owner=worker_id,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            last_heartbeat_at=now,
            lease_generation=Run.lease_generation + 1,
            started_at=case((Run.started_at.is_(None), now), else_=Run.started_at),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        return None
    run = await db.get(Run, run_id)
    await db.refresh(run)
    if run.status != RunStatus.CANCELLING:
        db.add(
            RunEvent(
                run_id=run.id,
                seq=run.next_event_seq,
                event_type=(
                    "run.started" if run.status == RunStatus.RUNNING else "run.recovering"
                ),
                payload_json={"run_id": run.id, "status": run.status},
            )
        )
        run.next_event_seq += 1
    await db.commit()
    return run


async def assert_run_ownership(
    db,
    run_id: str,
    *,
    worker_id: str,
    lease_generation: int,
) -> Run:
    run = (
        await db.execute(
            select(Run)
            .where(
                Run.id == run_id,
                Run.lease_owner == worker_id,
                Run.lease_generation == lease_generation,
                Run.status.in_(
                    [RunStatus.RUNNING, RunStatus.RECOVERING, RunStatus.CANCELLING]
                ),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if run is None:
        raise ExecutionLost(f"worker no longer owns run {run_id}")
    return run


async def renew_run_lease(
    db,
    run_id: str,
    *,
    worker_id: str,
    lease_generation: int,
    lease_seconds: int,
) -> None:
    now = utc_now()
    result = await db.execute(
        update(Run)
        .where(
            Run.id == run_id,
            Run.lease_owner == worker_id,
            Run.lease_generation == lease_generation,
            Run.status.in_(
                [RunStatus.RUNNING, RunStatus.RECOVERING, RunStatus.CANCELLING]
            ),
        )
        .values(
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            last_heartbeat_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise ExecutionLost(f"worker no longer owns run {run_id}")
    await db.commit()


async def finish_run(
    db,
    run_id: str,
    *,
    worker_id: str,
    lease_generation: int,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    terminal = RunStatus(status)
    if terminal not in TERMINAL_RUN_STATUSES:
        raise ValueError(f"run status is not terminal: {status}")
    run = await assert_run_ownership(
        db,
        run_id,
        worker_id=worker_id,
        lease_generation=lease_generation,
    )
    turn, conversation = await _lock_turn_and_conversation(db, run)
    now = utc_now()
    run.status = terminal
    run.finished_at = now
    run.updated_at = now
    run.lease_owner = None
    run.lease_expires_at = None
    run.error_code = error_code
    run.error_message = error_message
    _release(turn, conversation, terminal, now)
    db.add(
        RunEvent(
            run_id=run.id,
            seq=run.next_event_seq,
            event_type=f"run.{terminal}",
            payload_json={
                "run_id": run.id,
                "turn_id": turn.id,
                "status": terminal,
                "finished_at": now.isoformat(),
            },
        )
    )
    run.next_event_seq += 1
    await db.commit()


async def mark_requires_attention(
    db,
    run_id: str,
    *,
    worker_id: str,
    lease_generation: int,
    error_code: str,
    error_message: str,
) -> None:
    run = await assert_run_ownership(
        db,
        run_id,
        worker_id=worker_id,
        lease_generation=lease_generation,
    )
    turn, conversation = await _lock_turn_and_conversation(db, run)
    now = utc_now()
    run.status = RunStatus.REQUIRES_ATTENTION
    run.finished_at = now
    run.updated_at = now
    run.lease_owner = None
    run.lease_expires_at = None
    run.error_code = error_code
    run.error_message = error_message
    _release(turn, conversation, TurnStatus.REQUIRES_ATTENTION, now)
    db.add(
        RunEvent(
            run_id=run.id,
            seq=run.next_event_seq,
            event_type="run.requires_attention",
            payload_json={
                "run_id": run.id,
                "turn_id": turn.id,
                "status": RunStatus.REQUIRES_ATTENTION,
                "error_code": error_code,
                "message": error_message,
            },
        )
    )
    run.next_event_seq += 1
    await db.commit()


async def request_cancel(db, run_id: str) -> Run:
    """CAS a run into cancelling, or cancel it directly while it is still queued."""

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
    if run.status in (
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.REQUIRES_ATTENTION,
    ):
        return run
    now = utc_now()
    if run.status == RunStatus.QUEUED:
        turn, conversation = await _lock_turn_and_conversation(db, run)
        run.status = RunStatus.CANCELLED
        run.finished_at = now
        run.updated_at = now
        run.error_code = "CHAT_CANCELLED_BY_USER"
        _release(turn, conversation, TurnStatus.CANCELLED, now)
        db.add(
            RunEvent(
                run_id=run.id,
                seq=run.next_event_seq,
                event_type="run.cancelled",
                payload_json={
                    "run_id": run.id,
                    "turn_id": turn.id,
                    "status": RunStatus.CANCELLED,
                    "finished_at": now.isoformat(),
                },
            )
        )
        run.next_event_seq += 1
        await db.commit()
        return run
    if run.status != RunStatus.CANCELLING:
        run.status = RunStatus.CANCELLING
        run.updated_at = now
        db.add(
            RunEvent(
                run_id=run.id,
                seq=run.next_event_seq,
                event_type="run.cancelling",
                payload_json={
                    "run_id": run.id,
                    "turn_id": run.turn_id,
                    "status": RunStatus.CANCELLING,
                },
            )
        )
        run.next_event_seq += 1
    await db.commit()
    return run


async def recover_terminal(
    db,
    run_id: str,
    *,
    status: str,
    error_code: str,
    error_message: str,
    expect_recovery_count: int,
) -> bool:
    """Settle a run whose lease expired and whose recovery budget is exhausted."""

    terminal = RunStatus(status)
    run = (
        await db.execute(
            select(Run)
            .where(Run.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if run is None or run.status not in (
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.RECOVERING,
        RunStatus.CANCELLING,
    ):
        await db.rollback()
        return False
    if run.recovery_count != expect_recovery_count:
        await db.rollback()
        return False
    turn, conversation = await _lock_turn_and_conversation(db, run)
    now = utc_now()
    run.status = terminal
    run.finished_at = now
    run.updated_at = now
    run.lease_owner = None
    run.lease_expires_at = None
    run.error_code = error_code
    run.error_message = error_message
    _release(turn, conversation, terminal, now)
    db.add(
        RunEvent(
            run_id=run.id,
            seq=run.next_event_seq,
            event_type=f"run.{terminal}",
            payload_json={
                "run_id": run.id,
                "turn_id": turn.id,
                "status": terminal,
                "error_code": error_code,
                "finished_at": now.isoformat(),
            },
        )
    )
    run.next_event_seq += 1
    await db.commit()
    return True