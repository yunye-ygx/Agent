import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from test_a1_lifecycle import seed

from app.retrieval.access import local_owner
from app.retrieval.schemas import ExperimentRequest
from app.retrieval.scope import authorize_snapshot, resolve_scope


@pytest.mark.asyncio
async def test_http_experiment_roundtrip_and_owner_isolation(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from test_retrieval_experiments import MemorySnapshots, scope

    from app.api.routes import retrieval_experiments as routes

    @asynccontextmanager
    async def sessions():
        yield None

    fake_store = SimpleNamespace(client=AsyncMock())
    searcher = AsyncMock()
    searcher.search.return_value = []
    monkeypatch.setattr(routes, "AsyncSessionFactory", sessions)
    monkeypatch.setattr(routes, "resolve_scope", AsyncMock(return_value=scope()))
    monkeypatch.setattr(routes, "authorize_snapshot", AsyncMock())
    monkeypatch.setattr(
        routes,
        "resolve_clients",
        AsyncMock(
            return_value=(
                SimpleNamespace(embed_texts=AsyncMock(return_value=[[1.0, 0.0]])),
                None,
                None,
            )
        ),
    )
    monkeypatch.setattr(routes, "ChunkStore", lambda: fake_store)
    monkeypatch.setattr(routes, "HybridSearcher", lambda *a: searcher)
    monkeypatch.setattr(routes, "snapshots", MemorySnapshots())
    app = FastAPI()
    app.include_router(routes.router)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.post(
            "/api/v1/retrieval-experiments", json={"knowledge_base_id": "kb", "query": "where"}
        )
        assert response.status_code == 200
        report = response.json()
        identity = report["experiment_id"]
        assert report["status"] == "SUCCESS"
        again = await client.get("/api/v1/retrieval-experiments/" + identity)
        assert again.json() == report
        assert again.headers["cache-control"] == "no-store"
        fake_store.client.close.assert_awaited_once()
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as outsider:
        response = await outsider.get("/api/v1/retrieval-experiments/" + identity)
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_scope_only_uses_published_active_runs_and_no_secrets(db):
    doc, run, _ = await seed(db)
    doc.active_run_id, run.status = run.id, "SUCCESS"
    await db.commit()
    scope = await resolve_scope(db, ExperimentRequest(query="q", knowledge_base_id="kb"))
    assert scope["doc_runs"] == {doc.id: run.id}
    assert scope["embedding"]["dimension"] == 2
    assert "api_key" not in str(scope)
    doc.status = "DELETED"
    await db.commit()
    with pytest.raises(HTTPException) as error:
        await authorize_snapshot(db, scope)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_unpublished_run_cannot_be_searched(db):
    await seed(db)
    with pytest.raises(HTTPException) as error:
        await resolve_scope(db, ExperimentRequest(query="q", knowledge_base_id="kb"))
    assert error.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "peer,headers,allowed",
    [
        ("127.0.0.1", {}, True),
        ("10.0.0.2", {}, False),
        ("127.0.0.1", {"Origin": "https://evil.test"}, False),
        ("127.0.0.1", {"X-Forwarded-For": "127.0.0.1"}, False),
        ("127.0.0.1", {"Host": "evil.test"}, False),
    ],
)
async def test_access_checks_loopback_origin_and_proxy(peer, headers, allowed):
    app = FastAPI()

    @app.get("/api/v1/retrieval-experiments/session")
    async def session(owner=Depends(local_owner)):
        return {"owner": owner}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(peer, 12)), base_url="http://localhost"
    ) as client:
        response = await client.get("/api/v1/retrieval-experiments/session", headers=headers)
        assert response.status_code == (200 if allowed else 403)
        if allowed:
            again = await client.get("/api/v1/retrieval-experiments/session")
            assert response.json() == again.json()
            assert "HttpOnly" in response.headers["set-cookie"]
