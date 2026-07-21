# services/

Orchestration, transactions, decisions (ARCHITECTURE.md). `conversations.py`
(CRUD, §5.2), `messages.py` (list + truncate-delete, §5.3), `chat.py` (the
core turn, §5.4 *and* §5.5 — both non-streaming and streaming),
`idempotency.py` (§5.4's Idempotency-Key store), and `models.py` (§4 — the
one module here with no DB session at all, since the registry it filters
is static and in-memory) are real; `titling.py`, `tools.py`, `files.py`
don't exist yet. `messages.py`'s `row_to_schema()` is exported (not
`_`-prefixed) specifically so `chat.py` can reuse it.

`chat.py` exports four entry points, not one — `create_chat_message()` for
the JSON response; `prepare_stream()` + `emit_stream()`, always called as a
pair, for the SSE response. `prepare_stream()` must be `await`ed directly by
the caller (never wrapped in a generator itself) so a `DomainError` it
raises still produces a normal HTTP error before any response body starts —
see the module's own docstring before changing this shape.

## What lives here

- One module per resource area (`conversations.py`, `chat.py`, `titling.py`),
  each callable without an HTTP request existing — the agent runtime, a
  background job, and a future CLI all need to call this code directly.
- Domain error raises, from `app/core/errors.py`.

## What must never live here

- **`import fastapi`, or anything from `starlette`.** CI greps for this
  (`scripts/check_layering.sh`) and fails the build.
- HTTP status codes, `Request`/`Response` objects, or SSE framing.

## How to add a new one

1. Write the function to work from plain Python types and DB sessions, not
   from a request object.
2. Raise from `app/core/errors.py` on failure; never return an error tuple.
3. Router calls it and translates the domain error, if any — nothing else does.
