"""Short database transactions; never keep a transaction open while calling the LLM."""

from sqlalchemy import select, update

from app.business.errors import Conflict, NotFound
from app.business.utils import new_id, utc_now
from app.chat.models import Conversation, Message


async def create_conversation(db, user_id):
    row = Conversation(user_id=user_id)
    db.add(row)
    await db.commit()
    return row


async def get_conversation(db, user_id, conversation_id):
    row = (
        await db.execute(
            select(Conversation)
            .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("会话不存在")
    return row


async def list_conversations(db, user_id, *, offset=0, limit=30):
    return list(
        (
            await db.execute(
                select(Conversation)
                .where(Conversation.user_id == user_id)
                .order_by(Conversation.updated_at.desc(), Conversation.id)
                .offset(offset)
                .limit(limit)
            )
        ).scalars()
    )


async def list_messages(db, user_id, conversation_id, *, before=None, limit=50):
    await get_conversation(db, user_id, conversation_id)
    query = select(Message).where(Message.conversation_id == conversation_id)
    if before is not None:
        query = query.where(Message.sequence < before)
    return list(
        reversed(
            (await db.execute(query.order_by(Message.sequence.desc()).limit(limit))).scalars().all()
        )
    )


async def begin_turn(db, user_id, conversation_id, content):
    await get_conversation(db, user_id, conversation_id)
    # End the ownership-read snapshot before the atomic claim (MySQL REPEATABLE READ).
    await db.rollback()
    turn_id = new_id()
    result = await db.execute(
        update(Conversation)
        .where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
            Conversation.active_turn.is_(None),
        )
        .values(active_turn=turn_id, updated_at=utc_now())
    )
    if result.rowcount != 1:
        await db.rollback()
        raise Conflict("此会话有尚未完成的回复，请等待完成或新建会话", code="CHAT_BUSY")
    await _append(db, conversation_id, turn_id, "user", content)
    await db.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id, Conversation.next_sequence == 2)
        .values(title=content[:120])
    )
    await db.commit()
    return turn_id


async def _append(db, conversation_id, turn_id, role, content, **fields):
    # Current locking read avoids stale snapshots and serializes sequence assignment.
    conv = (
        await db.execute(
            select(Conversation)
            .where(Conversation.id == conversation_id, Conversation.active_turn == turn_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if conv is None:
        raise Conflict("本轮已结束，不能继续写入消息", code="CHAT_TURN_CLOSED")
    row = Message(
        conversation_id=conversation_id,
        turn_id=turn_id,
        sequence=conv.next_sequence,
        role=role,
        content=content,
        **fields,
    )
    conv.next_sequence += 1
    conv.updated_at = utc_now()
    db.add(row)
    await db.flush()
    return row


async def append_message(db, conversation_id, turn_id, role, content, **fields):
    row = await _append(db, conversation_id, turn_id, role, content, **fields)
    await db.commit()
    return row


async def finish_turn(db, conversation_id, turn_id, *, succeeded):
    result = await db.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id, Conversation.active_turn == turn_id)
        .values(active_turn=None, updated_at=utc_now())
    )
    if result.rowcount != 1:
        await db.rollback()
        raise Conflict("本轮已结束", code="CHAT_TURN_CLOSED")
    await db.execute(
        update(Message)
        .where(Message.conversation_id == conversation_id, Message.turn_id == turn_id)
        .values(status="complete" if succeeded else "failed")
    )
    await db.commit()


async def history(db, conversation_id, *, max_turns=20, max_chars=60000):
    # Select whole completed turns, never orphan a tool response by cutting at N messages.
    starts = list(
        (
            await db.execute(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.role == "user",
                    Message.status == "complete",
                )
                .order_by(Message.sequence.desc())
                .limit(max_turns)
            )
        ).scalars()
    )
    if not starts:
        return []
    rows = list(
        (
            await db.execute(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.status == "complete",
                    Message.turn_id.in_([m.turn_id for m in starts]),
                )
                .order_by(Message.sequence)
            )
        ).scalars()
    )
    import json

    while rows and sum(len(r.content) + len(json.dumps(r.tool_calls)) for r in rows) > max_chars:
        oldest = rows[0].turn_id
        rows = [r for r in rows if r.turn_id != oldest]
    return rows
