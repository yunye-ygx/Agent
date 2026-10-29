import json

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import Field

from app.business.principal import Principal
from app.tool_runtime import ToolArguments, ToolSpec


class ToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class Args(ToolArguments):
    page: int = Field(default=1, ge=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments,expected", [({"page": 1}, "success"), ({"user_id": "victim"}, "rejected")]
)
async def test_framework_routes_tool_through_runtime_with_trusted_identity(arguments, expected):
    from app.chat.agent import build_agent, stream_agent

    identities = []

    async def handler(args, context):
        identities.append(context.user_id)
        return {"total": 2}

    spec = ToolSpec(
        name="list_orders",
        arguments_model=Args,
        handler=handler,
        permission="orders:read",
        authorize=lambda a, c: True,
        description="查询本人订单",
    )
    principal = Principal.for_user(
        user_id="alice", username="alice", display_name="Alice", role="customer"
    )
    model = ToolModel(
        responses=[
            AIMessage(
                content="", tool_calls=[{"id": "c1", "name": "list_orders", "args": arguments}]
            ),
            AIMessage(content="回答完成"),
        ]
    )
    agent = build_agent(model, principal, specs=[spec])
    events = [event async for event in stream_agent(agent, [HumanMessage(content="查订单")])]
    messages = [data for kind, data in events if kind == "message"]
    assert [m["role"] for m in messages] == ["assistant", "tool", "assistant"]
    assert json.loads(messages[1]["content"])["status"] == expected
    assert identities == (["alice"] if expected == "success" else [])
    assert messages[1]["tool_call_id"] == "c1"


def test_database_history_restores_protocol_messages():
    from types import SimpleNamespace

    from app.chat.agent import restore_messages

    rows = [
        SimpleNamespace(
            role="assistant",
            content="",
            tool_calls=[{"id": "c1", "name": "list_orders", "args": {}}],
        ),
        SimpleNamespace(
            role="tool", content='{"total": 2}', tool_call_id="c1", tool_name="list_orders"
        ),
    ]
    messages = restore_messages(rows)
    assert messages[0].tool_calls[0]["id"] == messages[1].tool_call_id
