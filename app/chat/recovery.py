"""Recovery scanner: redeliver stuck runs and settle unrecoverable ones."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import case, select, update

from app.business.utils import as_utc, utc_now
from app.chat import lifecycle
from app.chat.contracts import RunStatus
from app.chat.events import purge_expired_events
from app.chat.models import ChatOutboxEvent, Run, ToolExecution
from app.core.config import settings
from app.db.mysql_session import AsyncSessionFactory

logger = logging.getLogger(__name__)

ACTIVE = (
    RunStatus.QUEUED,
    RunStatus.RUNNING,
    RunStatus.RECOVERING,
    RunStatus.CANCELLING,
)


def _expired(run: Run, now) -> bool:
    if run.status == RunStatus.QUEUED:
        return as_utc(run.updated_at) <= now - timedelta(
            seconds=settings.chat_queued_timeout_seconds
        )
    lease = as_utc(run.lease_expires_at)
    if lease is not None:
        return lease <= now
    return as_utc(run.updated_at) <= now - timedelta(seconds=settings.chat_lease_seconds)


async def _settle_exhausted(db, run: Run, now) -> None:
    unknown = (
        await db.execute(
            select(ToolExecution.id).where(
                ToolExecution.run_id == run.id,
                ToolExecution.status.in_(["unknown", "requires_attention"]),
            )
        )
    ).first()
    if run.status == RunStatus.CANCELLING:
        status = RunStatus.CANCELLED
        code = "CHAT_CANCELLED_BY_USER"
        message = "用户取消；恢复次数已用尽"
    elif unknown is not None:
        status = RunStatus.REQUIRES_ATTENTION
        code = "CHAT_TOOL_REQUIRES_ATTENTION"
        message = "工具结果未知且恢复次数已用尽，需要人工核对"
    else:
        status = RunStatus.FAILED
        code = "CHAT_RECOVERY_EXHAUSTED"
        message = "执行多次未能完成，已停止自动恢复"
    await lifecycle.recover_terminal(
        db,
        run.id,
        status=status,
        error_code=code,
        error_message=message,
        expect_recovery_count=run.recovery_count,
    )


async def scan_once(session_factory=None) -> int:
    session_factory = session_factory or AsyncSessionFactory
    now = utc_now()
    async with session_factory() as db:
        runs = list(
            (
                await db.execute(
                    select(Run)
                    .where(
                        Run.status.in_(ACTIVE),
                        Run.recovery_count < Run.max_recovery_count,
                    )
                    .order_by(Run.queued_at)
                    .limit(50)
                )
            ).scalars()
        )
    scheduled = 0
    for run in runs:
        if not _expired(run, now):
            continue
        async with session_factory() as db:
            next_generation = run.recovery_count + 1
            result = await db.execute(
                update(Run)
                .where(
                    Run.id == run.id,
                    Run.status == run.status,
                    Run.recovery_count == run.recovery_count,
                )
                .values(
                    recovery_count=next_generation,
                    status=case(
                        (Run.status == RunStatus.CANCELLING, RunStatus.CANCELLING),
                        (Run.status == RunStatus.QUEUED, RunStatus.QUEUED),
                        else_=RunStatus.RECOVERING,
                    ),
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await db.rollback()
                continue
            db.add(
                ChatOutboxEvent(
                    aggregate_type="run",
                    aggregate_id=run.id,
                    event_type="run.recovery_requested",
                    payload_json={
                        "run_id": run.id,
                        "dispatch_reason": "recovery",
                        "dispatch_generation": next_generation,
                        "schema_version": 1,
                    },
                    deduplication_key=f"run:{run.id}:recovery:{next_generation}",
                    available_at=now,
                    created_at=now,
                )
            )
            await db.commit()
            scheduled += 1
            logger.info(
                "chat run scheduled for recovery",
                extra={
                    "run_id": run.id,
                    "recovery_count": next_generation,
                    "previous_status": run.status,
                },
            )
    async with session_factory() as db:
        exhausted = list(
            (
                await db.execute(
                    select(Run).where(
                        Run.status.in_(ACTIVE),
                        Run.recovery_count >= Run.max_recovery_count,
                    )
                )
            ).scalars()
        )
        for run in exhausted:
            if not _expired(run, now):
                continue
            await _settle_exhausted(db, run, now)
    return scheduled


async def run_chat_recovery() -> None:
    while True:
        try:
            await scan_once()
            await purge_expired_events()
        except Exception:
            logger.exception("chat recovery scan failed")
        await asyncio.sleep(max(0.5, settings.chat_recovery_interval_seconds))