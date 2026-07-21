"""Async DB engine, pooling, and the FastAPI session dependency.

Role: owns engine/session lifecycle. Contains no queries (ARCHITECTURE.md).
Called by: main.py (readiness check), app/api/v1/* routers via Depends(get_db)
once they exist. Calls nothing internal except app.config.
Gotcha: pool_pre_ping is required — without it, a connection killed by a
postgres restart or idle timeout stays in the pool looking healthy until a
query hits it, so failures surface deep inside an unrelated request instead of
being caught on checkout.
See: docs/ARCHITECTURE.md#state-and-concurrency
"""

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

# WHY pool_size/max_overflow set explicitly: SQLAlchemy's defaults (5 + 10
# overflow) are a guess tuned for nothing in particular here. Naming them
# makes capacity a decision, not an accident of the library version.
engine: AsyncEngine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

# WHY expire_on_commit=False: the default expires every ORM attribute after
# commit, forcing a re-fetch on next access. That re-fetch is a lazy load,
# which async SQLAlchemy cannot perform outside an await — so the default
# turns innocent attribute access after commit into a MissingGreenlet error.
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding one session per request.

    Why a generator, not a plain return: FastAPI closes a generator
    dependency only after the response finishes (including SSE streams). A
    plain `return session` would let FastAPI close it as soon as the handler
    function itself returns, before a stream even starts sending.
    """
    async with async_session_factory() as session:
        yield session


# WHY an Annotated alias, not `Depends(get_db)` inline at each call site:
# ruff/bugbear's B008 flags function calls in argument defaults; this is the
# FastAPI-recommended way to avoid it — see the matching CurrentUser alias
# in app/api/v1/deps.py.
DbSession = Annotated[AsyncSession, Depends(get_db)]
