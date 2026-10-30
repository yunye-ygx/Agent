import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from test_auth import csrf_headers, register

from app.api.errors import install_business_error_handler
from app.api.routes.auth import router as auth_router
from app.chat.models import ChatOutboxEvent, Message, Run, Turn
from app.db.mysql_session import get_mysql_session


@asynccontextmanager
async def client_for(db, monkeypatch):
    from app.api.routes import chat as chat_routes
    from app.api.routes.chat import router

    @asynccontextmanager
    async def factory():
        yield db

    async def session():
        yield db

    monkeypatch.setattr(chat_routes, "AsyncSessionFactory", factory)
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(router)
    install_business_error_handler(app)
    app.dependency_overrides[get_mysql_session] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield client


async def _conversation(client) -> str:
    response = await client.post(
        "/api/v1/chat/conversations", headers=csrf_headers(client)
    )
    assert response.status_code == 201
    return response.json()["id"]


@pytest.mark.asyncio
async def test_submit_returns_202_and_is_idempotent(business_db, monkeypatch):
    async with client_for(business_db, monkeypatch) as client:
        assert (await client.get("/api/v1/chat/conversations")).status_code == 401
        await register(client)
        assert (await client.post("/api/v1/chat/conversations")).status_code == 403
        conversation_id = await _conversation(client)
        endpoint = f"/api/v1/chat/conversations/{conversation_id}/messages"

        missing = await client.post(
            endpoint, json={"content": "查订单"}, headers=csrf_headers(client)
        )
        assert missing.status_code == 422

        first = await client.post(
            endpoint,
            json={"content": "查订单"},
            headers={**csrf_headers(client), "Idempotency-Key": "submit-1"},
        )
        assert first.status_code == 202
        body = first.json()
        assert body["status"] == "queued"
        assert body["turn_id"] and body["run_id"]
        assert body["events_url"] == f"/api/v1/chat/runs/{body['run_id']}/events"

        repeated = await client.post(
            endpoint,
            json={"content": "不同正文但同一个幂等键"},
            headers={**csrf_headers(client), "Idempotency-Key": "submit-1"},
        )
        assert repeated.status_code == 202
        assert repeated.json()["turn_id"] == body["turn_id"]
        assert repeated.json()["run_id"] == body["run_id"]

        busy = await client.post(
            endpoint,
            json={"content": "第二条"},
            headers={**csrf_headers(client), "Idempotency-Key": "submit-2"},
        )
        assert busy.status_code == 409
        assert busy.json()["detail"]["code"] == "CHAT_BUSY"

        run = await client.get(f"/api/v1/chat/runs/{body['run_id']}")
        assert run.status_code == 200
        assert run.json()["can_cancel"] is True
        assert run.json()["can_retry"] is False

        turns = await client.get(f"/api/v1/chat/conversations/{conversation_id}/turns")
        assert turns.status_code == 200
        assert turns.json()["items"][0]["turn_id"] == body["turn_id"]

        messages = await client.get(endpoint)
        assert [item["role"] for item in messages.json()["items"]] == ["user"]
        assert messages.json()["items"][0]["run_id"] is None

        outbox = list(
            (
                await business_db.execute(
                    select(ChatOutboxEvent).where(
                        ChatOutboxEvent.aggregate_id == body["run_id"]
                    )
                )
            ).scalars()
        )
        assert len(outbox) == 1 and outbox[0].status == "pending"


@pytest.mark.asyncio
async def test_run_ownership_cancel_and_retry(business_db, monkeypatch):
    async with client_for(business_db, monkeypatch) as client:
        await register(client)
        conversation_id = await _conversation(client)
        endpoint = f"/api/v1/chat/conversations/{conversation_id}/messages"
        submitted = await client.post(
            endpoint,
            json={"content": "查订单"},
            headers={**csrf_headers(client), "Idempotency-Key": "cancel-1"},
        )
        body = submitted.json()

        cancelled = await client.post(
            f"/api/v1/chat/runs/{body['run_id']}/cancel",
            headers=csrf_headers(client),
        )
        assert cancelled.status_code == 202
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["can_retry"] is True

        conversation = (await client.get("/api/v1/chat/conversations")).json()["items"][0]
        assert conversation["busy"] is False

        retried = await client.post(
            f"/api/v1/chat/turns/{body['turn_id']}/runs",
            json={"trigger_reason": "cancel_retry"},
            headers=csrf_headers(client),
        )
        assert retried.status_code == 202
        retried_body = retried.json()
        assert retried_body["turn_id"] == body["turn_id"]
        assert retried_body["attempt_no"] == 2
        assert retried_body["status"] == "queued"

        runs = await client.get(f"/api/v1/chat/turns/{body['turn_id']}/runs")
        assert [item["attempt_no"] for item in runs.json()["items"]] == [1, 2]

        await register(client, username="bob")
        assert (await client.get(f"/api/v1/chat/runs/{body['run_id']}")).status_code == 404
        assert (
            await client.post(
                f"/api/v1/chat/runs/{body['run_id']}/cancel",
                headers=csrf_headers(client),
            )
        ).status_code == 404
        assert (
            await client.post(
                f"/api/v1/chat/turns/{body['turn_id']}/runs",
                json={"trigger_reason": "cancel_retry"},
                headers=csrf_headers(client),
            )
        ).status_code == 404


@pytest.mark.asyncio
async def test_sse_replays_events_after_last_event_id(business_db, monkeypatch):
    from app.chat import lifecycle

    async with client_for(business_db, monkeypatch) as client:
        await register(client)
        conversation_id = await _conversation(client)
        submitted = await client.post(
            f"/api/v1/chat/conversations/{conversation_id}/messages",
            json={"content": "查订单"},
            headers={**csrf_headers(client), "Idempotency-Key": "sse-1"},
        )
        body = submitted.json()
        run = await lifecycle.claim_run(
            business_db,
            body["run_id"],
            worker_id="worker-a",
            dispatch_generation=0,
            lease_seconds=60,
        )
        await lifecycle.finish_run(
            business_db,
            body["run_id"],
            worker_id="worker-a",
            lease_generation=run.lease_generation,
            status="failed",
            error_code="CHAT_EXECUTION_FAILED",
            error_message="boom",
        )

        response = await client.get(f"/api/v1/chat/runs/{body['run_id']}/events")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "id: 1\nevent: run.queued" in response.text
        assert "event: run.started" in response.text
        assert "event: run.failed" in response.text
        assert response.text.count("event: run.failed") == 1

        resumed = await client.get(
            f"/api/v1/chat/runs/{body['run_id']}/events",
            headers={"Last-Event-ID": "2"},
        )
        assert resumed.status_code == 200
        assert "event: run.queued" not in resumed.text
        assert "event: run.started" not in resumed.text
        assert "event: run.failed" in resumed.text

        await register(client, username="bob")
        assert (
            await client.get(f"/api/v1/chat/runs/{body['run_id']}/events")
        ).status_code == 404


@pytest.mark.asyncio
async def test_messages_and_turn_rows_are_written_in_one_submit(business_db, monkeypatch):
    async with client_for(business_db, monkeypatch) as client:
        await register(client)
        conversation_id = await _conversation(client)
        body = (
            await client.post(
                f"/api/v1/chat/conversations/{conversation_id}/messages",
                json={"content": "  查一下\n我的订单  "},
                headers={**csrf_headers(client), "Idempotency-Key": "rows-1"},
            )
        ).json()
        turn = await business_db.get(Turn, body["turn_id"])
        run = await business_db.get(Run, body["run_id"])
        messages = list(
            (
                await business_db.execute(
                    select(Message).where(Message.turn_id == body["turn_id"])
                )
            ).scalars()
        )
        assert turn.user_message_id == messages[0].id
        assert messages[0].content == "查一下\n我的订单"
        assert run.checkpoint_thread_id == run.id
        assert turn.latest_run_id == run.id == turn.active_run_id


@pytest.mark.asyncio
async def test_two_sse_observers_replay_the_same_events_without_mutating_run(
    business_db, monkeypatch
):
    from app.chat import lifecycle

    async with client_for(business_db, monkeypatch) as client:
        await register(client)
        conversation_id = await _conversation(client)
        body = (
            await client.post(
                f"/api/v1/chat/conversations/{conversation_id}/messages",
                json={"content": "查订单"},
                headers={**csrf_headers(client), "Idempotency-Key": "two-viewers"},
            )
        ).json()
        run = await lifecycle.claim_run(
            business_db,
            body["run_id"],
            worker_id="worker-a",
            dispatch_generation=0,
            lease_seconds=60,
        )
        await lifecycle.finish_run(
            business_db,
            body["run_id"],
            worker_id="worker-a",
            lease_generation=run.lease_generation,
            status="completed",
        )

        first, second = await asyncio.gather(
            client.get(f"/api/v1/chat/runs/{body['run_id']}/events"),
            client.get(f"/api/v1/chat/runs/{body['run_id']}/events"),
        )
        assert first.status_code == second.status_code == 200
        assert first.text == second.text
        assert "event: run.completed" in first.text

        status = (await client.get(f"/api/v1/chat/runs/{body['run_id']}")).json()
        assert status["status"] == "completed"


@pytest.mark.asyncio
async def test_edit_and_resend_creates_a_new_turn_after_completion(
    business_db, monkeypatch
):
    from app.chat import lifecycle

    async with client_for(business_db, monkeypatch) as client:
        await register(client)
        conversation_id = await _conversation(client)
        endpoint = f"/api/v1/chat/conversations/{conversation_id}/messages"
        first = (
            await client.post(
                endpoint,
                json={"content": "第一版问题"},
                headers={**csrf_headers(client), "Idempotency-Key": "edit-1"},
            )
        ).json()
        run = await lifecycle.claim_run(
            business_db,
            first["run_id"],
            worker_id="worker-a",
            dispatch_generation=0,
            lease_seconds=60,
        )
        await lifecycle.finish_run(
            business_db,
            first["run_id"],
            worker_id="worker-a",
            lease_generation=run.lease_generation,
            status="completed",
        )

        second = (
            await client.post(
                endpoint,
                json={"content": "编辑后的新问题"},
                headers={**csrf_headers(client), "Idempotency-Key": "edit-2"},
            )
        ).json()
        assert second["turn_id"] != first["turn_id"]
        assert second["status"] == "queued"

        turns = (
            await client.get(f"/api/v1/chat/conversations/{conversation_id}/turns")
        ).json()["items"]
        assert [item["turn_no"] for item in turns] == [2, 1]
        assert turns[0]["latest_run"]["run_id"] == second["run_id"]

        messages = (await client.get(endpoint)).json()["items"]
        assert [item["content"] for item in messages] == ["第一版问题", "编辑后的新问题"]