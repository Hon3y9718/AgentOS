"""Register/login/logout + the per-user token usage limit (API_CONTRACT.md §1).

Contract tier (ARCHITECTURE.md): real ASGI app + real Postgres, same as
test_conversations.py/test_messages.py — no provider call is ever reached by
the usage-limit test below, since app.services.chat._validate_and_resolve()
checks the quota before resolving the conversation or model (see its
docstring), so nothing needs respx mocking here.
"""

import asyncio
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.models.user import User as UserModel
from tests.conftest import AuthedUser

BASE = "/api/v1/auth"


def _set_token_limit(user_id: str, token_limit: int) -> None:
    async def _run() -> None:
        engine = create_async_engine(settings.database_url)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as db:
            await db.execute(
                update(UserModel).where(UserModel.id == user_id).values(token_limit=token_limit)
            )
            await db.commit()
        await engine.dispose()

    asyncio.run(_run())


def test_register_returns_201_with_usage_defaults(client: TestClient) -> None:
    email = f"user-{uuid.uuid4()}@example.com"
    response = client.post(BASE + "/register", json={"email": email, "password": "a-fine-password"})

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == email
    assert body["token_limit"] == 1_000_000
    assert body["tokens_used"] == 0
    assert "hashed_password" not in body


def test_register_duplicate_email_returns_409(client: TestClient) -> None:
    email = f"user-{uuid.uuid4()}@example.com"
    first = client.post(BASE + "/register", json={"email": email, "password": "a-fine-password"})
    second = client.post(BASE + "/register", json={"email": email, "password": "another-password"})

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["type"] == "conflict"


def test_login_returns_access_token(client: TestClient) -> None:
    email = f"user-{uuid.uuid4()}@example.com"
    client.post(BASE + "/register", json={"email": email, "password": "a-fine-password"})

    response = client.post(BASE + "/login", data={"username": email, "password": "a-fine-password"})

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert isinstance(body["access_token"], str) and body["access_token"]


def test_login_wrong_password_returns_401(client: TestClient) -> None:
    email = f"user-{uuid.uuid4()}@example.com"
    client.post(BASE + "/register", json={"email": email, "password": "a-fine-password"})

    response = client.post(BASE + "/login", data={"username": email, "password": "wrong-password"})

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "unauthenticated"


def test_protected_endpoint_without_token_returns_401(client: TestClient) -> None:
    response = client.get("/api/v1/conversations")

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "unauthenticated"


def test_chat_over_usage_limit_returns_402(client: TestClient, authed_user: AuthedUser) -> None:
    conversation_id = client.post(
        "/api/v1/conversations",
        json={"default_model": "anthropic:claude-sonnet-4-5"},
        headers=authed_user.headers,
    ).json()["id"]
    _set_token_limit(authed_user.user_id, token_limit=0)

    response = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": [{"type": "text", "text": "Hi"}]},
        headers={**authed_user.headers, "Idempotency-Key": str(uuid.uuid4())},
    )

    assert response.status_code == 402
    body = response.json()
    assert body["error"]["type"] == "usage_limit_exceeded"
    assert body["error"]["retryable"] is False
