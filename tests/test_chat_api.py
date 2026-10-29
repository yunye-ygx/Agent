import json
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from test_auth import csrf_headers, register

from app.api.errors import install_business_error_handler
from app.api.routes.auth import router as auth_router
from app.chat.models import Message
from app.db.mysql_session import get_mysql_session


@asynccontextmanager
async def client_for(db, monkeypatch, *, fail=False):
    from app.api.routes.chat import router
    from app.chat import service

    @asynccontextmanager
    async def factory():
        yield db

    async def session():
        yield db

    async def prepare(*args):
        return object(), []

    async def events(agent, history):
        yield "delta", {"text": "真实"}
        if fail:
            raise RuntimeError("secret-provider-response")
        yield "delta", {"text": "回答"}
        yield "message", {"role": "assistant", "content": "真实回答"}

    monkeypatch.setattr(service, "AsyncSessionFactory", factory)
    monkeypatch.setattr(service, "prepare", prepare)
    monkeypatch.setattr(service, "agent_events", events)
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(router)
    install_business_error_handler(app)
    app.dependency_overrides[get_mysql_session] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_real_api_auth_ownership_and_persist_before_done(business_db, monkeypatch):
    async with client_for(business_db, monkeypatch) as client:
        assert (await client.get("/api/v1/chat/conversations")).status_code == 401
        await register(client)
        assert (await client.post("/api/v1/chat/conversations")).status_code == 403
        response = await client.post("/api/v1/chat/conversations", headers=csrf_headers(client))
        assert response.status_code == 201
        cid = response.json()["id"]
        endpoint = f"/api/v1/chat/conversations/{cid}/messages"
        bad = await client.post(
            endpoint, json={"content": "hi", "user_id": "bob"}, headers=csrf_headers(client)
        )
        assert bad.status_code == 422
        response = await client.post(
            endpoint, json={"content": "查订单"}, headers=csrf_headers(client)
        )
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        assert "event: delta" in response.text
        assert "event: done" in response.text
        messages = (await client.get(endpoint)).json()["items"]
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert all(m["status"] == "complete" for m in messages)
        assert messages[1]["content"] == "真实回答"
        await register(client, username="bob")
        assert (await client.get(endpoint)).status_code == 404
        assert (
            await client.post(endpoint, json={"content": "steal"}, headers=csrf_headers(client))
        ).status_code == 404
        assert (await client.get("/api/v1/chat/conversations")).json()["items"] == []


@pytest.mark.asyncio
async def test_stream_failure_is_not_a_successful_assistant_message(business_db, monkeypatch):
    async with client_for(business_db, monkeypatch, fail=True) as client:
        await register(client)
        cid = (
            await client.post("/api/v1/chat/conversations", headers=csrf_headers(client))
        ).json()["id"]
        response = await client.post(
            f"/api/v1/chat/conversations/{cid}/messages",
            json={"content": "hello"},
            headers=csrf_headers(client),
        )
        assert "event: error" in response.text
        assert "event: done" not in response.text
        assert "secret-provider-response" not in response.text
        rows = list((await business_db.execute(select(Message))).scalars())
        assert len(rows) == 1 and rows[0].status == "failed"
        assert json.loads(response.text.split("event: error\ndata: ")[1])["code"] == "CHAT_FAILED"
