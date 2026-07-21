"""Message table (API_CONTRACT.md §3.3).

Role: persistence shape for one message. No business methods.
Called by: app/services/* (once they exist), alembic/env.py via Base.metadata.
Calls nothing internal beyond app.db.base.
Gotcha: `content` and `usage` are JSONB, not normalized columns or tables —
the Pydantic schema in app/schemas/ (§3.1's typed content blocks) is what
enforces their shape, not this table. Adding a new block type or usage field
needs a schema change, not a migration.
See: docs/API_CONTRACT.md#33-message
"""

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)

    # WHY plain str, not a Postgres ENUM, for role/status/stop_reason: a DB
    # enum needs a migration to add a value, and providers add new stop
    # reasons over time. The contract vocabulary (§2, §3.3) is enforced by
    # the Pydantic schema at the API boundary instead — ARCHITECTURE.md's
    # models own columns, not validation.
    role: Mapped[str]
    content: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(default="pending")
    model: Mapped[str | None]
    stop_reason: Mapped[str | None]
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None]
