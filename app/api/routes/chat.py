from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_principal, require_csrf
from app.business.principal import Principal
from app.chat import service, store
from app.db.mysql_session import get_mysql_session

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


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: str,
    payload: SendMessage,
    principal: Principal = Depends(require_csrf),
    db: AsyncSession = Depends(get_mysql_session),
):
    await store.get_conversation(db, principal.user_id, conversation_id)
    # Claim first: two HTTP requests must not prepare the same stale history concurrently.
    turn_id = await store.begin_turn(db, principal.user_id, conversation_id, payload.content)
    try:
        agent, history = await service.prepare(db, principal, conversation_id)
        await db.rollback()
    except Exception:
        await db.rollback()
        await store.finish_turn(db, conversation_id, turn_id, succeeded=False)
        raise
    return StreamingResponse(
        service.stream_reply(conversation_id, turn_id, payload.content, agent, history),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
    )
