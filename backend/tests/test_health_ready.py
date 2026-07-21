"""GET /health/ready — readiness probe (API_CONTRACT.md §5.1).

Gotcha: exercises a real DB connection, not a mock — needs postgres reachable
at DATABASE_URL. See conftest.py.
"""

from fastapi.testclient import TestClient


def test_health_ready_returns_200_when_db_reachable(client: TestClient) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"


def test_health_ready_reports_registry_check(client: TestClient) -> None:
    response = client.get("/health/ready")

    assert response.json()["checks"]["registry"] == "ok"
