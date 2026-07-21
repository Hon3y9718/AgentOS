"""app/core/ids.py — typed, sortable ID generation (API_CONTRACT.md §0)."""

from app.core.ids import new_id


def test_new_id_has_typed_prefix() -> None:
    assert new_id("conv").startswith("conv_")
    assert new_id("msg").startswith("msg_")


def test_new_id_is_chronologically_sortable() -> None:
    # WHY this matters: §5.2's cursor pagination relies on IDs sorting the
    # same way their creation order does — this is the property uuid7 (not
    # uuid4) buys us.
    ids = [new_id("conv") for _ in range(50)]
    assert ids == sorted(ids)
