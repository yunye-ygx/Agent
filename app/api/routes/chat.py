import asyncio
import json
import logging
import time
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_principal, require_csrf
from app.business.errors import Unavailable
from app.business.principal import Principal
from app.chat import events, lifecycle, service, store
from app.chat.contracts import SETTLED_RUN_STATUSES, TERMINAL_EVENT_TYPES
from app.chat.models import Run
from app.core.config import settings
from app.db.mysql_session import AsyncSessionFactory, get_mysql_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


class SendMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    content: str = Field(min_length=1, max_length=8000)

    @field_validator("content")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("消息不能为空")
        return value.strip()


class CreateRun(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    trigger_reason: Literal["failure_retry", "cancel_retry", "regenerate"]


@router.post("/conversations", status_code=201)
async def create_conversation(
    principal: Principal = Depends(require_csrf), db: AsyncSession = Depends(get_mysql_session)
):
    return service.conversation_payload(await store.create_conversation(db, principal.user_id))


@router.get("/conversations")
async def list_conversations(
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_mysql_session),
):
    rows = await store.list_conversations(db, principal.user_id, offset=offset, limit=31)
    return {
        "items": [service.conversation_payload(r) for r in rows[:30]],
        "next_offset": offset + 30 if len(rows) > 30 else None,
    }


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: str,
    before: int | None = Query(default=None, ge=1),
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_mysql_session),
):
    conv = await store.get_conversation(db, principal.user_id, conversation_id)
    rows = await store.list_messages(
        db, principal.user_id, conversation_id, before=before, limit=51
    )
    page = rows[-50:]
    return {
        "conversation": service.conversation_payload(conv),
        "items": [service.message_payload(r) for r in page],
        "next_before": page[0].sequence if len(rows) > 50 else None,
    }


@router.get("/conversations/{conversation_id}/turns")
async def list_turns(
    conversation_id: str,
    limit: int = Query(default=20, ge=1, le=50),
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_mysql_session),
):
    await store.get_conversation(db, principal.user_id, conversation_id)
    turns = await store.list_turns(
        db, principal.user_id, conversation_id, limit=limit
    )
    run_ids = [turn.latest_run_id for turn in turns if turn.latest_run_id]
    runs = {}
    if run_ids:
        runs = {
            row.id: row
            for row in (
                await db.execute(select(Run).where(Run.id.in_(run_ids)))
            ).scalars()
        }
    return {
        "items": [
            service.turn_payload(turn, runs.get(turn.latest_run_id)) for turn in turns
        ]
    }


@router.post("/conversations/{conversation_id}/messages", status_code=202)
async def send_message(
    conversation_id: str,
    payload: SendMessage,
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=1, max_length=100
    ),
    principal: Principal = Depends(require_csrf),
    db: AsyncSession = Depends(get_mysql_session),
):
    try:
        submission = await store.create_turn_and_initial_run(
            db,
            principal.user_id,
            conversation_id,
            payload.content,
            client_request_id=idempotency_key,
        )
    except SQLAlchemyError as exc:
        await db.rollback()
        raise Unavailable(
            "聊天执行表尚未初始化，请先执行 durable chat 迁移",
            code="CHAT_SCHEMA_MISSING",
        ) from exc
    return service.submission_payload(submission)


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_mysql_session),
):
    run = await store.get_run_for_user(db, principal.user_id, run_id)
    return service.run_payload(run)


async def _event_stream(run_id: str, after: int):
    last = max(0, after)
    terminal_seen = False
    last_ping = time.monotonic()
    yield events.SSE_RETRY
    while True:
        async with AsyncSessionFactory() as db:
            run = await db.get(Run, run_id)
            rows = await events.read_events(db, run_id, after=last, limit=200)
            has_events = bool(rows) or await events.last_event_seq(db, run_id) > 0
        if run is None:
            return
        for row in rows:
            last = row.seq
            if row.event_type in TERMINAL_EVENT_TYPES:
                terminal_seen = True
            yield events.sse_frame(row)
        if terminal_seen:
            return
        if run.status in SETTLED_RUN_STATUSES:
            if not has_events:
                yield (
                    f"event: run.{run.status}\n"
                    f"data: {json.dumps({'run_id': run.id, 'turn_id': run.turn_id, 'status': run.status}, ensure_ascii=False)}\n\n"
                )
            return
        if time.monotonic() - last_ping >= 15:
            last_ping = time.monotonic()
            yield events.SSE_HEARTBEAT
        if not rows:
            await asyncio.sleep(settings.chat_event_poll_interval_seconds)


@router.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_mysql_session),
):
    await store.get_run_for_user(db, principal.user_id, run_id)
    cursor = after
    if last_event_id is not None:
        try:
            cursor = max(cursor, int(last_event_id))
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "INVALID_LAST_EVENT_ID",
                    "message": "Last-Event-ID 必须是整数",
                },
            ) from exc
    return StreamingResponse(
        _event_stream(run_id, cursor),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/runs/{run_id}/cancel", status_code=202)
async def cancel_run(
    run_id: str,
    principal: Principal = Depends(require_csrf),
    db: AsyncSession = Depends(get_mysql_session),
):
    await store.get_run_for_user(db, principal.user_id, run_id)
    run = await lifecycle.request_cancel(db, run_id)
    return service.run_payload(run)


@router.post("/turns/{turn_id}/runs", status_code=202)
async def create_turn_run(
    turn_id: str,
    payload: CreateRun,
    principal: Principal = Depends(require_csrf),
    db: AsyncSession = Depends(get_mysql_session),
):
    await store.get_turn_for_user(db, principal.user_id, turn_id)
    run = await store.create_retry_run(
        db,
        principal.user_id,
        turn_id,
        trigger_reason=payload.trigger_reason,
    )
    return service.run_payload(run)


@router.get("/turns/{turn_id}/runs")
async def list_turn_runs(
    turn_id: str,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_mysql_session),
):
    turn = await store.get_turn_for_user(db, principal.user_id, turn_id)
    runs = await store.list_runs(db, turn.id)
    return {
        "turn": service.turn_payload(turn, runs[-1] if runs else None),
        "items": [service.run_payload(run) for run in runs],
    }