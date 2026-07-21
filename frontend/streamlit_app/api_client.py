"""HTTP client for the AgentOS backend (docs/API_CONTRACT.md).

Role: the only file under frontend/ allowed to talk HTTP to the backend
(ARCHITECTURE.md — "All frontend HTTP access goes through
frontend/streamlit_app/api_client.py and nowhere else"). app.py calls these
functions and never touches httpx or the wire format directly.
Called by: frontend/streamlit_app/app.py. Calls nothing internal — this is
the frontend's own leaf, talking only to the backend over HTTP.
Gotcha: MVP auth (API_CONTRACT §1) accepts any Bearer token and resolves it
to one fixed dev user — AGENTOS_API_TOKEN below is not a real credential,
just a value satisfying the "Bearer <token>" shape.
See: docs/API_CONTRACT.md
"""

import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import httpx

# WHY os.getenv here, not a config.py module: CLAUDE.md's "config only via
# app/config.py, no os.getenv elsewhere" is a backend/app/ rule
# (scripts/check_layering.sh only greps that path) — frontend/ has no
# equivalent, and CLAUDE.md calls Streamlit disposable, not worth building
# one for a client getting replaced by Next.js.
API_BASE_URL = os.getenv("AGENTOS_API_BASE_URL", "http://localhost:8000")
API_TOKEN = os.getenv("AGENTOS_API_TOKEN", "dev-token")
# WHY hardcoded, not fetched from GET /api/v1/models: that endpoint doesn't
# exist yet (ROADMAP.md) — this is the one model core/llm's registry.yaml
# actually has an adapter for. A conversation created with no default_model
# fails its first message with invalid_request ("no model specified"), so
# the UI needs to set one; there's nothing to select from yet regardless.
_DEFAULT_MODEL = "anthropic:claude-sonnet-4-5"

_AUTH_HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}
_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
# WHY a longer read timeout just for the chat call: a model turn can
# legitimately take longer than a normal CRUD request; §6's own stream idle
# timeout is 120s, so the client shouldn't time out before the server would.
_STREAM_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


class ApiError(Exception):
    """Raised for any non-2xx response — carries the §2 error envelope."""

    def __init__(self, status_code: int, envelope: dict[str, Any]) -> None:
        self.status_code = status_code
        self.envelope = envelope
        message = envelope.get("error", {}).get("message", "Request failed")
        super().__init__(f"{message} ({status_code})")


def _raise_for_error(response: httpx.Response) -> None:
    if response.status_code >= 400:
        raise ApiError(response.status_code, response.json())


def list_conversations(*, cursor: str | None = None, limit: int = 20) -> dict[str, Any]:
    """GET /api/v1/conversations (§5.2), newest first."""
    params: dict[str, Any] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    with httpx.Client(
        base_url=API_BASE_URL, headers=_AUTH_HEADERS, timeout=_DEFAULT_TIMEOUT
    ) as client:
        response = client.get("/api/v1/conversations", params=params)
    _raise_for_error(response)
    return response.json()


def create_conversation(*, title: str | None = None) -> dict[str, Any]:
    """POST /api/v1/conversations (§5.2). `title` stays null until the
    first exchange completes server-side — see §5.2's client obligation."""
    with httpx.Client(
        base_url=API_BASE_URL, headers=_AUTH_HEADERS, timeout=_DEFAULT_TIMEOUT
    ) as client:
        response = client.post(
            "/api/v1/conversations", json={"title": title, "default_model": _DEFAULT_MODEL}
        )
    _raise_for_error(response)
    return response.json()


def delete_conversation(conversation_id: str) -> None:
    """DELETE /api/v1/conversations/{id} (§5.2), soft delete server-side."""
    with httpx.Client(
        base_url=API_BASE_URL, headers=_AUTH_HEADERS, timeout=_DEFAULT_TIMEOUT
    ) as client:
        response = client.delete(f"/api/v1/conversations/{conversation_id}")
    _raise_for_error(response)


def list_messages(conversation_id: str, *, cursor: str | None = None) -> dict[str, Any]:
    """GET .../messages (§5.3), chronological (the API's own default order)."""
    params: dict[str, Any] = {}
    if cursor:
        params["cursor"] = cursor
    with httpx.Client(
        base_url=API_BASE_URL, headers=_AUTH_HEADERS, timeout=_DEFAULT_TIMEOUT
    ) as client:
        response = client.get(f"/api/v1/conversations/{conversation_id}/messages", params=params)
    _raise_for_error(response)
    return response.json()


def stream_chat_message(conversation_id: str, text: str) -> Iterator[dict[str, Any]]:
    """Send one user turn and yield parsed SSE events (§5.4, §5.5).

    WHY yielding the raw parsed {event, data} pairs, not just extracted text
    chunks: the caller needs more than the text delta — content_block_start
    marks a new block beginning, message_stop/error mark the turn's end
    (successfully or not). Reducing to text-only here would hide that.

    WHY a fresh Idempotency-Key generated here: §7's client obligation is
    "send Idempotency-Key on every message creation and reuse it on retry" —
    each call to this function is one send action, not a retry of a
    previous one, so a new key is correct here specifically.
    """
    idempotency_key = str(uuid.uuid4())
    payload = {"content": [{"type": "text", "text": text}]}
    headers = {
        **_AUTH_HEADERS,
        "Idempotency-Key": idempotency_key,
        "Accept": "text/event-stream",
    }

    with (
        httpx.Client(base_url=API_BASE_URL, timeout=_STREAM_TIMEOUT) as client,
        client.stream(
            "POST",
            f"/api/v1/conversations/{conversation_id}/messages",
            json=payload,
            headers=headers,
        ) as response,
    ):
        if response.status_code >= 400:
            response.read()
            raise ApiError(response.status_code, response.json())

        event_name = ""
        for line in response.iter_lines():
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data = json.loads(line.removeprefix("data:").strip())
                yield {"event": event_name, "data": data}
