import asyncio

import pytest

from app import main


@pytest.mark.asyncio
async def test_local_lifespan_starts_and_stops_background_services(monkeypatch) -> None:
    started: set[str] = set()
    stop_events = {name: asyncio.Event() for name in ("publisher", "worker", "maintenance")}

    async def fake_initialize() -> dict:
        return {"business": {"ready": True}, "rag": {"ready": True, "components": {}}}

    async def fake_publisher() -> None:
        started.add("publisher")
        await stop_events["publisher"].wait()

    async def fake_worker() -> None:
        started.add("worker")
        await stop_events["worker"].wait()

    async def fake_maintenance() -> None:
        started.add("maintenance")
        await stop_events["maintenance"].wait()

    monkeypatch.setattr(main.settings, "app_env", "local")
    monkeypatch.setattr(main.settings, "run_background_services", True)
    monkeypatch.setattr(main, "initialize_infrastructure", fake_initialize)
    monkeypatch.setattr(main, "run_publisher", fake_publisher)
    monkeypatch.setattr(main, "run_worker", fake_worker)
    monkeypatch.setattr(main, "run_maintenance", fake_maintenance, raising=False)

    async with main.lifespan(main.app):
        await asyncio.sleep(0)
        assert started == {"publisher", "worker", "maintenance"}
        assert len(main.app.state.background_services) == 3

    assert main.app.state.background_services == ()


@pytest.mark.asyncio
async def test_rag_unavailable_keeps_business_and_skips_background_services(monkeypatch) -> None:
    started: set[str] = set()

    async def fake_initialize() -> dict:
        return {
            "business": {"ready": True},
            "rag": {
                "ready": False,
                "components": {"minio": False, "elasticsearch": False, "rabbitmq": False},
            },
        }

    async def fake_publisher() -> None:
        started.add("publisher")

    monkeypatch.setattr(main.settings, "app_env", "local")
    monkeypatch.setattr(main.settings, "run_background_services", True)
    monkeypatch.setattr(main, "initialize_infrastructure", fake_initialize)
    monkeypatch.setattr(main, "run_publisher", fake_publisher)
    main.app.state.background_services = ()

    async with main.lifespan(main.app):
        await asyncio.sleep(0)
        assert started == set()
        assert main.app.state.dependency_status["business"]["ready"] is True
        assert main.app.state.dependency_status["rag"]["ready"] is False
        assert main.app.state.background_services == ()
