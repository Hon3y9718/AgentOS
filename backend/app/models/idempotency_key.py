"""Idempotency key table (API_CONTRACT.md §5.4).

Role: persistence shape for one Idempotency-Key record. No business
methods — see ARCHITECTURE.md's "models/ must not contain business methods."
Called by: app/services/idempotency.py. Calls nothing internal beyond app.db.base.
Gotcha: `key` (the raw client-supplied header value) is the primary key, not
a generated id — its own uniqueness is exactly the DB-level mutual-exclusion
mechanism that makes a concurrent duplicate request fail with an
IntegrityError instead of racing through twice. See app/services/idempotency.py.
See: docs/API_CONTRACT.md#54-chat--the-core-endpoint
"""

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    # WHY the client-supplied key itself, not a generated id: its uniqueness
    # as a primary key IS the mechanism (see module docstring).
    key: Mapped[str] = mapped_column(primary_key=True)
    # WHY stored and always filtered on, even though a `key` collision
    # across two different users is practically impossible (client-generated
    # UUIDs): a lookup that ignores user_id would let one user probe for or
    # replay another user's cached response by guessing their key.
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    conversation_id: Mapped[str]
    # WHY a hash, not the raw body: the raw request body isn't needed after
    # the fact — only whether a replay's body matches the original well
    # enough to be the same logical request (§5.4: different body -> 409).
    request_hash: Mapped[str]
    # WHY plain str, not a Postgres ENUM: same reasoning as
    # app/models/message.py's role/status columns — no migration needed to
    # add a value later.
    status: Mapped[str] = mapped_column(default="pending")
    response_status: Mapped[int | None]
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
