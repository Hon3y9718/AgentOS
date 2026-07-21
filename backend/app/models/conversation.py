"""Conversation table (API_CONTRACT.md §3.2).

Role: persistence shape for a conversation. No business methods — see
ARCHITECTURE.md's "models/ must not contain business methods."
Called by: app/services/conversations.py (once it exists), alembic/env.py via
Base.metadata. Calls nothing internal beyond app.db.base.
Gotcha: the Python attribute is `metadata_`, not `metadata` — SQLAlchemy's
DeclarativeBase already owns a class-level `.metadata` (the schema's MetaData
registry), so a same-named instance attribute would collide with it. The DB
column itself is still named `metadata` to match the wire field in §3.2;
translating between the two names is the service layer's job.
See: docs/API_CONTRACT.md#32-conversation
"""

from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(primary_key=True)
    # WHY no ForeignKey to a users table: API_CONTRACT §1 auth is still a
    # stub ("any token resolves to a fixed development user") — there is no
    # users table to reference yet. Scoping by user_id from day one is still
    # required by ARCHITECTURE.md; the FK constraint arrives with real auth.
    user_id: Mapped[str] = mapped_column(index=True)

    title: Mapped[str | None]
    system_prompt: Mapped[str | None]
    default_model: Mapped[str | None]
    default_params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    message_count: Mapped[int] = mapped_column(default=0)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    # WHY deleted_at instead of a real DELETE: §5.2 specifies conversation
    # deletion is a soft delete. The service layer must filter
    # `deleted_at IS NULL` on every read — nothing here enforces that; models
    # own columns, not query behavior.
    deleted_at: Mapped[datetime | None]
