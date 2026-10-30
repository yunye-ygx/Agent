from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from pydantic import Field
from sqlalchemy import select

from app.chat.contracts import RunStatus


class SessionFactory:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def __call__(self):
        yield self.db


class ToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _fake_checkpointer():
    @asynccontextmanager
    async def factory():
        yield None

    return factory


async def _prepare(db, content="查一下我的订单", key="exec-request"):
    from app.business.models import BusinessUser
    from app.chat import lifecycle, store
    from app.chat.contracts import RunLease
    from app.db.mysql_models import ModelConfiguration

    if await db.get(BusinessUser, "alice") is None:
        db.add(
            BusinessUser(
                id="alice",
                username="alice",
                display_name="Alice",
                password_hash="test",
                role="customer",
                status="active",
            )
        )
        await db.commit()
    if (
        await db.execute(
            select(ModelConfiguration).where(ModelConfiguration.is_default.is_(True))
        )
    ).first() is None:
        db.add(
            ModelConfiguration(
                provider_key="openai",
                provider_name="Test",
                display_name="Test",
                base_url="http://127.0.0.1:1/v1",
                api_key="test",
                chat_model="test-model",
                is_default=True,
            )
        )
        await db.commit()
    conversation = await store.create_conversation(db, "alice")
    submission = await store.create_turn_and_initial_run(
        db,
        "alice",
        conversation.id,
        content,
        client_request_id=key,
    )
    run = await lifecycle.claim_run(
        db,
        submission.run_id,
        worker_id="worker-a",
        dispatch_generation=0,
        lease_seconds=60,
    )
    lease = RunLease(
        run_id=run.id,
        turn_id=run.turn_id,
        worker_id="worker-a",
        lease_generation=run.lease_generation,
    )
    return conversation, submission, lease


@pytest.mark.asyncio
async def test_execute_run_projects_final_answer_and_events(knowledge_db, monkeypatch):
    from app.chat import store
    from app.chat.executor import execute_run
    from app.chat.models import Conversation, Message, RunEvent, Turn

    monkeypatch.setattr(
        "app.chat.executor.settings.chat_heartbeat_interval_seconds", 3600
    )
    conversation, submission, lease = await _prepare(knowledge_db, key="final")

    model = ToolModel(responses=[AIMessage(content="这是最终回答")])

    def builder(ctx, recorder, saver):
        from app.chat.agent import build_agent

        return build_agent(model, ctx.principal, specs=[], checkpointer=saver)

    await execute_run(
        lease,
        session_factory=SessionFactory(knowledge_db),
        agent_builder=builder,
        checkpointer_factory=_fake_checkpointer(),
    )

    run = await knowledge_db.get(store.Run, submission.run_id)
    assert run.status == RunStatus.COMPLETED
    turn = await knowledge_db.get(Turn, submission.turn_id)
    assert turn.status == "completed"
    refreshed = await store.get_conversation(knowledge_db, "alice", conversation.id)
    assert refreshed.active_turn is None
    messages = list(
        (
            await knowledge_db.execute(
                select(Message)
                .where(Message.turn_id == submission.turn_id)
                .order_by(Message.sequence)
            )
        ).scalars()
    )
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[-1].content == "这是最终回答"
    events = list(
        (
            await knowledge_db.execute(
                select(RunEvent)
                .where(RunEvent.run_id == submission.run_id)
                .order_by(RunEvent.seq)
            )
        ).scalars()
    )
    types = [event.event_type for event in events]
    assert types[0] == "run.queued" and types[1] == "run.started"
    assert "response.completed" in types
    assert types[-1] == "run.completed"
    assert (await knowledge_db.get(Conversation, conversation.id)).active_turn is None


@pytest.mark.asyncio
async def test_execute_run_reuses_and_orders_tool_results(knowledge_db, monkeypatch):
    from app.chat import store
    from app.chat.executor import execute_run
    from app.chat.models import Message, ToolExecution
    from app.tool_runtime import ToolArguments, ToolSpec

    monkeypatch.setattr(
        "app.chat.executor.settings.chat_heartbeat_interval_seconds", 3600
    )
    conversation, submission, lease = await _prepare(knowledge_db, key="tool")

    class Args(ToolArguments):
        page: int = Field(default=1, ge=1)

    executed = []

    async def list_orders(args, context):
        executed.append(args.page)
        return {"items": [{"order_no": "SO-1"}], "total": 1}

    spec = ToolSpec(
        name="list_orders",
        arguments_model=Args,
        handler=list_orders,
        permission="orders:read",
        authorize=lambda args, context: True,
        description="查看订单",
    )
    model = ToolModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"id": "c1", "name": "list_orders", "args": {"page": 1}}],
            ),
            AIMessage(content="你有一笔订单 SO-1"),
        ]
    )

    def builder(ctx, recorder, saver):
        from app.chat.agent import build_agent

        return build_agent(
            model,
            ctx.principal,
            specs=[spec],
            checkpointer=saver,
            tool_recorder=recorder,
        )

    await execute_run(
        lease,
        session_factory=SessionFactory(knowledge_db),
        agent_builder=builder,
        checkpointer_factory=_fake_checkpointer(),
    )

    run = await knowledge_db.get(store.Run, submission.run_id)
    assert run.status == RunStatus.COMPLETED
    assert executed == [1]
    rows = list(
        (
            await knowledge_db.execute(
                select(Message)
                .where(Message.turn_id == submission.turn_id)
                .order_by(Message.sequence)
            )
        ).scalars()
    )
    assert [row.role for row in rows] == ["user", "assistant", "tool", "assistant"]
    assert rows[1].tool_calls[0]["id"] == rows[2].tool_call_id == "c1"
    executions = list(
        (
            await knowledge_db.execute(
                select(ToolExecution).where(ToolExecution.run_id == submission.run_id)
            )
        ).scalars()
    )
    assert len(executions) == 1
    assert executions[0].status == "succeeded"
    assert executions[0].result_json["status"] == "success"
    assert (
        await store.get_conversation(knowledge_db, "alice", conversation.id)
    ).active_turn is None


@pytest.mark.asyncio
async def test_checkpoint_version_mismatch_fails_without_building_agent(
    knowledge_db, monkeypatch
):
    from app.chat.executor import execute_run
    from app.chat.models import Run

    monkeypatch.setattr(
        "app.chat.executor.settings.chat_heartbeat_interval_seconds", 3600
    )
    _, submission, lease = await _prepare(knowledge_db, key="version")
    run = await knowledge_db.get(Run, submission.run_id)
    run.checkpoint_schema_version = "v0"
    await knowledge_db.commit()

    built = {"value": False}

    def builder(ctx, recorder, saver):
        built["value"] = True
        raise AssertionError("an incompatible checkpoint must not build an agent")

    await execute_run(
        lease,
        session_factory=SessionFactory(knowledge_db),
        agent_builder=builder,
        checkpointer_factory=_fake_checkpointer(),
    )

    run = await knowledge_db.get(Run, submission.run_id)
    await knowledge_db.refresh(run)
    assert built["value"] is False
    assert run.status == "failed"
    assert run.error_code == "CHECKPOINT_VERSION_UNSUPPORTED"


@pytest.mark.asyncio
async def test_closing_the_event_stream_does_not_stop_execution(knowledge_db, monkeypatch):
    from app.api.routes import chat as chat_routes
    from app.chat.executor import execute_run
    from app.chat.models import Run

    monkeypatch.setattr(
        "app.chat.executor.settings.chat_heartbeat_interval_seconds", 3600
    )
    monkeypatch.setattr(
        chat_routes, "AsyncSessionFactory", SessionFactory(knowledge_db)
    )
    _, submission, lease = await _prepare(knowledge_db, key="observer-close")

    stream = chat_routes._event_stream(submission.run_id, 0)
    first = await anext(stream)
    assert first.startswith("retry:")
    await stream.aclose()

    model = ToolModel(responses=[AIMessage(content="执行不受观察连接影响")])

    def builder(ctx, recorder, saver):
        from app.chat.agent import build_agent

        return build_agent(model, ctx.principal, specs=[], checkpointer=saver)

    await execute_run(
        lease,
        session_factory=SessionFactory(knowledge_db),
        agent_builder=builder,
        checkpointer_factory=_fake_checkpointer(),
    )
    run = await knowledge_db.get(Run, submission.run_id)
    await knowledge_db.refresh(run)
    assert run.status == RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_executor_marks_run_requires_attention_on_tool_unknown(
    knowledge_db, monkeypatch
):
    from app.chat.contracts import ToolRequiresAttention
    from app.chat.executor import execute_run
    from app.chat.models import Run, Turn

    monkeypatch.setattr(
        "app.chat.executor.settings.chat_heartbeat_interval_seconds", 3600
    )
    conversation, submission, lease = await _prepare(knowledge_db, key="attention-exec")

    class AttentionAgent:
        async def aget_state(self, config):
            return None

        async def astream(self, *args, **kwargs):
            raise ToolRequiresAttention("tool side effect is unknown")
            yield  # pragma: no cover

    def builder(ctx, recorder, saver):
        return AttentionAgent()

    await execute_run(
        lease,
        session_factory=SessionFactory(knowledge_db),
        agent_builder=builder,
        checkpointer_factory=_fake_checkpointer(),
    )
    run = await knowledge_db.get(Run, submission.run_id)
    turn = await knowledge_db.get(Turn, submission.turn_id)
    await knowledge_db.refresh(run)
    await knowledge_db.refresh(turn)
    assert run.status == "requires_attention"
    assert run.error_code == "CHAT_TOOL_REQUIRES_ATTENTION"
    assert turn.status == "requires_attention"
    assert turn.active_run_id is None
    stored = await knowledge_db.get(
        type(conversation), conversation.id
    )
    assert stored.active_turn is None


@pytest.mark.asyncio
async def test_lease_heartbeat_stops_execution_after_any_renewal_failure(monkeypatch):
    from app.chat import lifecycle
    from app.chat.contracts import ExecutionLost, RunLease
    from app.chat.executor import LeaseHeartbeat

    async def unavailable(*args, **kwargs):
        raise RuntimeError("database connection lost")

    monkeypatch.setattr(lifecycle, "renew_run_lease", unavailable)
    lease = RunLease("run-1", "turn-1", "worker-a", 1)

    async with LeaseHeartbeat(
        session_factory=object(),
        lease=lease,
        interval_seconds=0.001,
        lease_seconds=60,
    ) as heartbeat:
        await asyncio.sleep(0.02)
        with pytest.raises(ExecutionLost, match="lease renewal"):
            heartbeat.check()


@pytest.mark.asyncio
async def test_inflight_tool_from_crashed_worker_requires_attention(knowledge_db):
    from app.chat.contracts import ToolRequiresAttention
    from app.chat.models import ToolExecution
    from app.chat.tool_execution import DurableToolRecorder
    from app.tool_runtime import ExecutionContext

    _conversation, submission, lease = await _prepare(knowledge_db, key="inflight-tool")
    knowledge_db.add(
        ToolExecution(
            run_id=submission.run_id,
            model_response_id="response-1",
            tool_batch_id="response-1",
            tool_call_id="call-1",
            call_index=0,
            tool_name="write_ticket",
            tool_version="v1",
            arguments_json={"subject": "printer"},
            arguments_hash="a" * 64,
            status="running",
            attempt_count=1,
            max_attempts=3,
        )
    )
    await knowledge_db.commit()
    recorder = DurableToolRecorder(SessionFactory(knowledge_db), lease=lease)

    with pytest.raises(ToolRequiresAttention, match="may have completed"):
        await recorder.before(
            {"id": "call-1", "name": "write_ticket", "args": {"subject": "printer"}},
            ExecutionContext(user_id="alice", permissions=frozenset()),
        )

    execution = (
        await knowledge_db.execute(
            select(ToolExecution).where(ToolExecution.id.is_not(None))
        )
    ).scalar_one()
    assert execution.status == "requires_attention"
    assert execution.error_code == "TOOL_RESULT_UNKNOWN"
