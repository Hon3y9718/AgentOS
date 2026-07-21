# api/v1/

Routers, auth deps, status codes, SSE framing (ARCHITECTURE.md). `conversations.py`,
`messages.py`, `chat.py` (§5.4 and §5.5, both response shapes), `auth.py`
(register/login/logout, §1), `models.py` (§4), and `deps.py`
(`get_current_user`, real JWT verification) are real; `tools.py` and
`files.py` don't exist yet. `/health` and `/health/ready` live in
`app/main.py` instead, because they sit outside the `/api/v1` base path
(API_CONTRACT §0, §5.1).

## What lives here

- One router module per resource (`conversations.py`, `messages.py`,
  `chat.py`, `auth.py`, `models.py`, `tools.py`, `files.py`) mounted under
  `/api/v1`. `chat.py` and `messages.py` share a URL path
  (`/conversations/{id}/messages`, different HTTP methods) but stay
  separate files, matching the same split in `app/services/`. `models.py`
  is the one router here with no DB access at all — it's a thin wrapper
  over `app/services/models.py`, which itself only reads the in-memory
  `core/llm` registry.
- Auth dependency (`get_current_user`, `app/api/v1/deps.py`) — real JWT
  verification via `app.core.auth`, not the original MVP stub.
- SSE response framing for the chat endpoint (§5.5) — `chat.py`'s router
  `await`s `app.services.chat.prepare_stream()` *before* constructing the
  `StreamingResponse`, specifically so a pre-stream `DomainError` still
  produces a normal HTTP error rather than a corrupted 200. Only
  `service.emit_stream()` (called after that succeeds) is the actual
  response-body generator. See `app/services/chat.py`'s module docstring
  and `docs/DECISIONS/0002 Provider Abstraction.md`.

**`auth.py` is the one router here that doesn't follow "thin — validate,
call service, return schema."** It composes fastapi-users' own pre-built
routers instead. See its own module docstring and
`docs/DECISIONS/0003 Auth Layering.md`.

## What must never live here

- Business logic, orchestration, or transactions — call into `app/services/`
  and return what it gives you.
- Direct DB access — go through a service.

## How to add a new one

1. Add the router module, thin: validate → call service → return a schema.
2. `app.include_router(...)` it in `main.py`.
3. Update `docs/API_CONTRACT.md` in the same PR if the wire format moved.
