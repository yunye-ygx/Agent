"""Test-only worker process: real RabbitMQ/MySQL, fake model, no API server."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.chat.agent import build_agent
from app.chat.executor import execute_run
from app.chat.worker import ChatRunProcessor, run_chat_worker


class FakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@asynccontextmanager
async def memory_saver():
    yield InMemorySaver()


async def execute_with_fake_model(self, lease):
    model = FakeModel(responses=[AIMessage(content="worker-only answer")])

    def builder(ctx, recorder, saver):
        return build_agent(model, ctx.principal, specs=[], checkpointer=saver)

    await execute_run(
        lease,
        agent_builder=builder,
        checkpointer_factory=memory_saver,
    )


ChatRunProcessor.execute = execute_with_fake_model


if __name__ == "__main__":
    asyncio.run(run_chat_worker())