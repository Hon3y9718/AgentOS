# schemas/

Wire format in and out (ARCHITECTURE.md). `conversation.py`, `message.py`,
`content_block.py`, `pagination.py`, and `chat.py` (§5.4) are real. No
schema for the model registry entry or the error envelope yet — those are
still hand-assembled where they're used (`app/core/llm/registry.py`,
`app/main.py`'s `domain_error_handler`).

## What lives here

- Pydantic models mirroring `docs/API_CONTRACT.md` §3 exactly: conversations,
  messages, content blocks, the model registry entry, the error envelope.
- Request models use `extra="forbid"` (CLAUDE.md) so unknown fields 422 instead
  of being silently dropped.

## What must never live here

- A DB session, a query, or an import from `app/db/`.
- A provider SDK type — `core/llm/` normalizes before anything reaches here.

## A leaf package, not a rung above `core/llm/`

`docs/DECISIONS/0002 Provider Abstraction.md` decision 2: `core/llm/types.py`
imports `content_block.py` and `message.py`'s `StopReason` directly. This
package has no dependency on `services/`, `models/`, `db/`, or `core/llm/`
itself, so that import doesn't invert `ARCHITECTURE.md`'s `api → services →
core/llm` direction — read the ADR before assuming schemas/ may only be
imported from `api/v1/`.

## How to add a new one

1. Match the JSON shape in `API_CONTRACT.md` field-for-field; don't invent
   names. If the shape needs to change, amend the contract in the same PR.
2. Split request/response models even when identical today — they drift.
