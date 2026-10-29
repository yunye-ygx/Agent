import asyncio
import json
import logging

from sqlalchemy import select

from app.business.errors import Unavailable
from app.chat import store
from app.db.mysql_models import ModelConfiguration
from app.db.mysql_session import AsyncSessionFactory

logger = logging.getLogger(__name__)


def message_payload(row):
    return {
        "id": row.id,
        "sequence": row.sequence,
        "turn_id": row.turn_id,
        "role": row.role,
        "content": row.content,
        "tool_calls": row.tool_calls,
        "tool_call_id": row.tool_call_id,
        "tool_name": row.tool_name,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
    }


def conversation_payload(row):
    return {
        "id": row.id,
        "title": row.title,
        "busy": row.active_turn is not None,
        "updated_at": row.updated_at.isoformat(),
    }


async def prepare(db, principal, conversation_id):
    try:
        from app.chat.agent import build_agent, model_from_configuration, restore_messages
    except ImportError as exc:
        raise Unavailable(
            "聊天依赖未安装，请安装项目 agent 依赖", code="CHAT_DEPENDENCY_MISSING"
        ) from exc
    configs = list(
        (
            await db.execute(
                select(ModelConfiguration).where(ModelConfiguration.is_default.is_(True))
            )
        ).scalars()
    )
    if len(configs) != 1:
        raise Unavailable("请管理员设置一个默认聊天模型", code="CHAT_MODEL_MISSING")
    model = model_from_configuration(configs[0])
    history = restore_messages(await store.history(db, conversation_id))
    return build_agent(model, principal), history


async def agent_events(agent, history):
    from app.chat.agent import stream_agent

    async for event in stream_agent(agent, history):
        yield event


def sse(kind, data):
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_reply(conversation_id, turn_id, content, agent, history):
    from langchain_core.messages import HumanMessage

    succeeded = False
    pending = set()
    final_answer = False
    try:
        yield sse("start", {"conversation_id": conversation_id, "turn_id": turn_id})
        async for kind, data in agent_events(agent, [*history, HumanMessage(content=content)]):
            if kind == "message":
                if data["role"] == "assistant":
                    if pending:
                        raise ValueError("Missing tool results")
                    calls = data.get("tool_calls") or []
                    ids = [call["id"] for call in calls]
                    if len(set(ids)) != len(ids):
                        raise ValueError("Duplicate tool call ids")
                    pending.update(ids)
                    final_answer = not calls and bool(data["content"].strip())
                elif data["role"] == "tool":
                    pending.remove(data["tool_call_id"])
                async with AsyncSessionFactory() as db:
                    row = await store.append_message(db, conversation_id, turn_id, **data)
                    data = message_payload(row)
            yield sse(kind, data)
        if pending or not final_answer:
            raise ValueError("Incomplete model turn")
        async with AsyncSessionFactory() as db:
            await store.finish_turn(db, conversation_id, turn_id, succeeded=True)
        succeeded = True
        yield sse("done", {"conversation_id": conversation_id, "turn_id": turn_id})
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Provider exception strings can contain credentials or response content.
        logger.warning(
            "Chat failed conversation=%s turn=%s error_type=%s",
            conversation_id,
            turn_id,
            type(exc).__name__,
        )
        yield sse(
            "error",
            {
                "code": "CHAT_FAILED",
                "message": "本轮回复未完成，请稍后重新发送。已保存的消息可在历史中查看。",
            },
        )
    finally:
        if not succeeded:
            try:
                async with AsyncSessionFactory() as db:
                    await store.finish_turn(db, conversation_id, turn_id, succeeded=False)
            except Exception:
                logger.warning(
                    "Chat cleanup incomplete conversation=%s turn=%s", conversation_id, turn_id
                )
