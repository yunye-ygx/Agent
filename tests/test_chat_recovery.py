from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select


class SessionFactory:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def __call__(self):
        yield self.db


@pytest.mark.asyncio
async def test_succeeded_tool_is_reused_and_pending_tools_resume_in_order(business_db):
    from app.chat import lifecycle, store
    from app.chat.models import ToolExecution
    from app.chat.tool_execution import execute_tool_batch, prepare_tool_batch

    conversation = await store.create_conversation(business_db, "alice")
    submission = await store.create_turn_and_initial_run(
        business_db,
        "alice",
        conversation.id,
        "执行三个查询",
        client_request_id="tool-recovery",
    )
    lease = await lifecycle.claim_run(
        business_db,
        submission.run_id,
        worker_id="worker-a",
        dispatch_generation=0,
        lease_seconds=60,
    )
    calls = [
        {"id": "A", "name": "tool_a", "args": {"value": 1}, "version": "v1"},
        {"id": "B", "name": "tool_b", "args": {"value": 2}, "version": "v1"},
        {"id": "C", "name": "tool_c", "args": {"value": 3}, "version": "v1"},
    ]
    await prepare_tool_batch(
        business_db,
        run_id=lease.id,
        worker_id="worker-a",
        lease_generation=lease.lease_generation,
        model_response_id="response-1",
        calls=calls,
    )
    first = (
        await business_db.execute(select(ToolExecution).where(ToolExecution.tool_call_id == "A"))
    ).scalar_one()
    first.status = "succeeded"
    first.result_json = {"status": "success", "data": {"value": "A"}}
    await business_db.commit()

    executed = []

    async def runner(call):
        executed.append(call["id"])
        return {"status": "success", "data": {"value": call["id"]}}

    observations = await execute_tool_batch(
        business_db,
        run_id=lease.id,
        worker_id="worker-a",
        lease_generation=lease.lease_generation,
        model_response_id="response-1",
        calls=calls,
        runner=runner,
    )

    assert executed == ["B", "C"]
    assert [item["data"]["value"] for item in observations] == ["A", "B", "C"]
    rows = list(
        (
            await business_db.execute(
                select(ToolExecution).order_by(ToolExecution.call_index)
            )
        ).scalars()
    )
    assert [row.status for row in rows] == ["succeeded", "succeeded", "succeeded"]
    assert [row.attempt_count for row in rows] == [0, 1, 1]


@pytest.mark.asyncio
async def test_scanner_recovery_completes_without_any_http_request(knowledge_db, monkeypatch):
    from datetime import timedelta

    from sqlalchemy import update
    from test_chat_executor import ToolModel, _fake_checkpointer, _prepare

    from app.business.utils import utc_now
    from app.chat import lifecycle
    from app.chat.contracts import RunLease
    from app.chat.executor import execute_run
    from app.chat.models import Run
    from app.chat.recovery import scan_once

    monkeypatch.setattr(
        "app.chat.executor.settings.chat_heartbeat_interval_seconds", 3600
    )
    conversation, submission, _first_lease = await _prepare(
        knowledge_db, key="scanner-exec"
    )
    await knowledge_db.execute(
        update(Run)
        .where(Run.id == submission.run_id)
        .values(lease_expires_at=utc_now() - timedelta(seconds=1))
    )
    await knowledge_db.commit()

    assert await scan_once(SessionFactory(knowledge_db)) == 1
    run = await knowledge_db.get(Run, submission.run_id)
    await knowledge_db.refresh(run)
    assert run.status == "recovering" and run.recovery_count == 1

    resumed = await lifecycle.claim_run(
        knowledge_db,
        submission.run_id,
        worker_id="worker-b",
        dispatch_generation=1,
        lease_seconds=60,
    )
    lease = RunLease(
        run_id=resumed.id,
        turn_id=resumed.turn_id,
        worker_id="worker-b",
        lease_generation=resumed.lease_generation,
    )
    from langchain_core.messages import AIMessage

    model = ToolModel(responses=[AIMessage(content="恢复后完成的回答")])

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
    assert run.status == "completed"
    assert (
        await knowledge_db.get(type(conversation), conversation.id)
    ).active_turn is None

