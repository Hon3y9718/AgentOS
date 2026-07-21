"""Declarative base for SQLAlchemy models.

Role: the Base class every table in app/models/ will inherit from, and the
target_metadata alembic/env.py autogenerates migrations against.
Called by: app/models/* (once tables exist), alembic/env.py. Calls nothing internal.
Gotcha: must never import app/models/* — models import Base, not the other way,
or autogenerate's metadata import in alembic/env.py becomes circular.
See: docs/ARCHITECTURE.md#package-responsibilities
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all ORM models. No columns, no behavior."""
