"""
Per-model $ cost calculation for budget enforcement.

Implements the TRD's cost formula (TRD, "Budget Enforcement Formulas and
Prompt-Cache Accounting"):

    cost = input_tokens * input_price
         + output_tokens * output_price
         + cache_read_input_tokens * cache_read_price   (if the provider
           bills cache reads separately, e.g. Anthropic/OpenAI)

Pricing is DATA (config/pricing.yaml), not code, precisely because it
shifts monthly across every provider — see that file's own version note.
This module only implements the *mechanism*: look up a rate, multiply,
sum. Getting the numbers exactly current is an ops task (re-verify the
YAML before this ever bills someone for real), not a code change.

Model-name matching: a provider's *served* model string often carries a
dated snapshot suffix the pricing table doesn't enumerate (OpenAI returns
"gpt-5.4-2026-03-05" for a request that named "gpt-5.4" — see
test_response_normalization.py's own fixture). `_lookup` matches by the
LONGEST pricing-table model-name prefix of the served model string within
that provider, so "gpt-5.4-2026-03-05" resolves to the "gpt-5.4" rate
without the table needing every dated snapshot listed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.core.schema import Usage

logger = logging.getLogger("gateway.pricing")


@dataclass(frozen=True)
class ModelPricing:
    input_per_million: float
    output_per_million: float
    cache_read_per_million: float | None = None
    cache_write_per_million: float | None = None


PricingTable = dict[str, ModelPricing]


def load_pricing(path: str | Path) -> PricingTable:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    table: PricingTable = {}
    for key, entry in (raw.get("models") or {}).items():
        table[key] = ModelPricing(
            input_per_million=float(entry.get("input_per_million", 0.0)),
            output_per_million=float(entry.get("output_per_million", 0.0)),
            cache_read_per_million=(
                float(entry["cache_read_per_million"])
                if entry.get("cache_read_per_million") is not None
                else None
            ),
            cache_write_per_million=(
                float(entry["cache_write_per_million"])
                if entry.get("cache_write_per_million") is not None
                else None
            ),
        )
    return table


def _lookup(table: PricingTable, provider: str, model_served: str) -> ModelPricing | None:
    prefix = f"{provider}:"
    model_served_l = model_served.lower()
    best: tuple[str, ModelPricing] | None = None

    for key, entry in table.items():
        if not key.startswith(prefix):
            continue
        model_key = key[len(prefix) :].lower()
        if (model_key == "" or model_served_l.startswith(model_key)) and (
            best is None or len(model_key) > len(best[0])
        ):
            best = (model_key, entry)

    return best[1] if best else None


def calculate_cost_usd(table: PricingTable, provider: str, model_served: str, usage: Usage) -> float:
    entry = _lookup(table, provider, model_served)
    if entry is None:
        logger.warning(
            "no pricing entry for %s:%s — billing $0.00 for this call; add it to config/pricing.yaml",
            provider,
            model_served,
        )
        return 0.0

    cost = (usage.input_tokens * entry.input_per_million / 1_000_000) + (
        usage.output_tokens * entry.output_per_million / 1_000_000
    )
    if entry.cache_read_per_million is not None and usage.cache_read_input_tokens:
        cost += usage.cache_read_input_tokens * entry.cache_read_per_million / 1_000_000
    if entry.cache_write_per_million is not None and usage.cache_creation_input_tokens:
        cost += usage.cache_creation_input_tokens * entry.cache_write_per_million / 1_000_000

    return round(cost, 8)
