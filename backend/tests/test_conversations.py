"""Conversations CRUD (API_CONTRACT.md §5.2).

Gotcha: "another user's conversation" is created by calling the service
directly with a different user_id, not through the API — the MVP auth stub
(app/api/v1/deps.py) resolves every Bearer token to the same dev user, so
there is no way to *authenticate* as a second user yet.
"""

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.schemas.conversation import Conversation, ConversationCreate
from app.services import conversations as service

BASE = "/api/v1/conversations"


def _create_conversation_for_user(user_id: str) -> Conversation:
    # WHY a throwaway engine run inside its own asyncio.run(), instead of
    # reusing app.db.session's module-level engine: that engine's connection
    # pool is already bound to the TestClient fixture's event loop (it runs
    # the ASGI app via an anyio portal in a separate loop). asyncpg
    # connections are loop-bound — reusing the shared pool from a second
    # loop here raises "attached to a different loop". A short-lived engine,
    # created and disposed within one `asyncio.run()`, never touches it.
    async def _run() -> Conversation:
        engine = create_async_engine(settings.database_url)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as db:
            result = await service.create_conversation(db, user_id, ConversationCreate())
        await engine.dispose()
        return result

    return asyncio.run(_run())


def test_create_conversation_returns_201(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.post(
        BASE,
        json={"title": "hello", "system_prompt": "Be concise."},
        headers=auth_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "hello"
    assert body["system_prompt"] == "Be concise."
    assert body["message_count"] == 0
    assert body["id"].startswith("conv_")


def test_create_conversation_rejects_unknown_field(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(BASE, json={"nope": "nope"}, headers=auth_headers)

    assert response.status_code == 422


def test_create_conversation_without_auth_header_is_401(client: TestClient) -> None:
    response = client.post(BASE, json={})

    assert response.status_code == 401


def test_get_nonexistent_conversation_is_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get(f"{BASE}/conv_doesnotexist", headers=auth_headers)

    assert response.status_code == 404


def test_get_other_users_conversation_is_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    other = _create_conversation_for_user("some_other_user")

    response = client.get(f"{BASE}/{other.id}", headers=auth_headers)

    assert response.status_code == 404


def test_list_conversations_paginates_with_cursor(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    created_ids = [
        client.post(BASE, json={"title": f"t{i}"}, headers=auth_headers).json()["id"]
        for i in range(3)
    ]
    # WHY assert exact IDs, not just counts: the DB is shared across tests
    # (no per-test isolation fixture exists in this repo yet, per BUILD_LOG),
    # so other tests may have left conversations for the same dev user
    # behind. IDs are chronologically sortable (core/ids.py) and default
    # order is desc (newest first), so the 3 conversations just created here
    # are necessarily the most recent for this user regardless of what else
    # is in the table.
    newest_first = list(reversed(created_ids))

    first_page = client.get(f"{BASE}?limit=2", headers=auth_headers).json()
    assert [row["id"] for row in first_page["data"]] == newest_first[:2]
    assert first_page["pagination"]["has_more"] is True
    cursor = first_page["pagination"]["next_cursor"]
    assert cursor is not None

    second_page = client.get(f"{BASE}?limit=2&cursor={cursor}", headers=auth_headers).json()
    assert second_page["data"][0]["id"] == newest_first[2]


def test_patch_partial_update_leaves_untouched_fields_alone(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    created = client.post(
        BASE,
        json={"title": "original", "system_prompt": "original prompt"},
        headers=auth_headers,
    ).json()

    response = client.patch(
        f"{BASE}/{created['id']}", json={"title": "updated"}, headers=auth_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "updated"
    assert body["system_prompt"] == "original prompt"


def test_patch_explicit_null_clears_field(client: TestClient, auth_headers: dict[str, str]) -> None:
    created = client.post(
        BASE, json={"system_prompt": "original prompt"}, headers=auth_headers
    ).json()

    response = client.patch(
        f"{BASE}/{created['id']}", json={"system_prompt": None}, headers=auth_headers
    )

    assert response.status_code == 200
    assert response.json()["system_prompt"] is None


def test_delete_then_get_is_404(client: TestClient, auth_headers: dict[str, str]) -> None:
    created = client.post(BASE, json={}, headers=auth_headers).json()

    delete_response = client.delete(f"{BASE}/{created['id']}", headers=auth_headers)
    assert delete_response.status_code == 204

    get_response = client.get(f"{BASE}/{created['id']}", headers=auth_headers)
    assert get_response.status_code == 404
