"""GET /health — liveness probe (API_CONTRACT.md §5.1)."""

from fastapi.testclient import TestClient


def test_health_returns_200_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
