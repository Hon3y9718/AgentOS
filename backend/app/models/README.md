# models/

SQLAlchemy tables (ARCHITECTURE.md). `conversation.py`, `message.py`,
`idempotency_key.py`, and `user.py` are real. `conversations.user_id` and
`idempotency_keys.user_id` FK to `users.id`.

## What lives here

- One module per table (`conversation.py`, `message.py`,
  `idempotency_key.py`, ...), each a class inheriting `app.db.base.Base`.

## What must never live here

- Business methods, validation, or anything beyond columns/relationships —
  that belongs in `app/services/`. A model is a table, not a domain object.

## How to add a new one

1. Define the class, inheriting `Base` from `app/db/base.py`.
2. Import it somewhere `alembic/env.py` reaches at import time (so its table
   is registered on `Base.metadata` before autogenerate runs).
3. `make migrate m="add <table>"` to generate the revision. Never hand-edit
   `alembic/versions/*` (CLAUDE.md).
