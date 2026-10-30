"""Short database transactions; never keep a transaction open while calling the LLM."""

from dataclasses import dataclass

from sqlalchemy import func, or_, select, update

from app.business.errors import Conflict, NotFound
from app.business.utils import new_id, utc_now
from app.chat.contracts import TRIGGER_REASONS, TurnStatus
from app.chat.models import (
    ChatOutboxEvent,
    Conversation,
    Message,
    Run,
    RunEvent,
    Turn,
)


@dataclass(frozen=True)
class TurnSubmission:
    conversation_id: str
    turn_id: str
    run_id: str
    status: str


async def create_conversation(db, user_id):
    row = Conversation(user_id=user_id)
    db.add(row)
    await db.commit()
    return row


def _title_from_content(content: str) -> str:
    return " ".join(content.split())[:120] or "新会话"


async def create_turn_and_initial_run(
    db,
    user_id: str,
    conversation_id: str,
    content: str,
    *,
    client_request_id: str,
    max_run_attempts: int = 3,
) -> TurnSubmission:
    await get_conversation(db, user_id, conversation_id)
    existing = (
        await db.execute(
            select(Turn).where(
                Turn.conversation_id == conversation_id,
                Turn.client_request_id == client_request_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        run = await db.get(Run, existing.latest_run_id)
        return TurnSubmission(conversation_id, existing.id, run.id, run.status)

    # End the ownership-read snapshot before the atomic occupation CAS.
    await db.rollback()
    now = utc_now()
    turn_id = new_id()
    run_id = new_id()
    claimed = await db.execute(
        update(Conversation)
        .where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
            Conversation.active_turn.is_(None),
        )
        .values(active_turn=turn_id, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        await db.rollback()
        existing = (
            await db.execute(
                select(Turn).where(
                    Turn.conversation_id == conversation_id,
                    Turn.client_request_id == client_request_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            run = await db.get(Run, existing.latest_run_id)
            return TurnSubmission(conversation_id, existing.id, run.id, run.status)
        raise Conflict("此会话已有活动轮次", code="CHAT_BUSY")

    conversation = (
        await db.execute(
            select(Conversation)
            .where(Conversation.id == conversation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    turn_no = (
        await db.execute(
            select(func.coalesce(func.max(Turn.turn_no), 0) + 1).where(
                Turn.conversation_id == conversation_id
            )
        )
    ).scalar_one()
    message = Message(
        conversation_id=conversation_id,
        turn_id=turn_id,
        run_id=None,
        sequence=conversation.next_sequence,
        role="user",
        content=content,
        status="complete",
    )
    conversation.next_sequence += 1
    if message.sequence == 1:
        conversation.title = _title_from_content(content)
    db.add(message)
    await db.flush()
    turn = Turn(
        id=turn_id,
        conversation_id=conversation_id,
        user_message_id=message.id,
        turn_no=turn_no,
        status="active",
        run_count=1,
        max_run_attempts=max_run_attempts,
        client_request_id=client_request_id,
        created_at=now,
        updated_at=now,
    )
    db.add(turn)
    await db.flush()
    run = Run(
        id=run_id,
        turn_id=turn.id,
        attempt_no=1,
        trigger_reason="initial",
        status="queued",
        checkpoint_thread_id=run_id,
        queued_at=now,
        created_at=now,
        updated_at=now,
        next_event_seq=2,
    )
    db.add(run)
    turn.latest_run_id = run.id
    turn.active_run_id = run.id
    db.add(
        RunEvent(
            run_id=run.id,
            seq=1,
            event_type="run.queued",
            payload_json={"run_id": run.id, "turn_id": turn.id, "status": "queued"},
            created_at=now,
        )
    )
    db.add(
        ChatOutboxEvent(
            aggregate_type="run",
            aggregate_id=run.id,
            event_type="run.created",
            payload_json={
                "run_id": run.id,
                "dispatch_reason": "initial",
                "dispatch_generation": 0,
                "schema_version": 1,
            },
            deduplication_key=f"run:{run.id}:created",
            available_at=now,
            created_at=now,
        )
    )
    await db.commit()
    return TurnSubmission(conversation_id, turn.id, run.id, run.status)


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
    # Show the selected (latest) Run of each Turn; keep user rows and legacy
    # messages that predate the Run projection.
    query = (
        select(Message)
        .outerjoin(Turn, Turn.id == Message.turn_id)
        .where(
            Message.conversation_id == conversation_id,
            or_(
                Message.role == "user",
                Message.run_id.is_(None),
                Message.run_id == Turn.latest_run_id,
            ),
        )
    )
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


async def create_retry_run(db, user_id: str, turn_id: str, *, trigger_reason: str) -> Run:
    """Create the next Run on a settled Turn and re-occupy its Conversation."""

    if trigger_reason not in TRIGGER_REASONS or trigger_reason == "initial":
        raise Conflict("不支持的重新执行原因", code="CHAT_TRIGGER_INVALID")
    turn = (
        await db.execute(
            select(Turn)
            .where(Turn.id == turn_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if turn is None:
        raise NotFound("轮次不存在")
    conversation = (
        await db.execute(
            select(Conversation)
            .where(
                Conversation.id == turn.conversation_id,
                Conversation.user_id == user_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise NotFound("轮次不存在")
    latest_turn_no = (
        await db.execute(
            select(func.max(Turn.turn_no)).where(
                Turn.conversation_id == conversation.id
            )
        )
    ).scalar_one()
    if turn.turn_no != latest_turn_no:
        raise Conflict("只能重新执行最新一轮", code="CHAT_TURN_NOT_LATEST")
    if turn.active_run_id is not None:
        raise Conflict("该轮仍有执行中的任务", code="CHAT_RUN_ACTIVE")
    if turn.status not in (
        TurnStatus.FAILED,
        TurnStatus.CANCELLED,
        TurnStatus.COMPLETED,
        TurnStatus.REQUIRES_ATTENTION,
    ):
        raise Conflict("该轮当前不能重新执行", code="CHAT_TURN_NOT_SETTLED")
    if turn.run_count >= turn.max_run_attempts:
        raise Conflict("该轮重新执行次数已达上限", code="CHAT_RUN_LIMIT")
    if conversation.active_turn is not None:
        raise Conflict("此会话已有活动轮次", code="CHAT_BUSY")

    now = utc_now()
    run_id = new_id()
    run = Run(
        id=run_id,
        turn_id=turn.id,
        attempt_no=turn.run_count + 1,
        retry_of_run_id=turn.latest_run_id,
        trigger_reason=trigger_reason,
        status="queued",
        checkpoint_thread_id=run_id,
        queued_at=now,
        created_at=now,
        updated_at=now,
        next_event_seq=2,
    )
    db.add(run)
    await db.flush()
    turn.status = TurnStatus.ACTIVE
    turn.active_run_id = run.id
    turn.latest_run_id = run.id
    turn.run_count = turn.run_count + 1
    turn.closed_at = None
    turn.updated_at = now
    conversation.active_turn = turn.id
    conversation.updated_at = now
    db.add(
        RunEvent(
            run_id=run.id,
            seq=1,
            event_type="run.queued",
            payload_json={
                "run_id": run.id,
                "turn_id": turn.id,
                "status": "queued",
                "trigger_reason": trigger_reason,
            },
            created_at=now,
        )
    )
    db.add(
        ChatOutboxEvent(
            aggregate_type="run",
            aggregate_id=run.id,
            event_type="run.created",
            payload_json={
                "run_id": run.id,
                "dispatch_reason": trigger_reason,
                "dispatch_generation": 0,
                "schema_version": 1,
            },
            deduplication_key=f"run:{run.id}:created",
            available_at=now,
            created_at=now,
        )
    )
    await db.commit()
    return run


async def history_rows(db, conversation_id: str, *, max_turns=20, max_chars=60000):
    """Rows of completed prior Turns, restricted to each Turn's selected Run.

    Legacy conversations created before chat_turns existed have no Turn rows;
    their completed messages are still replayed so old history stays readable.
    """

    turns = list(
        (
            await db.execute(
                select(Turn)
                .where(
                    Turn.conversation_id == conversation_id,
                    Turn.status == TurnStatus.COMPLETED,
                )
                .order_by(Turn.turn_no.desc())
                .limit(max_turns)
            )
        ).scalars()
    )
    rows = []
    if turns:
        for turn in reversed(turns):
            user_message = await db.get(Message, turn.user_message_id)
            if user_message is not None:
                rows.append(user_message)
            query = select(Message).where(
                Message.turn_id == turn.id,
                Message.role.in_(("assistant", "tool")),
            )
            if turn.latest_run_id is not None:
                query = query.where(
                    or_(
                        Message.run_id == turn.latest_run_id,
                        Message.run_id.is_(None),
                    )
                )
            rows.extend(
                list(
                    (
                        await db.execute(query.order_by(Message.sequence))
                    ).scalars()
                )
            )
    else:
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
        if starts:
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
        rows = [row for row in rows if row.turn_id != oldest]
    return rows


async def get_run_for_user(db, user_id: str, run_id: str) -> Run:
    row = (
        await db.execute(
            select(Run)
            .join(Turn, Turn.id == Run.turn_id)
            .join(Conversation, Conversation.id == Turn.conversation_id)
            .where(Run.id == run_id, Conversation.user_id == user_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("执行不存在")
    return row


async def get_turn_for_user(db, user_id: str, turn_id: str) -> Turn:
    row = (
        await db.execute(
            select(Turn)
            .join(Conversation, Conversation.id == Turn.conversation_id)
            .where(Turn.id == turn_id, Conversation.user_id == user_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("轮次不存在")
    return row


async def list_turns(db, user_id: str, conversation_id: str, *, limit=20):
    await get_conversation(db, user_id, conversation_id)
    return list(
        (
            await db.execute(
                select(Turn)
                .where(Turn.conversation_id == conversation_id)
                .order_by(Turn.turn_no.desc())
                .limit(limit)
            )
        ).scalars()
    )


async def list_runs(db, turn_id: str):
    return list(
        (
            await db.execute(
                select(Run).where(Run.turn_id == turn_id).order_by(Run.attempt_no)
            )
        ).scalars()
    )


async def project_run_message(
    db,
    *,
    run: Run,
    turn: Turn,
    role: str,
    content: str,
    source_key: str,
    tool_calls: list | None = None,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
) -> Message:
    """Idempotently project one protocol message into the conversation transcript."""

    existing = (
        await db.execute(
            select(Message).where(
                Message.run_id == run.id,
                Message.source_key == source_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    conversation = (
        await db.execute(
            select(Conversation)
            .where(Conversation.id == turn.conversation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    # Another concurrent projector may have inserted the same source_key while
    # this transaction waited for the conversation row lock.
    existing = (
        await db.execute(
            select(Message).where(
                Message.run_id == run.id,
                Message.source_key == source_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    message = Message(
        conversation_id=conversation.id,
        sequence=conversation.next_sequence,
        turn_id=turn.id,
        run_id=run.id,
        source_key=source_key,
        role=role,
        content=content,
        tool_calls=tool_calls,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        status="complete",
    )
    conversation.next_sequence += 1
    conversation.updated_at = utc_now()
    db.add(message)
    await db.flush()
    return message
