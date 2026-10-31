"""Phase 4, Step 8: FinOps cost model — tokens in, dollars out.

Prices are Anthropic first-party API rates, USD per million tokens (taken
from the Claude API reference, cached 2026-10-06). Every turn's real token
usage is captured per model with LangChain's usage-metadata callback, so a
turn's cost is measured, not estimated from prompt length. The semantic
cache (Step 8) uses it to price what a hit saved; the model router (Step 9)
uses it to track spend against a budget.

Cache reads/writes are priced at the input rate here — the swarm doesn't
use prompt caching, so those token counts are zero in practice.
"""

import os
from contextlib import contextmanager
from dataclasses import dataclass, field

from langchain_core.callbacks import get_usage_metadata_callback

# model id prefix → (input $/MTok, output $/MTok). Prefix match so dated
# snapshot ids (claude-haiku-4-5-20251001) resolve to their family.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Used for the monthly projection in cache stats: how many helpdesk turns
# a month the savings rate is extrapolated over.
MONTHLY_TURNS = int(os.getenv("COST_MODEL_MONTHLY_TURNS", "20000"))


def price_for(model: str) -> tuple[float, float]:
    # Longest prefix wins, so "claude-sonnet-5-5" isn't priced as "claude-sonnet-5".
    for prefix in sorted(PRICES_PER_MTOK, key=len, reverse=True):
        if model.startswith(prefix):
            return PRICES_PER_MTOK[prefix]
    raise KeyError(f"no price for model {model!r} — add it to cost_model.PRICES_PER_MTOK")


def cost_usd(usage_by_model: dict[str, dict]) -> float:
    """usage_by_model is LangChain's usage_metadata shape:
    {model: {"input_tokens": n, "output_tokens": n, ...}}"""
    total = 0.0
    for model, usage in usage_by_model.items():
        input_price, output_price = price_for(model)
        total += usage.get("input_tokens", 0) * input_price / 1_000_000
        total += usage.get("output_tokens", 0) * output_price / 1_000_000
    return total


@dataclass
class TurnUsage:
    """Filled in when the measure_turn() block exits."""

    usage_by_model: dict[str, dict] = field(default_factory=dict)
    cost_usd: float = 0.0


@contextmanager
def measure_turn():
    """Captures every LLM call made inside the block — including the nested
    specialist subgraphs on their worker threads, since the callback rides
    on LangChain's callback propagation like tracing does — and prices it."""
    turn = TurnUsage()
    with get_usage_metadata_callback() as callback:
        yield turn
    turn.usage_by_model = dict(callback.usage_metadata)
    turn.cost_usd = cost_usd(turn.usage_by_model)
