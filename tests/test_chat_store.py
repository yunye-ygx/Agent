import pytest
from sqlalchemy import select

from app.business.errors import Conflict, NotFound


@pytest.mark.asyncio
async def test_chat_round_is_ordered_and_only_complete_round_enters_history(business_db):
    from app.chat import store

    conversation = await store.create_conversation(business_db, "alice")
    turn = await store.begin_turn(business_db, "alice", conversation.id, "查我的订单")
    await store.append_message(
        business_db,
        conversation.id,
        turn,
        "assistant",
        "",
        tool_calls=[{"id": "call-1", "name": "list_orders", "args": {}}],
    )
    await store.append_message(
        business_db,
        conversation.id,
        turn,
        "tool",
        '{"total": 2}',
        tool_call_id="call-1",
        tool_name="list_orders",
    )
    await store.append_message(business_db, conversation.id, turn, "assistant", "共两笔订单")
    assert await store.history(business_db, conversation.id) == []
    await store.finish_turn(business_db, conversation.id, turn, succeeded=True)
    rows = await store.list_messages(business_db, "alice", conversation.id)
    assert [row.sequence for row in rows] == [1, 2, 3, 4]
    assert [row.role for row in rows] == ["user", "assistant", "tool", "assistant"]
    assert len(await store.history(business_db, conversation.id)) == 4
    assert rows[2].tool_call_id == "call-1"


@pytest.mark.asyncio
async def test_ownership_and_overlapping_sends_are_enforced(business_db):
    from app.chat import store

    conv = await store.create_conversation(business_db, "alice")
    with pytest.raises(NotFound):
        await store.list_messages(business_db, "bob", conv.id)
    with pytest.raises(NotFound):
        await store.begin_turn(business_db, "bob", conv.id, "偷看")
    turn = await store.begin_turn(business_db, "alice", conv.id, "hello")
    with pytest.raises(Conflict):
        await store.begin_turn(business_db, "alice", conv.id, "second")
    await store.finish_turn(business_db, conv.id, turn, succeeded=False)
    assert await store.history(business_db, conv.id) == []
    assert (await store.list_messages(business_db, "alice", conv.id))[0].status == "failed"
    await store.begin_turn(business_db, "alice", conv.id, "retry")


@pytest.mark.asyncio
async def test_late_writer_cannot_append_after_turn_finished(business_db):
    from app.chat import store

    conv = await store.create_conversation(business_db, "alice")
    turn = await store.begin_turn(business_db, "alice", conv.id, "hello")
    await store.finish_turn(business_db, conv.id, turn, succeeded=False)
    with pytest.raises(Conflict):
        await store.append_message(business_db, conv.id, turn, "assistant", "late")


@pytest.mark.asyncio
async def test_history_never_splits_a_tool_round(business_db):
    from app.chat import store

    conv = await store.create_conversation(business_db, "alice")
    for text in ["first", "second", "third"]:
        turn = await store.begin_turn(business_db, "alice", conv.id, text)
        await store.append_message(business_db, conv.id, turn, "assistant", text)
        await store.finish_turn(business_db, conv.id, turn, succeeded=True)
    rows = await store.history(business_db, conv.id, max_turns=2)
    assert [r.content for r in rows] == ["second", "second", "third", "third"]
    assert len(await store.list_messages(business_db, "alice", conv.id, before=5, limit=2)) == 2


@pytest.mark.asyncio
async def test_create_turn_and_initial_run_is_atomic_and_idempotent(business_db):
    from app.chat import store
    from app.chat.models import ChatOutboxEvent, Run, RunEvent, Turn

    conversation = await store.create_conversation(business_db, "alice")
    first = await store.create_turn_and_initial_run(
        business_db,
        "alice",
        conversation.id,
        "  查一下\n我的订单  ",
        client_request_id="request-1",
    )
    repeated = await store.create_turn_and_initial_run(
        business_db,
        "alice",
        conversation.id,
        "不会重复写入",
        client_request_id="request-1",
    )

    assert repeated == first
    turn = await business_db.get(Turn, first.turn_id)
    run = await business_db.get(Run, first.run_id)
    assert turn.turn_no == 1
    assert turn.latest_run_id == run.id == turn.active_run_id
    assert run.status == "queued" and run.attempt_no == 1
    assert run.checkpoint_thread_id == run.id
    assert (await business_db.get(store.Message, turn.user_message_id)).run_id is None
    assert (
        await business_db.execute(select(RunEvent).where(RunEvent.run_id == run.id))
    ).scalar_one().event_type == "run.queued"
    assert (
        await business_db.execute(select(ChatOutboxEvent).where(ChatOutboxEvent.aggregate_id == run.id))
    ).scalar_one().deduplication_key == f"run:{run.id}:created"

    refreshed = await store.get_conversation(business_db, "alice", conversation.id)
    assert refreshed.active_turn == turn.id
    assert refreshed.title == "查一下 我的订单"
    with pytest.raises(Conflict):
        await store.create_turn_and_initial_run(
            business_db,
            "alice",
            conversation.id,
            "第二条并发消息",
            client_request_id="request-2",
        )


@pytest.mark.asyncio
async def test_legacy_messages_without_turns_remain_readable(business_db):
    from app.chat import store
    from app.chat.models import Conversation, Message

    conversation = Conversation(user_id="alice", title="旧会话")
    business_db.add(conversation)
    await business_db.flush()
    business_db.add(
        Message(
            conversation_id=conversation.id,
            sequence=1,
            turn_id="legacy-turn",
            role="user",
            content="旧问题",
            status="complete",
        )
    )
    business_db.add(
        Message(
            conversation_id=conversation.id,
            sequence=2,
            turn_id="legacy-turn",
            role="assistant",
            content="旧回答",
            status="complete",
        )
    )
    await business_db.commit()

    rows = await store.history_rows(business_db, conversation.id)
    assert [(row.role, row.content) for row in rows] == [
        ("user", "旧问题"),
        ("assistant", "旧回答"),
    ]
    messages = await store.list_messages(business_db, "alice", conversation.id)
    assert [row.content for row in messages] == ["旧问题", "旧回答"]


@pytest.mark.asyncio
async def test_messages_show_only_the_selected_run_of_a_turn(business_db):
    from app.chat import lifecycle, store
    from app.chat.models import Run, Turn

    conversation = await store.create_conversation(business_db, "alice")
    first = await store.create_turn_and_initial_run(
        business_db,
        "alice",
        conversation.id,
        "问题",
        client_request_id="selected-run-1",
    )
    first_run = await lifecycle.claim_run(
        business_db,
        first.run_id,
        worker_id="worker-a",
        dispatch_generation=0,
        lease_seconds=60,
    )
    turn = await business_db.get(Turn, first.turn_id)
    await store.project_run_message(
        business_db,
        run=first_run,
        turn=turn,
        role="assistant",
        content="第一版回答",
        source_key="final:first",
    )
    await business_db.commit()
    await lifecycle.finish_run(
        business_db,
        first.run_id,
        worker_id="worker-a",
        lease_generation=first_run.lease_generation,
        status="completed",
    )

    second = await store.create_retry_run(
        business_db, "alice", first.turn_id, trigger_reason="regenerate"
    )
    second_run = await lifecycle.claim_run(
        business_db,
        second.id,
        worker_id="worker-b",
        dispatch_generation=0,
        lease_seconds=60,
    )
    turn = await business_db.get(Turn, first.turn_id)
    await store.project_run_message(
        business_db,
        run=second_run,
        turn=turn,
        role="assistant",
        content="第二版回答",
        source_key="final:second",
    )
    await business_db.commit()
    await lifecycle.finish_run(
        business_db,
        second.id,
        worker_id="worker-b",
        lease_generation=second_run.lease_generation,
        status="completed",
    )

    messages = await store.list_messages(business_db, "alice", conversation.id)
    assert [row.content for row in messages] == ["问题", "第二版回答"]
    turn = await business_db.get(Turn, first.turn_id)
    assert turn.latest_run_id == second.id
    assert (await business_db.get(Run, first.run_id)).status == "completed"
