"""Typed, sortable resource IDs (API_CONTRACT.md §0).

Role: the one place a `conv_`/`msg_`/`run_`-style ID gets generated.
Called by: app/services/* (once they exist). Calls nothing internal.
Gotcha: uuid7 is time-ordered, unlike uuid4 — that's what makes cursor
pagination (§5.2) a plain `WHERE id > cursor ORDER BY id` query instead of
needing a separate `created_at` sort key.
See: docs/API_CONTRACT.md#0-ground-rules
"""

from uuid6 import uuid7


def new_id(prefix: str) -> str:
    """Generate a typed, chronologically-sortable ID, e.g. `new_id("conv")`.

    Args:
        prefix: the resource's type tag — "conv", "msg", "run", etc.

    Returns:
        A string like "conv_018f5c2a...".
    """
    return f"{prefix}_{uuid7().hex}"
