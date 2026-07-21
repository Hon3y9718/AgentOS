"""Cost computation from token usage + registry pricing (API_CONTRACT.md §3.3, §4).

Role: the one place `LLMUsage` (raw token counts) becomes `cost_usd` (a
decimal string). Deliberately not part of any adapter — ARCHITECTURE.md's
request lifecycle assigns "computed cost" to the service, and this needs
registry pricing data alongside adapter-yielded usage, which only a caller
holding both has (see ADR-0002's note on why MessageDelta.usage is LLMUsage,
not the wire Usage type).
Called by: app/services/chat.py (and the future SSE chat service, once it
exists — the same math, not duplicated).
Calls: app.core.llm.types, app.core.llm.catalog.
Gotcha: `Pricing` (§4) has no cache-write rate — Anthropic prices cache
writes at a premium over plain input tokens, but the contract's registry
shape doesn't model that distinctly. cache_write_tokens is billed at the
plain input rate here, which underprices a real cache write. Low-impact
today (nothing in this repo requests prompt caching yet, so cache_write_tokens
is always 0 in practice) but worth fixing — by widening Pricing, deliberately,
not silently — before prompt caching is ever turned on.
See: docs/DECISIONS/0002 Provider Abstraction.md
"""

from decimal import ROUND_HALF_UP, Decimal

from app.core.llm.catalog import Pricing
from app.core.llm.types import LLMUsage

_MTOK = Decimal(1_000_000)
# WHY 6 decimal places: matches the number of digits in API_CONTRACT §3.3's
# own worked example ("0.001584") — the contract doesn't state a rounding
# rule explicitly, so this is an inferred convention, not a quoted one.
_QUANTIZE = Decimal("0.000001")


def compute_cost_usd(usage: LLMUsage, pricing: Pricing) -> str:
    """Compute cost as a decimal string. Never uses float — CLAUDE.md: money
    is a decimal string, and the arithmetic that produces it must be Decimal
    throughout, or the string is only accidentally correct."""

    input_price = Decimal(pricing.input_per_mtok_usd)
    output_price = Decimal(pricing.output_per_mtok_usd)
    # WHY fall back to input_price, not zero: a registry entry with no
    # cache_read_per_mtok_usd set (the field is Optional) shouldn't make
    # cache-read tokens free — treating them as ordinary input tokens is the
    # conservative default until every entry specifies a real discount rate.
    cache_read_price = (
        Decimal(pricing.cache_read_per_mtok_usd)
        if pricing.cache_read_per_mtok_usd is not None
        else input_price
    )

    total = (
        Decimal(usage.input_tokens) * input_price
        + Decimal(usage.output_tokens) * output_price
        + Decimal(usage.cache_read_tokens) * cache_read_price
        + Decimal(usage.cache_write_tokens) * input_price
    ) / _MTOK

    return str(total.quantize(_QUANTIZE, rounding=ROUND_HALF_UP))
