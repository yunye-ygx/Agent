from fastapi.testclient import TestClient

from app import main


def test_health_does_not_require_database() -> None:
    client = TestClient(main.app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readiness_reports_database_failure(monkeypatch) -> None:
    async def unavailable() -> bool:
        return False

    monkeypatch.setattr(main, "check_database", unavailable)
    client = TestClient(main.app)
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["business"]["ready"] is False


def test_readiness_ready_keeps_rag_status_separate(monkeypatch) -> None:
    async def available() -> bool:
        return True

    monkeypatch.setattr(main, "check_database", available)
    client = TestClient(main.app)
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["business"]["ready"] is True
    assert "rag" in response.json()
