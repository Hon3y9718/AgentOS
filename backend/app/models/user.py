"""User table (API_CONTRACT.md §1).

Role: persistence shape for a registered account, plus the flat per-user
token quota this feature adds. No business methods — see
ARCHITECTURE.md's "models/ must not contain business methods."
Called by: app/core/auth/* (fastapi-users wiring), app/services/users.py,
alembic/env.py via Base.metadata. Calls nothing internal beyond app.db.base
and app.core.ids.
Gotcha: `email`, `hashed_password`, `is_active`, `is_superuser`, `is_verified`
come from fastapi_users_db_sqlalchemy's `SQLAlchemyBaseUserTable` mixin, not
defined below — only `id` needs overriding, because the mixin only declares
it under `TYPE_CHECKING` (it has no opinion on ID type/generation strategy
by design; the UUID-generating variant is a separate subclass we're not using).
See: docs/API_CONTRACT.md#1-authentication, docs/DECISIONS/0003 Auth Layering.md
"""

from datetime import datetime

from fastapi_users_db_sqlalchemy import SQLAlchemyBaseUserTable
from sqlalchemy import func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import Base


class User(SQLAlchemyBaseUserTable[str], Base):
    __tablename__ = "users"

    # WHY new_id("user"), not the mixin's UUID default: matches this repo's
    # typed-prefix ID convention (core/ids.py) used by every other table —
    # SQLAlchemyBaseUserTable itself is ID-type-agnostic (see module docstring).
    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: new_id("user"))

    # WHY a flat counter pair, not a UsageRecord ledger with reset periods:
    # MVP scope is "per-user limit exists and is enforced," not "quotas reset
    # monthly" — see app/services/users.py. No admin endpoint adjusts
    # token_limit yet; it's a fixed default until one exists.
    token_limit: Mapped[int] = mapped_column(default=1_000_000)
    tokens_used: Mapped[int] = mapped_column(default=0)

    # WHY naive UTC, matching conversation.py/message.py: asyncpg rejects a
    # tz-aware Python datetime against a TIMESTAMP WITHOUT TIME ZONE column
    # (this repo's default for a plain `Mapped[datetime]`) — see
    # app/services/conversations.py's delete_conversation for the same gotcha.
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
