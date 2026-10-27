from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

from app.api.routes import tool_lab
from app.auth.dependencies import get_current_principal, require_csrf
from app.business.principal import Principal
from app.tool_runtime import Observation

ORIGIN = "http://testserver"


def alice() -> Principal:
    return Principal.for_user(
        user_id="alice-id", username="alice", display_name="爱丽丝", role="customer"
    )


@asynccontextmanager
async def client_for(principal=None, *, csrf=True):
    app = FastAPI()
    app.include_router(tool_lab.router)
    if principal is not None:
        app.dependency_overrides[get_current_principal] = lambda: principal
        if csrf:
            app.dependency_overrides[require_csrf] = lambda: principal
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        yield client


@pytest.mark.asyncio
async def test_tool_directory_requires_login_and_shows_server_identity():
    async with client_for(None) as client:
        assert (await client.get("/api/v1/tool-lab/tools")).status_code == 401
    async with client_for(alice()) as client:
        response = await client.get("/api/v1/tool-lab/tools")
    assert response.status_code == 200
    payload = response.json()
    assert payload["identity"]["user_id"] == "alice-id"
    assert payload["identity"]["role"] == "customer"
    assert len(payload["tools"]) == 8
    assert {item["name"] for item in payload["tools"]} >= {
        "list_orders",
        "create_ticket",
        "search_knowledge",
    }


@pytest.mark.asyncio
async def test_execute_uses_backend_identity_and_rejects_override_fields(monkeypatch):
    captured = {}

    class FakeRuntime:
        async def execute(self, call, context):
            captured["call"] = call
            captured["context"] = context
            return Observation(
                status="success",
                code="OK",
                message="ok",
                call_id=call.call_id,
                tool=call.name,
                attempts=1,
                data={"rows": 0},
                next_action="continue",
            )

    monkeypatch.setattr(tool_lab.catalog, "runtime", FakeRuntime())
    async with client_for(alice()) as client:
        forbidden = await client.post(
            "/api/v1/tool-lab/execute",
            json={
                "call_id": "call-1",
                "name": "list_orders",
                "arguments": {"user_id": "forged", "page": 1},
                "user_id": "forged",
            },
            headers={"Origin": ORIGIN, "X-CSRF-Token": "x"},
        )
        assert forbidden.status_code == 422
        response = await client.post(
            "/api/v1/tool-lab/execute",
            json={
                "call_id": "call-1",
                "name": "list_orders",
                "arguments": {"page": 1, "page_size": 20},
            },
            headers={"Origin": ORIGIN, "X-CSRF-Token": "x"},
        )
    assert response.status_code == 200
    assert response.json()["observation"]["status"] == "success"
    assert response.json()["attempts"] == 1
    assert captured["context"].user_id == "alice-id"
    assert "admin:manage" not in captured["context"].permissions


@pytest.mark.asyncio
async def test_execute_requires_csrf_dependency():
    async with client_for(alice(), csrf=False) as client:
        response = await client.post(
            "/api/v1/tool-lab/execute",
            json={"call_id": "call-1", "name": "list_orders", "arguments": {}},
            headers={"Origin": ORIGIN, "X-CSRF-Token": "x"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_create_ticket_without_operation_is_rejected_before_any_database_work():
    async with client_for(alice()) as client:
        response = await client.post(
            "/api/v1/tool-lab/execute",
            json={
                "call_id": "call-1",
                "name": "create_ticket",
                "arguments": {
                    "serial_no": "SN-X",
                    "subject": "问题",
                    "description": "描述",
                },
            },
            headers={"Origin": ORIGIN, "X-CSRF-Token": "x"},
        )
    payload = response.json()["observation"]
    assert payload["status"] == "rejected"
    assert payload["code"] == "OPERATION_ID_REQUIRED"
    assert payload["attempts"] == 0
