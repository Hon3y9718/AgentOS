"""Messages: cursor list + truncate-delete (API_CONTRACT.md §5.3).

Gotcha: there is no `create_message` service yet (that's the chat endpoint's
job, §5.4, not built) — messages are seeded directly as ORM rows via a
throwaway engine, same pattern test_conversations.py uses for
`_create_conversation_for_user`, and for the same reason (the shared
app.db.session engine is bound to TestClient's event loop).
"""

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.ids import new_id
from app.models.message import Message as MessageModel
from app.models.user import User as UserModel
from app.schemas.conversation import Conversation, ConversationCreate
from app.services import conversations as conversations_service

BASE = "/api/v1/conversations"


def _create_conversation_for_user(user_id: str) -> Conversation:
    # WHY ON CONFLICT DO NOTHING before creating the conversation: see the
    # identical comment in test_conversations.py's version of this helper —
    # conversations.user_id now FKs to users.id, and this literal user_id is
    # reused across tests sharing one persistent Postgres.
    async def _run() -> Conversation:
        engine = create_async_engine(settings.database_url)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as db:
            await db.execute(
                pg_insert(UserModel)
                .values(
                    id=user_id,
                    email=f"{user_id}@test.invalid",
                    hashed_password="unusable-test-placeholder-hash",
                    is_active=True,
                    is_superuser=False,
                    is_verified=True,
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await db.commit()
            result = await conversations_service.create_conversation(
                db, user_id, ConversationCreate()
            )
        await engine.dispose()
        return result

    return asyncio.run(_run())


def _seed_messages(conversation_id: str, contents: list[list[dict[str, object]]]) -> list[str]:
    """Insert one Message row per entry in `contents`, in order. Returns their ids."""

    async def _run() -> list[str]:
        engine = create_async_engine(settings.database_url)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        ids: list[str] = []
        async with session_factory() as db:
            for content in contents:
                message_id = new_id("msg")
                db.add(
                    MessageModel(
                        id=message_id,
                        conversation_id=conversation_id,
                        role="assistant",
                        content=content,
                        status="complete",
                    )
                )
                ids.append(message_id)
            await db.commit()
        await engine.dispose()
        return ids

    return asyncio.run(_run())


def _create_conversation(client: TestClient, auth_headers: dict[str, str]) -> str:
    response: str = client.post(BASE, json={}, headers=auth_headers).json()["id"]
    return response


_TEXT = [{"type": "text", "text": "hi"}]


def test_list_messages_returns_chronological_order(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    conversation_id = _create_conversation(client, auth_headers)
    ids = _seed_messages(conversation_id, [_TEXT, _TEXT, _TEXT])

    response = client.get(f"{BASE}/{conversation_id}/messages", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert [m["id"] for m in body["data"]] == ids
    assert body["pagination"] == {"next_cursor": None, "has_more": False, "limit": 20}


def test_list_messages_paginates_with_cursor(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    conversation_id = _create_conversation(client, auth_headers)
    ids = _seed_messages(conversation_id, [_TEXT, _TEXT, _TEXT])

    first_page = client.get(
        f"{BASE}/{conversation_id}/messages?limit=2", headers=auth_headers
    ).json()
    assert [m["id"] for m in first_page["data"]] == ids[:2]
    assert first_page["pagination"]["has_more"] is True
    cursor = first_page["pagination"]["next_cursor"]
    assert cursor is not None

    second_page = client.get(
        f"{BASE}/{conversation_id}/messages?limit=2&cursor={cursor}", headers=auth_headers
    ).json()
    assert [m["id"] for m in second_page["data"]] == ids[2:]
    assert second_page["pagination"]["has_more"] is False


def test_list_messages_desc_order(client: TestClient, auth_headers: dict[str, str]) -> None:
    conversation_id = _create_conversation(client, auth_headers)
    ids = _seed_messages(conversation_id, [_TEXT, _TEXT, _TEXT])

    response = client.get(f"{BASE}/{conversation_id}/messages?order=desc", headers=auth_headers)

    assert [m["id"] for m in response.json()["data"]] == list(reversed(ids))


def test_list_messages_omits_reasoning_by_default(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    conversation_id = _create_conversation(client, auth_headers)
    content = [
        {"type": "text", "text": "answer"},
        {"type": "reasoning", "text": "secret thoughts", "redacted": False},
    ]
    _seed_messages(conversation_id, [content])

    response = client.get(f"{BASE}/{conversation_id}/messages", headers=auth_headers)

    block_types = [b["type"] for b in response.json()["data"][0]["content"]]
    assert block_types == ["text"]


def test_list_messages_include_reasoning_true(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    conversation_id = _create_conversation(client, auth_headers)
    content = [
        {"type": "text", "text": "answer"},
        {"type": "reasoning", "text": "secret thoughts", "redacted": False},
    ]
    _seed_messages(conversation_id, [content])

    response = client.get(
        f"{BASE}/{conversation_id}/messages?include_reasoning=true", headers=auth_headers
    )

    block_types = [b["type"] for b in response.json()["data"][0]["content"]]
    assert block_types == ["text", "reasoning"]


def test_list_messages_unknown_conversation_is_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get(f"{BASE}/conv_doesnotexist/messages", headers=auth_headers)

    assert response.status_code == 404


def test_list_messages_other_users_conversation_is_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    other = _create_conversation_for_user("some_other_user")

    response = client.get(f"{BASE}/{other.id}/messages", headers=auth_headers)

    assert response.status_code == 404


def test_list_messages_without_auth_is_401(client: TestClient) -> None:
    response = client.get(f"{BASE}/conv_doesnotexist/messages")

    assert response.status_code == 401


def test_delete_message_truncates_everything_after(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    conversation_id = _create_conversation(client, auth_headers)
    ids = _seed_messages(conversation_id, [_TEXT, _TEXT, _TEXT, _TEXT])

    response = client.delete(f"{BASE}/{conversation_id}/messages/{ids[1]}", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["deleted_message_ids"] == ids[1:]
    assert body["count"] == 3

    remaining = client.get(f"{BASE}/{conversation_id}/messages", headers=auth_headers).json()
    assert [m["id"] for m in remaining["data"]] == ids[:1]


def test_delete_last_message_only_deletes_itself(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    conversation_id = _create_conversation(client, auth_headers)
    ids = _seed_messages(conversation_id, [_TEXT, _TEXT, _TEXT])

    response = client.delete(f"{BASE}/{conversation_id}/messages/{ids[-1]}", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["deleted_message_ids"] == [ids[-1]]
    assert body["count"] == 1


def test_delete_nonexistent_message_is_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    conversation_id = _create_conversation(client, auth_headers)

    response = client.delete(
        f"{BASE}/{conversation_id}/messages/msg_doesnotexist", headers=auth_headers
    )

    assert response.status_code == 404


def test_delete_message_in_other_users_conversation_is_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    other = _create_conversation_for_user("some_other_user")
    ids = _seed_messages(other.id, [_TEXT])

    response = client.delete(f"{BASE}/{other.id}/messages/{ids[0]}", headers=auth_headers)

    assert response.status_code == 404


def test_delete_message_without_auth_is_401(client: TestClient) -> None:
    response = client.delete(f"{BASE}/conv_doesnotexist/messages/msg_doesnotexist")

    assert response.status_code == 401
