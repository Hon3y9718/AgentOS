"""app/core/llm/pricing.py — cost computation (API_CONTRACT.md §3.3, §4).

Unit tier (ARCHITECTURE.md): pure function, no DB, no network.
"""

from app.core.llm.pricing import compute_cost_usd
from app.core.llm.registry import Pricing
from app.core.llm.types import LLMUsage


def test_matches_api_contract_worked_example() -> None:
    pricing = Pricing(
        input_per_mtok_usd="3.00", output_per_mtok_usd="15.00", cache_read_per_mtok_usd="0.30"
    )
    usage = LLMUsage(input_tokens=412, output_tokens=88)

    assert compute_cost_usd(usage, pricing) == "0.002556"


def test_cache_read_tokens_priced_at_their_own_rate() -> None:
    pricing = Pricing(
        input_per_mtok_usd="3.00", output_per_mtok_usd="15.00", cache_read_per_mtok_usd="0.30"
    )
    usage = LLMUsage(input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000)

    assert compute_cost_usd(usage, pricing) == "0.300000"


def test_cache_read_without_a_configured_rate_falls_back_to_input_price() -> None:
    pricing = Pricing(input_per_mtok_usd="3.00", output_per_mtok_usd="15.00")
    usage = LLMUsage(input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000)

    assert compute_cost_usd(usage, pricing) == "3.000000"


def test_zero_usage_is_zero_cost() -> None:
    pricing = Pricing(input_per_mtok_usd="3.00", output_per_mtok_usd="15.00")
    usage = LLMUsage(input_tokens=0, output_tokens=0)

    assert compute_cost_usd(usage, pricing) == "0.000000"


def test_returns_a_string_not_a_float() -> None:
    pricing = Pricing(input_per_mtok_usd="3.00", output_per_mtok_usd="15.00")
    usage = LLMUsage(input_tokens=1, output_tokens=1)

    assert isinstance(compute_cost_usd(usage, pricing), str)
