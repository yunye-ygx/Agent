import pytest

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
