from __future__ import annotations

import os
from typing import TypedDict

import pytest
from sqlalchemy import func, select


class RecoveryState(TypedDict, total=False):
    completed_nodes: list[str]


@pytest.mark.asyncio
async def test_postgres_checkpoint_survives_saver_recreation() -> None:
    postgres_url = os.getenv("TEST_CHECKPOINT_POSTGRES_URL")
    if not postgres_url:
        pytest.skip("set TEST_CHECKPOINT_POSTGRES_URL to run PostgreSQL recovery verification")

    from langgraph.graph import END, START, StateGraph

    from app.chat.checkpoints import open_postgres_checkpointer

    first_node_calls = 0
    second_node_calls = 0

    async def first_node(state: RecoveryState) -> RecoveryState:
        nonlocal first_node_calls
        first_node_calls += 1
        return {"completed_nodes": [*state.get("completed_nodes", []), "first"]}

    async def second_node(state: RecoveryState) -> RecoveryState:
        nonlocal second_node_calls
        second_node_calls += 1
        if second_node_calls == 1:
            raise RuntimeError("simulated worker crash")
        return {"completed_nodes": [*state.get("completed_nodes", []), "second"]}

    builder = StateGraph(RecoveryState)
    builder.add_node("first", first_node)
    builder.add_node("second", second_node)
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", END)
    thread_id = "checkpoint-recovery-test"
    config = {"configurable": {"thread_id": thread_id}}

    async with open_postgres_checkpointer(postgres_url, setup=True) as saver:
        graph = builder.compile(checkpointer=saver)
        with pytest.raises(RuntimeError, match="simulated worker crash"):
            await graph.ainvoke({"completed_nodes": []}, config)

    async with open_postgres_checkpointer(postgres_url) as recreated_saver:
        checkpoint = await recreated_saver.aget_tuple(config)
        assert checkpoint is not None
        assert checkpoint.checkpoint["channel_values"]["completed_nodes"] == ["first"]
        assert checkpoint.pending_writes

        graph = builder.compile(checkpointer=recreated_saver)
        result = await graph.ainvoke(None, config)

    assert result["completed_nodes"] == ["first", "second"]
    assert first_node_calls == 1
    assert second_node_calls == 2


@pytest.mark.asyncio
async def test_final_checkpoint_projection_is_idempotent(business_db) -> None:
    from app.chat import lifecycle, store
    from app.chat.executor import project_final_response
    from app.chat.models import Conversation, Message, Run, RunEvent, Turn

    conversation = await store.create_conversation(business_db, "alice")
    submission = await store.create_turn_and_initial_run(
        business_db,
        "alice",
        conversation.id,
        "给出最终回答",
        client_request_id="final-projection",
    )
    lease = await lifecycle.claim_run(
        business_db,
        submission.run_id,
        worker_id="worker-a",
        dispatch_generation=0,
        lease_seconds=60,
    )

    first = await project_final_response(
        business_db,
        run_id=lease.id,
        worker_id="worker-a",
        lease_generation=lease.lease_generation,
        model_response_id="response-final",
        content="这是权威完整回答。",
    )
    second = await project_final_response(
        business_db,
        run_id=lease.id,
        worker_id="worker-a",
        lease_generation=lease.lease_generation,
        model_response_id="response-final",
        content="这是权威完整回答。",
    )

    assert second.id == first.id
    assert (
        await business_db.execute(
            select(func.count(Message.id)).where(Message.run_id == lease.id)
        )
    ).scalar_one() == 1
    events = list(
        (
            await business_db.execute(
                select(RunEvent).where(RunEvent.run_id == lease.id).order_by(RunEvent.seq)
            )
        ).scalars()
    )
    assert [event.event_type for event in events].count("response.completed") == 1
    assert [event.event_type for event in events].count("run.completed") == 1
    assert next(
        event.payload_json["content"]
        for event in events
        if event.event_type == "response.completed"
    ) == "这是权威完整回答。"
    assert (await business_db.get(Run, lease.id)).status == "completed"
    assert (await business_db.get(Turn, lease.turn_id)).status == "completed"
    assert (await business_db.get(Conversation, conversation.id)).active_turn is None


@pytest.mark.asyncio
async def test_abc_tool_batch_resumes_after_worker_crash(knowledge_db, monkeypatch) -> None:
    postgres_url = os.getenv("TEST_CHECKPOINT_POSTGRES_URL")
    if not postgres_url:
        pytest.skip("set TEST_CHECKPOINT_POSTGRES_URL to run the A/B/C checkpoint recovery test")

    from contextlib import asynccontextmanager
    from datetime import timedelta

    from langchain_core.messages import AIMessage
    from sqlalchemy import update
    from test_chat_executor import SessionFactory, ToolModel, _prepare

    from app.business.utils import utc_now
    from app.chat import lifecycle
    from app.chat.agent import build_agent
    from app.chat.checkpoints import open_postgres_checkpointer
    from app.chat.contracts import RunLease
    from app.chat.executor import _execute, _load_context, execute_run
    from app.chat.models import Run, ToolExecution
    from app.chat.recovery import scan_once
    from app.chat.tool_execution import DurableToolRecorder
    from app.tool_runtime import ToolArguments, ToolSpec

    monkeypatch.setattr(
        "app.chat.executor.settings.chat_heartbeat_interval_seconds", 3600
    )
    conversation, submission, first_lease = await _prepare(
        knowledge_db, key="abc-checkpoint-resume"
    )
    calls = {"tool_a": 0, "tool_b": 0, "tool_c": 0}

    class Args(ToolArguments):
        pass

    async def tool_a(args, context):
        calls["tool_a"] += 1
        return {"name": "A"}

    async def tool_b(args, context):
        calls["tool_b"] += 1
        return {"name": "B"}

    async def tool_c(args, context):
        calls["tool_c"] += 1
        return {"name": "C"}

    specs = [
        ToolSpec(
            name=name,
            arguments_model=Args,
            handler=handler,
            permission="orders:read",
            authorize=lambda args, context: True,
            description=f"测试工具 {name}",
        )
        for name, handler in (
            ("tool_a", tool_a),
            ("tool_b", tool_b),
            ("tool_c", tool_c),
        )
    ]
    model = ToolModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"id": "call-a", "name": "tool_a", "args": {}}],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "call-b", "name": "tool_b", "args": {}},
                    {"id": "call-c", "name": "tool_c", "args": {}},
                ],
            ),
            AIMessage(content="A/B/C 恢复后的最终回答"),
        ]
    )
    failed_once: set[str] = set()

    class FailOnceOnFirstRound:
        def __init__(self, inner):
            self.inner = inner

        def begin_response(self, model_response_id, calls_):
            return self.inner.begin_response(model_response_id, calls_)

        async def before(self, call, context):
            if call["name"] in {"tool_b", "tool_c"} and call["name"] not in failed_once:
                failed_once.add(call["name"])
                raise RuntimeError("simulated worker crash before side effect")
            return await self.inner.before(call, context)

        async def after(self, call, context, observation):
            return await self.inner.after(call, context, observation)

    async with open_postgres_checkpointer(postgres_url, setup=True) as saver:
        ctx = await _load_context(SessionFactory(knowledge_db), first_lease)
        ctx.lease = first_lease
        first_recorder = FailOnceOnFirstRound(
            DurableToolRecorder(SessionFactory(knowledge_db), lease=first_lease)
        )
        first_agent = build_agent(
            model,
            ctx.principal,
            specs=specs,
            checkpointer=saver,
            tool_recorder=first_recorder,
        )
        with pytest.raises(RuntimeError):
            await _execute(
                ctx,
                lease=first_lease,
                recorder=first_recorder,
                agent=first_agent,
                saver=saver,
                session_factory=SessionFactory(knowledge_db),
            )

        run = await knowledge_db.get(Run, submission.run_id)
        await knowledge_db.refresh(run)
        assert run.status == "running"
        assert calls == {"tool_a": 1, "tool_b": 0, "tool_c": 0}

        await knowledge_db.execute(
            update(Run)
            .where(Run.id == submission.run_id)
            .values(lease_expires_at=utc_now() - timedelta(seconds=1))
        )
        await knowledge_db.commit()
        assert await scan_once(SessionFactory(knowledge_db)) == 1
        resumed = await lifecycle.claim_run(
            knowledge_db,
            submission.run_id,
            worker_id="worker-b",
            dispatch_generation=1,
            lease_seconds=60,
        )
        second_lease = RunLease(
            run_id=resumed.id,
            turn_id=resumed.turn_id,
            worker_id="worker-b",
            lease_generation=resumed.lease_generation,
        )

        @asynccontextmanager
        async def reuse_saver():
            yield saver

        def second_builder(ctx_, recorder, _saver):
            return build_agent(
                model,
                ctx_.principal,
                specs=specs,
                checkpointer=_saver,
                tool_recorder=recorder,
            )

        def recorder_factory(session_factory, *, lease):
            return FailOnceOnFirstRound(
                DurableToolRecorder(session_factory, lease=lease)
            )

        await execute_run(
            second_lease,
            session_factory=SessionFactory(knowledge_db),
            agent_builder=second_builder,
            checkpointer_factory=reuse_saver,
            recorder_factory=recorder_factory,
        )
        await saver.adelete_thread(submission.run_id)

    run = await knowledge_db.get(Run, submission.run_id)
    await knowledge_db.refresh(run)
    assert run.status == "completed"
    assert calls == {"tool_a": 1, "tool_b": 1, "tool_c": 1}
    executions = list(
        (
            await knowledge_db.execute(
                select(ToolExecution).where(ToolExecution.run_id == submission.run_id)
            )
        ).scalars()
    )
    assert len(executions) == 3
    assert all(row.status == "succeeded" for row in executions)
    assert all(row.attempt_count == 1 for row in executions)
    assert (
        await knowledge_db.get(type(conversation), conversation.id)
    ).active_turn is None


@pytest.mark.asyncio
async def test_completed_checkpoint_without_mysql_projection_is_recovered(
    knowledge_db, monkeypatch
) -> None:
    postgres_url = os.getenv("TEST_CHECKPOINT_POSTGRES_URL")
    if not postgres_url:
        pytest.skip("set TEST_CHECKPOINT_POSTGRES_URL to run the projection recovery test")

    from contextlib import asynccontextmanager

    from langchain_core.messages import AIMessage, HumanMessage
    from test_chat_executor import SessionFactory, ToolModel, _prepare

    from app.chat.agent import build_agent
    from app.chat.checkpoints import open_postgres_checkpointer
    from app.chat.executor import _load_context, execute_run
    from app.chat.models import Message, Run

    monkeypatch.setattr(
        "app.chat.executor.settings.chat_heartbeat_interval_seconds", 3600
    )
    _, submission, lease = await _prepare(knowledge_db, key="checkpoint-projection")
    ctx = await _load_context(SessionFactory(knowledge_db), lease)
    ctx.lease = lease
    model = ToolModel(responses=[AIMessage(content="只在 checkpoint 中的回答")])

    async with open_postgres_checkpointer(postgres_url, setup=True) as saver:
        agent = build_agent(model, ctx.principal, specs=[], checkpointer=saver)
        config = {
            "configurable": {"thread_id": ctx.run.checkpoint_thread_id},
            "recursion_limit": 24,
        }
        await agent.ainvoke(
            {"messages": [HumanMessage(content=ctx.user_message.content)]},
            config=config,
        )
        rows = list(
            (
                await knowledge_db.execute(
                    select(Message).where(Message.turn_id == submission.turn_id)
                )
            ).scalars()
        )
        assert [row.role for row in rows] == ["user"]

        @asynccontextmanager
        async def reuse_saver():
            yield saver

        def builder(ctx_, recorder, _saver):
            return build_agent(
                model, ctx_.principal, specs=[], checkpointer=_saver
            )

        await execute_run(
            lease,
            session_factory=SessionFactory(knowledge_db),
            agent_builder=builder,
            checkpointer_factory=reuse_saver,
        )
        await saver.adelete_thread(submission.run_id)

    rows = list(
        (
            await knowledge_db.execute(
                select(Message)
                .where(Message.turn_id == submission.turn_id)
                .order_by(Message.sequence)
            )
        ).scalars()
    )
    assert [row.role for row in rows] == ["user", "assistant"]
    assert rows[-1].content == "只在 checkpoint 中的回答"
    run = await knowledge_db.get(Run, submission.run_id)
    await knowledge_db.refresh(run)
    assert run.status == "completed"

