"""Shared test fixtures.

Role: seeds default env vars (DATABASE_URL, SECRET_KEY,
ENABLE_LIVE_MODEL_REFRESH) before app.main (and therefore app.config) is
imported, and provides a lifespan-aware test client plus a real
registered-and-logged-in user for authenticated-endpoint tests.
Called by: pytest, auto-discovered. Calls nothing internal beyond app.main.
Gotcha: test_health_ready hits a real postgres (ARCHITECTURE.md's "Integration"
tier, not mocked) — `make dev`'s postgres, or CI's service container, must be
reachable at DATABASE_URL before running `make test`.
Gotcha: ENABLE_LIVE_MODEL_REFRESH defaults to false here regardless of what a
developer's real .env sets — app.core.llm.registry's live refresh makes real
HTTP calls to every configured provider, and `make test` must stay
deterministic and network-free even when real provider keys are sitting in
.env for manual smoke testing (this repo's own established convention, per
the adapter files' "verified live during implementation" comments).
"""

import os
import uuid
from dataclasses import dataclass

# WHY setdefault before the app.main import below: app/config.py constructs
# `settings = Settings()` at module import time, so these env vars must exist
# before that import runs, not before any test body runs.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://agentos:agentos@localhost:5432/agentos")
os.environ.setdefault("ENABLE_LIVE_MODEL_REFRESH", "false")
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")

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


@dataclass
class AuthedUser:
    """A real, freshly registered-and-logged-in user, for tests that need
    the raw user_id/email (not just headers) — e.g. to mutate its row
    directly to exercise the usage-limit path."""

    headers: dict[str, str]
    user_id: str
    email: str


@pytest.fixture
def authed_user(client: TestClient) -> AuthedUser:
    """Registers a brand-new user via the real API and logs in for a JWT.

    WHY a fresh email per call (not a fixed literal): this repo's tests
    share one persistent Postgres with no per-test isolation (see
    ROADMAP.md's testing gaps) — a fixed email would collide with a
    previous test run's already-registered account.
    """
    email = f"user-{uuid.uuid4()}@example.com"
    password = "a-fine-password"  # noqa: S105 -- test fixture, not a real secret
    register_response = client.post(
        "/api/v1/auth/register", json={"email": email, "password": password}
    )
    assert register_response.status_code == 201, register_response.text
    user_id = register_response.json()["id"]

    # WHY form-encoded `data=`, not `json=`: /auth/login is
    # OAuth2PasswordRequestForm (a well-known OAuth2 convention, not this
    # repo's usual JSON convention) — the field is named `username` even
    # though it holds the email. See app/api/v1/auth.py's module docstring.
    login_response = client.post(
        "/api/v1/auth/login", data={"username": email, "password": password}
    )
    assert login_response.status_code == 200, login_response.text
    token = login_response.json()["access_token"]

    return AuthedUser(headers={"Authorization": f"Bearer {token}"}, user_id=user_id, email=email)


@pytest.fixture
def auth_headers(authed_user: AuthedUser) -> dict[str, str]:
    """Headers for a real, freshly registered-and-logged-in user.

    Thin wrapper over `authed_user` — kept as its own fixture since most
    tests only need the headers, not the user_id/email.
    """
    return authed_user.headers
