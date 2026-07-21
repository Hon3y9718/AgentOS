"""Idempotency key store (API_CONTRACT.md §5.4).

Role: makes the chat endpoint's retry button safe. `Idempotency-Key` is
required on that endpoint; a replay with the same body returns the original
result, a replay with a different body is a 409, and — the actual point of
this existing at all, per §5.4's own framing ("what stops an agent retry
loop from duplicating turns") — two *concurrent* calls with the same key
must not both do the real work.
Called by: app/services/chat.py. Calls app.models.idempotency_key, app.core.errors.
Gotcha: concurrency safety comes from `key` being the table's primary key
(app/models/idempotency_key.py) — a second concurrent insert fails with a
real DB-level IntegrityError, not a race-prone SELECT-then-INSERT. See
`_claim()`.
Gotcha: a genuinely concurrent duplicate (same key, same body, arriving
while the first call is still `status="pending"`) raises ConflictError —
the closest fit in §2's taxonomy, but an imperfect one: `retryable: false`
undersells it, since retrying once the first call finishes is exactly
correct. §5.4 doesn't define this case explicitly; not inventing a new §2
type to fit it more precisely.
See: docs/API_CONTRACT.md#54-chat--the-core-endpoint
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models.idempotency_key import IdempotencyKey as IdempotencyKeyModel

# WHY 24h: API_CONTRACT §5.4 — "The key is stored with a hash of the request
# body for 24 hours." Enforced lazily (checked at lookup time), not by a
# scheduled sweep — no scheduled-job infrastructure exists in this repo yet.
_TTL = timedelta(hours=24)


def hash_request_body(body: dict[str, Any]) -> str:
    """Hash a request body for replay comparison.

    WHY re-serialize with sort_keys rather than hash raw bytes: two requests
    that are the same *logical* JSON body but differ in key order or
    whitespace should hash identically — hashing the canonicalized form,
    not the wire bytes, is what makes that true.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def check_or_claim(
    db: AsyncSession, *, user_id: str, conversation_id: str, key: str, request_hash: str
) -> IdempotencyKeyModel | None:
    """Claim `key` for a new request, or resolve it against an existing one.

    Returns:
        None if the caller should proceed with the real work — `key` is now
            claimed as `status="pending"`; call `complete()` or `abandon()`
            afterward.
        The existing row if this is a valid replay of an already-completed
            request (same key, same body hash, same user).

    Raises:
        ConflictError: `key` was used before with a different request body,
            belongs to another user, or a request with `key` is still in
            flight (see module docstring).
    """
    if await _claim(
        db, user_id=user_id, conversation_id=conversation_id, key=key, request_hash=request_hash
    ):
        return None
    return await _resolve_conflict(db, user_id=user_id, request_hash=request_hash, key=key)


async def complete(
    db: AsyncSession, *, key: str, response_status: int, response_body: dict[str, Any]
) -> None:
    """Record the successful outcome so future replays can return it."""
    row = await db.get(IdempotencyKeyModel, key)
    assert row is not None, "complete() called for a key that was never claimed"
    row.status = "complete"
    row.response_status = response_status
    row.response_body = response_body
    await db.commit()


async def abandon(db: AsyncSession, *, key: str) -> None:
    """Delete a still-pending row after the underlying operation failed.

    WHY delete, not mark "failed": a pending row that's never resolved would
    permanently block every future retry with this key behind the
    concurrent-duplicate check in `_resolve_conflict()`. Deleting it means a
    retry after a genuine failure is treated as a fresh attempt, which is
    what a client pressing "retry" actually wants.
    """
    row = await db.get(IdempotencyKeyModel, key)
    if row is not None:
        await db.delete(row)
        await db.commit()


async def _claim(
    db: AsyncSession, *, user_id: str, conversation_id: str, key: str, request_hash: str
) -> bool:
    """Try to insert a new pending row. Returns whether this call won the
    race and claimed `key` — the DB's primary-key uniqueness is what makes
    this safe under concurrent callers, not application-level locking."""
    db.add(
        IdempotencyKeyModel(
            key=key,
            user_id=user_id,
            conversation_id=conversation_id,
            request_hash=request_hash,
            status="pending",
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return False
    return True


async def _resolve_conflict(
    db: AsyncSession, *, user_id: str, request_hash: str, key: str
) -> IdempotencyKeyModel | None:
    existing = (
        await db.execute(select(IdempotencyKeyModel).where(IdempotencyKeyModel.key == key))
    ).scalar_one_or_none()
    # WHY this can't be None: `_claim()` only calls this after its own
    # insert lost a unique-key race, which means a row with this key exists
    # by definition at that instant. The only way it vanishes before this
    # SELECT runs is a concurrent `abandon()` — an extremely narrow window,
    # accepted as a known limitation rather than solved with row locking.
    assert existing is not None

    if existing.user_id != user_id:
        # WHY the same ConflictError as a body mismatch, not a distinct
        # message: telling a caller "this key belongs to someone else"
        # leaks information about another user's request (§1: never leak
        # existence).
        raise ConflictError(
            "Idempotency-Key was already used with a different request.",
            code="idempotency.conflict",
        )

    if _is_expired(existing):
        conversation_id = existing.conversation_id
        await db.delete(existing)
        await db.commit()
        if await _claim(
            db,
            user_id=user_id,
            conversation_id=conversation_id,
            key=key,
            request_hash=request_hash,
        ):
            return None
        # Narrow race: another request claimed the same key in the gap
        # between our delete and our re-claim. Resolve against whatever is
        # there now rather than looping unboundedly.
        return await _resolve_conflict(db, user_id=user_id, request_hash=request_hash, key=key)

    if existing.request_hash != request_hash:
        raise ConflictError(
            "Idempotency-Key was already used with a different request body.",
            code="idempotency.conflict",
        )

    if existing.status == "pending":
        raise ConflictError(
            "A request with this Idempotency-Key is already in progress.",
            code="idempotency.in_progress",
        )

    return existing


def _is_expired(row: IdempotencyKeyModel) -> bool:
    return datetime.now(UTC).replace(tzinfo=None) - row.created_at > _TTL
