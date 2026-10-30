"""Durable Run execution: checkpoint resume, event projection, final answer.

MySQL Run state is authoritative. PostgreSQL only remembers where the graph is;
every externally visible fact is projected into MySQL before it is emitted.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import select

from app.business.errors import Unavailable
from app.business.models import BusinessUser
from app.business.principal import Principal
from app.business.utils import utc_now
from app.chat import events, lifecycle, store
from app.chat.agent import (
    build_agent,
    model_from_configuration,
    restore_messages,
    stream_agent,
    text_content,
)
from app.chat.checkpoints import open_postgres_checkpointer
from app.chat.contracts import (
    CHECKPOINT_SCHEMA_VERSION,
    ExecutionLost,
    RunCancelled,
    RunLease,
    RunStatus,
    ToolRequiresAttention,
    TurnStatus,
)
from app.chat.models import Conversation, Message, Run, RunEvent, Turn
from app.chat.tool_execution import DurableToolRecorder
from app.core.config import settings
from app.db.mysql_models import ModelConfiguration
from app.db.mysql_session import AsyncSessionFactory

logger = logging.getLogger(__name__)


@dataclass
class RunContext:
    run: Run
    turn: Turn
    conversation: Conversation
    principal: Principal
    user_message: Message
    history_rows: list
    model: object
    lease: RunLease | None = None


class LeaseHeartbeat:
    """Renew the lease independently of a long model or tool call."""

    def __init__(
        self,
        session_factory,
        *,
        lease: RunLease,
        interval_seconds: float,
        lease_seconds: int,
    ):
        self.session_factory = session_factory
        self.lease = lease
        self.interval_seconds = interval_seconds
        self.lease_seconds = lease_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._lost: ExecutionLost | None = None

    async def __aenter__(self):
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        return False

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass
            else:
                return
            try:
                async with self.session_factory() as db:
                    await lifecycle.renew_run_lease(
                        db,
                        self.lease.run_id,
                        worker_id=self.lease.worker_id,
                        lease_generation=self.lease.lease_generation,
                        lease_seconds=self.lease_seconds,
                    )
            except ExecutionLost as exc:
                self._lost = exc
                return
            except Exception:
                logger.warning(
                    "chat lease renewal failed",
                    extra={"run_id": self.lease.run_id},
                )
                self._lost = ExecutionLost(
                    f"lease renewal unavailable for run {self.lease.run_id}"
                )
                return

    def check(self) -> None:
        if self._lost is not None:
            raise self._lost


async def _load_context(session_factory, lease: RunLease) -> RunContext:
    async with session_factory() as db:
        run = await lifecycle.assert_run_ownership(
            db,
            lease.run_id,
            worker_id=lease.worker_id,
            lease_generation=lease.lease_generation,
        )
        turn = await db.get(Turn, run.turn_id)
        conversation = await db.get(Conversation, turn.conversation_id)
        user = await db.get(BusinessUser, conversation.user_id)
        if user is None or user.status != "active":
            raise Unavailable("登录状态已失效", code="CHAT_UNAUTHENTICATED")
        principal = Principal.for_user(
            user_id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
        )
        user_message = await db.get(Message, turn.user_message_id)
        if user_message is None:
            raise Unavailable("用户消息不存在", code="CHAT_MESSAGE_MISSING")
        configs = list(
            (
                await db.execute(
                    select(ModelConfiguration).where(
                        ModelConfiguration.is_default.is_(True)
                    )
                )
            ).scalars()
        )
        if len(configs) != 1:
            raise Unavailable("请管理员设置一个默认聊天模型", code="CHAT_MODEL_MISSING")
        model = model_from_configuration(configs[0])
        history = await store.history_rows(db, conversation.id)
    return RunContext(
        run=run,
        turn=turn,
        conversation=conversation,
        principal=principal,
        user_message=user_message,
        history_rows=history,
        model=model,
    )


def _build_agent(ctx: RunContext, recorder, saver):
    return build_agent(
        ctx.model,
        ctx.principal,
        checkpointer=saver,
        tool_recorder=recorder,
    )


@asynccontextmanager
async def _open_checkpointer():
    url = settings.checkpoint_postgres_url
    if not url:
        yield None
        return
    try:
        async with open_postgres_checkpointer(url) as saver:
            yield saver
    except Exception as exc:
        if settings.chat_require_checkpoint:
            raise Unavailable(
                "检查点存储暂不可用", code="CHAT_CHECKPOINT_UNAVAILABLE"
            ) from exc
        logger.warning("chat checkpoint unavailable; continuing without it")
        yield None


async def _emit(
    session_factory,
    lease: RunLease,
    event_type: str,
    payload: dict,
    *,
    model_response_id: str | None = None,
    response_segment_id: str | None = None,
) -> None:
    await events.emit_event(
        session_factory,
        run_id=lease.run_id,
        event_type=event_type,
        payload={"run_id": lease.run_id, "turn_id": lease.turn_id, **payload},
        lease=lease,
        model_response_id=model_response_id,
        response_segment_id=response_segment_id,
    )


async def _assert_not_cancelled(session_factory, lease: RunLease) -> None:
    async with session_factory() as db:
        run = await db.get(Run, lease.run_id)
        if run is None:
            raise ExecutionLost(f"run no longer exists: {lease.run_id}")
        if run.status == RunStatus.CANCELLING:
            raise RunCancelled(f"run {lease.run_id} is cancelling")
        if run.status in (
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.REQUIRES_ATTENTION,
        ):
            raise ExecutionLost(f"run is no longer executable: {run.status}")


def _current_run_messages(messages: list) -> list:
    """Messages produced after the latest user message belong to this run.

    The checkpoint also stores prior completed turns used as context; projecting
    those again under the current run_id would duplicate conversation history.
    """

    start = 0
    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage):
            start = index
    return messages[start + 1 :]


async def _project_checkpoint_messages(
    session_factory, ctx: RunContext, messages: list
) -> None:
    async with session_factory() as db:
        run = await lifecycle.assert_run_ownership(
            db,
            ctx.run.id,
            worker_id=ctx.lease.worker_id,
            lease_generation=ctx.lease.lease_generation,
        )
        turn = await db.get(Turn, run.turn_id)
        for message in _current_run_messages(messages):
            if isinstance(message, AIMessage) and message.tool_calls:
                await store.project_run_message(
                    db,
                    run=run,
                    turn=turn,
                    role="assistant",
                    content=text_content(message.content),
                    source_key=f"assistant:{message.tool_calls[0]['id']}",
                    tool_calls=message.tool_calls,
                )
            elif isinstance(message, ToolMessage):
                await store.project_run_message(
                    db,
                    run=run,
                    turn=turn,
                    role="tool",
                    content=text_content(message.content),
                    source_key=f"tool:{message.tool_call_id}",
                    tool_call_id=message.tool_call_id,
                    tool_name=message.name,
                )
        await db.commit()


def _last_final_message(messages: list) -> dict | None:
    for message in reversed(_current_run_messages(messages)):
        if isinstance(message, AIMessage) and not message.tool_calls:
            content = text_content(message.content)
            if content.strip():
                return {"content": content, "message_id": message.id}
    return None


async def _project_assistant_tool_calls(
    session_factory,
    ctx: RunContext,
    *,
    content: str,
    tool_calls: list[dict],
    message_id: str | None,
    finish_reason: str | None,
) -> None:
    async with session_factory() as db:
        run = await lifecycle.assert_run_ownership(
            db,
            ctx.run.id,
            worker_id=ctx.lease.worker_id,
            lease_generation=ctx.lease.lease_generation,
        )
        turn = await db.get(Turn, run.turn_id)
        message = await store.project_run_message(
            db,
            run=run,
            turn=turn,
            role="assistant",
            content=content,
            source_key=f"assistant:{tool_calls[0]['id']}",
            tool_calls=tool_calls,
        )
        run = await events.lock_run(db, run.id, lease=ctx.lease)
        existing_event = (
            await db.execute(
                select(RunEvent).where(
                    RunEvent.run_id == run.id,
                    RunEvent.event_type == "response.completed",
                    RunEvent.model_response_id == message_id,
                )
            )
        ).scalar_one_or_none()
        if existing_event is None:
            await events.append_event(
                db,
                run=run,
                event_type="response.completed",
                payload={
                    "run_id": run.id,
                    "turn_id": turn.id,
                    "message_id": message.id,
                    "content": content,
                    "finish_reason": finish_reason,
                    "tool_calls": tool_calls,
                },
                lease=ctx.lease,
                model_response_id=message_id,
            )
        await db.commit()


async def project_final_response(
    db,
    *,
    run_id: str,
    worker_id: str,
    lease_generation: int,
    model_response_id: str,
    content: str,
) -> Message:
    """Idempotently project a completed graph checkpoint into MySQL truth."""

    source_key = f"final:{model_response_id}"
    existing = (
        await db.execute(
            select(Message).where(
                Message.run_id == run_id,
                Message.source_key == source_key,
            )
        )
    ).scalar_one_or_none()
    run = (
        await db.execute(
            select(Run)
            .where(Run.id == run_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if run is None:
        raise ExecutionLost(f"run no longer exists: {run_id}")
    if existing is not None and run.status == RunStatus.COMPLETED:
        if existing.content != content:
            raise ValueError("completed checkpoint content changed during replay")
        return existing

    run = await lifecycle.assert_run_ownership(
        db,
        run_id,
        worker_id=worker_id,
        lease_generation=lease_generation,
    )
    if run.status == RunStatus.CANCELLING:
        raise RunCancelled(f"run {run_id} is cancelling")
    turn = await db.get(Turn, run.turn_id)
    write_lease = RunLease(
        run_id=run.id,
        turn_id=run.turn_id,
        worker_id=worker_id,
        lease_generation=lease_generation,
    )
    conversation = (
        await db.execute(
            select(Conversation)
            .where(
                Conversation.id == turn.conversation_id,
                Conversation.active_turn == turn.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise ExecutionLost(f"turn is no longer active: {turn.id}")

    message = await store.project_run_message(
        db,
        run=run,
        turn=turn,
        role="assistant",
        content=content,
        source_key=source_key,
    )
    if message.content != content:
        raise ValueError("checkpoint projection content changed during replay")

    run = await events.lock_run(db, run_id, lease=write_lease)
    response_event = (
        await db.execute(
            select(RunEvent).where(
                RunEvent.run_id == run.id,
                RunEvent.event_type == "response.completed",
                RunEvent.model_response_id == model_response_id,
            )
        )
    ).scalar_one_or_none()
    if response_event is None:
        await events.append_event(
            db,
            run=run,
            event_type="response.completed",
            model_response_id=model_response_id,
            payload={
                "run_id": run.id,
                "turn_id": turn.id,
                "message_id": message.id,
                "content": content,
            },
            lease=write_lease,
        )

    now = utc_now()
    completed_event = (
        await db.execute(
            select(RunEvent).where(
                RunEvent.run_id == run.id,
                RunEvent.event_type == "run.completed",
            )
        )
    ).scalar_one_or_none()
    if completed_event is None:
        await events.append_event(
            db,
            run=run,
            event_type="run.completed",
            payload={
                "run_id": run.id,
                "turn_id": turn.id,
                "status": RunStatus.COMPLETED,
                "finished_at": now.isoformat(),
            },
            lease=write_lease,
        )

    run.status = RunStatus.COMPLETED
    run.finished_at = now
    run.updated_at = now
    run.lease_owner = None
    run.lease_expires_at = None
    turn.status = TurnStatus.COMPLETED
    turn.active_run_id = None
    turn.latest_run_id = run.id
    turn.closed_at = now
    turn.updated_at = now
    conversation.active_turn = None
    conversation.updated_at = now
    await db.commit()
    return message


async def _finish(
    session_factory,
    lease: RunLease,
    status: str,
    code: str,
    message: str,
) -> None:
    try:
        async with session_factory() as db:
            await lifecycle.finish_run(
                db,
                lease.run_id,
                worker_id=lease.worker_id,
                lease_generation=lease.lease_generation,
                status=status,
                error_code=code,
                error_message=message,
            )
    except ExecutionLost:
        logger.warning(
            "chat run finish skipped; ownership lost",
            extra={"run_id": lease.run_id},
        )


async def _complete(
    session_factory, lease: RunLease, content: str, model_response_id: str
) -> None:
    async with session_factory() as db:
        await project_final_response(
            db,
            run_id=lease.run_id,
            worker_id=lease.worker_id,
            lease_generation=lease.lease_generation,
            model_response_id=model_response_id,
            content=content,
        )


async def _requires_attention(
    session_factory, lease: RunLease, code: str, message: str
) -> None:
    try:
        async with session_factory() as db:
            await lifecycle.mark_requires_attention(
                db,
                lease.run_id,
                worker_id=lease.worker_id,
                lease_generation=lease.lease_generation,
                error_code=code,
                error_message=message,
            )
    except ExecutionLost:
        logger.warning(
            "chat requires_attention skipped; ownership lost",
            extra={"run_id": lease.run_id},
        )


async def _execute(
    ctx: RunContext,
    *,
    lease: RunLease,
    recorder,
    agent,
    saver,
    session_factory,
) -> None:
    config = {
        "configurable": {"thread_id": ctx.run.checkpoint_thread_id},
        "recursion_limit": 24,
    }
    state = await agent.aget_state(config) if saver is not None else None
    messages = (state.values or {}).get("messages") if state is not None else None
    resume = bool(messages)
    if resume:
        await _project_checkpoint_messages(session_factory, ctx, messages)
        if not state.next:
            final = _last_final_message(messages)
            if final is None:
                await _finish(
                    session_factory,
                    lease,
                    "failed",
                    "CHAT_EMPTY_RESPONSE",
                    "模型未产生可用的最终回答",
                )
                return
            await _complete(
                session_factory,
                lease,
                final["content"],
                final["message_id"] or "final",
            )
            return
        for message in reversed(_current_run_messages(messages)):
            if isinstance(message, AIMessage) and message.tool_calls:
                recorder.begin_response(message.id, message.tool_calls)
                break
        await _emit(
            session_factory,
            lease,
            "response.restarted",
            {"response_segment_id": None},
        )
        await _emit(
            session_factory,
            lease,
            "text.reset",
            {"response_segment_id": None},
        )
        input_messages = None
    else:
        input_messages = [
            *restore_messages(ctx.history_rows),
            HumanMessage(content=ctx.user_message.content),
        ]

    segment_id: str | None = None
    delta_buffer: list[str] = []
    last_flush = time.monotonic()
    last_cancel_check = 0.0
    deadline = last_flush + settings.chat_execution_timeout_seconds
    final: dict | None = None

    async def flush(force: bool = False) -> None:
        nonlocal last_flush
        if not delta_buffer:
            last_flush = time.monotonic()
            return
        if (
            not force
            and len("".join(delta_buffer)) < settings.chat_event_batch_chars
            and time.monotonic() - last_flush
            < settings.chat_event_batch_interval_seconds
        ):
            return
        text = "".join(delta_buffer)
        delta_buffer.clear()
        last_flush = time.monotonic()
        await _emit(
            session_factory,
            lease,
            "text.delta",
            {"delta": text, "response_segment_id": segment_id},
            response_segment_id=segment_id,
        )

    async with LeaseHeartbeat(
        session_factory,
        lease=lease,
        interval_seconds=settings.chat_heartbeat_interval_seconds,
        lease_seconds=settings.chat_lease_seconds,
    ) as heartbeat:
        async for kind, data in stream_agent(agent, input_messages, config=config):
            heartbeat.check()
            now = time.monotonic()
            if now > deadline:
                raise Unavailable(
                    "本轮执行超时", code="CHAT_EXECUTION_TIMEOUT"
                )
            if now - last_cancel_check > 0.5:
                last_cancel_check = now
                await _assert_not_cancelled(session_factory, lease)
            if kind == "delta":
                if segment_id is None:
                    segment_id = uuid.uuid4().hex
                    await _emit(
                        session_factory,
                        lease,
                        "response.started",
                        {"response_segment_id": segment_id},
                        response_segment_id=segment_id,
                    )
                delta_buffer.append(data["text"])
                await flush()
            elif kind == "message" and data.get("role") == "assistant":
                await flush(force=True)
                calls = data.get("tool_calls") or []
                recorder.begin_response(data.get("message_id"), calls)
                if calls:
                    await _project_assistant_tool_calls(
                        session_factory,
                        ctx,
                        content=data.get("content") or "",
                        tool_calls=calls,
                        message_id=data.get("message_id"),
                        finish_reason=data.get("finish_reason"),
                    )
                    segment_id = None
                else:
                    final = data
            # Tool messages are projected by the durable recorder.
        await flush(force=True)

    if final is None and saver is not None and state is not None:
        latest = await agent.aget_state(config)
        final = _last_final_message((latest.values or {}).get("messages") or [])
    if final is None or not (final.get("content") or "").strip():
        await _finish(
            session_factory,
            lease,
            "failed",
            "CHAT_EMPTY_RESPONSE",
            "模型未产生可用的最终回答",
        )
        return
    await _complete(
        session_factory,
        lease,
        final["content"],
        final.get("message_id") or "final",
    )


def _find_exception(exc, types):
    """LangGraph may wrap a tool middleware exception in an ExceptionGroup."""

    if isinstance(exc, types):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for child in exc.exceptions:
            found = _find_exception(child, types)
            if found is not None:
                return found
    return None


async def execute_run(
    lease: RunLease,
    *,
    session_factory=None,
    agent_builder=None,
    checkpointer_factory=None,
    recorder_factory=None,
) -> None:
    """Execute one claimed Run. The caller has already committed the lease claim."""

    session_factory = session_factory or AsyncSessionFactory
    agent_builder = agent_builder or _build_agent
    checkpointer_factory = checkpointer_factory or _open_checkpointer
    recorder_factory = recorder_factory or DurableToolRecorder

    try:
        ctx = await _load_context(session_factory, lease)
        if ctx.run.checkpoint_schema_version != CHECKPOINT_SCHEMA_VERSION:
            raise Unavailable(
                "检查点版本不兼容，已停止自动恢复",
                code="CHECKPOINT_VERSION_UNSUPPORTED",
            )
        ctx.lease = lease
        recorder = recorder_factory(session_factory, lease=lease)
        async with checkpointer_factory() as saver:
            agent = agent_builder(ctx, recorder, saver)
            await _execute(
                ctx,
                lease=lease,
                recorder=recorder,
                agent=agent,
                saver=saver,
                session_factory=session_factory,
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        cancelled = _find_exception(exc, RunCancelled)
        if cancelled is not None:
            await _finish(
                session_factory,
                lease,
                "cancelled",
                "CHAT_CANCELLED_BY_USER",
                "用户取消",
            )
            return
        attention = _find_exception(exc, ToolRequiresAttention)
        if attention is not None:
            await _requires_attention(
                session_factory, lease, attention.code, "工具结果未知，需要人工核对"
            )
            return
        lost = _find_exception(exc, ExecutionLost)
        if lost is not None:
            logger.warning(
                "chat run stopped; execution ownership lost",
                extra={"run_id": lease.run_id},
            )
            return
        logger.exception(
            "chat run failed",
            extra={"run_id": lease.run_id, "error_type": type(exc).__name__},
        )
        await _finish(
            session_factory,
            lease,
            "failed",
            getattr(exc, "code", "CHAT_EXECUTION_FAILED"),
            getattr(exc, "message", type(exc).__name__),
        )
