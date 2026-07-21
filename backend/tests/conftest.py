"""Shared test fixtures.

Role: seeds a default DATABASE_URL before app.main (and therefore app.config)
is imported, and provides a lifespan-aware test client.
Called by: pytest, auto-discovered. Calls nothing internal beyond app.main.
Gotcha: test_health_ready hits a real postgres (ARCHITECTURE.md's "Integration"
tier, not mocked) — `make dev`'s postgres, or CI's service container, must be
reachable at DATABASE_URL before running `make test`.
"""

import os

# WHY setdefault before the app.main import below: app/config.py constructs
# `settings = Settings()` at module import time, so the env var must exist
# before that import runs, not before the test body runs.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://agentos:agentos@localhost:5432/agentos")

from collections.abc import Generator  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """Test client that runs the app's lifespan around each test.

    Why `with TestClient(...)`: outside the context manager, startup/shutdown
    events never fire, so anything depending on lifespan-configured state
    (today: logging config) would run against a half-initialized app.
    """
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Headers satisfying app.api.v1.deps.get_current_user's MVP Bearer check.

    The token value is never checked (API_CONTRACT §1) — any Bearer string
    resolves to the same fixed dev user, so every authenticated-endpoint test
    can share this fixture.
    """
    return {"Authorization": "Bearer test-token"}
