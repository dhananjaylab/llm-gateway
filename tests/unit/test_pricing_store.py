"""
test_pricing_store.py

Phase 9a: verifies PricingStore's seed/hot-reload mechanics and, most
importantly, the in-place-mutation claim app/core/pricing_store.py's own
docstring makes — that `refresh()`/`update_pricing()` mutate the SAME dict
object `FallbackRouter` captured a reference to at construction time,
rather than reassigning `app.state.pricing` to a new dict (which would
leave `FallbackRouter` reading stale prices forever).
"""

from __future__ import annotations

import pytest

from app.core.config import TiersConfig
from app.core.pricing import ModelPricing, PricingTable, calculate_cost_usd
from app.core.pricing_store import PricingStore
from app.core.schema import Usage
from app.resilience.fallback import FallbackRouter
from app.resilience.retry import RetryPolicy
from app.resilience.stub import CircuitBreaker as AlwaysClosedCircuitBreaker


def _bootstrap(**rows: ModelPricing) -> PricingTable:
    return dict(rows)


async def test_seed_from_yaml_if_empty_writes_every_configured_row(fake_redis):
    table: PricingTable = {}
    store = PricingStore(fake_redis, table=table)
    bootstrap = _bootstrap(
        **{"openai:gpt-5.4": ModelPricing(input_per_million=2.5, output_per_million=15.0)}
    )

    seeded = await store.seed_from_yaml_if_empty(bootstrap)

    assert seeded == 1
    assert await store.all_model_keys() == ["openai:gpt-5.4"]


async def test_seed_is_idempotent(fake_redis):
    table: PricingTable = {}
    store = PricingStore(fake_redis, table=table)
    bootstrap = _bootstrap(
        **{"openai:gpt-5.4": ModelPricing(input_per_million=2.5, output_per_million=15.0)}
    )
    first = await store.seed_from_yaml_if_empty(bootstrap)
    second = await store.seed_from_yaml_if_empty(bootstrap)
    assert first == 1
    assert second == 0


async def test_refresh_mutates_the_same_dict_object_not_a_new_one(fake_redis):
    """The critical regression guard: `id(table)` must be unchanged
    before/after refresh() — proving in-place mutation, not a reference
    swap, actually happened."""
    table: PricingTable = {}
    original_id = id(table)
    store = PricingStore(fake_redis, table=table)
    await store.seed_from_yaml_if_empty(
        _bootstrap(**{"openai:gpt-5.4": ModelPricing(input_per_million=2.5, output_per_million=15.0)})
    )

    await store.refresh()

    assert id(table) == original_id
    assert table["openai:gpt-5.4"].input_per_million == 2.5


async def test_refresh_preserves_optional_cache_fields(fake_redis):
    table: PricingTable = {}
    store = PricingStore(fake_redis, table=table)
    await store.seed_from_yaml_if_empty(
        _bootstrap(
            **{
                "anthropic:claude-sonnet-5": ModelPricing(
                    input_per_million=2.0, output_per_million=10.0, cache_read_per_million=0.2
                )
            }
        )
    )
    await store.refresh()
    assert table["anthropic:claude-sonnet-5"].cache_read_per_million == 0.2
    assert table["anthropic:claude-sonnet-5"].cache_write_per_million is None


async def test_update_pricing_raises_key_error_for_an_unknown_model_key(fake_redis):
    store = PricingStore(fake_redis, table={})
    with pytest.raises(KeyError):
        await store.update_pricing("does-not-exist", {"input_per_million": 1.0})


async def test_update_pricing_persists_and_only_touches_the_provided_fields(fake_redis):
    table: PricingTable = {}
    store = PricingStore(fake_redis, table=table)
    await store.seed_from_yaml_if_empty(
        _bootstrap(**{"openai:gpt-5.4": ModelPricing(input_per_million=2.5, output_per_million=15.0)})
    )
    await store.refresh()

    updated = await store.update_pricing("openai:gpt-5.4", {"output_per_million": 20.0})

    assert updated.input_per_million == 2.5  # untouched
    assert updated.output_per_million == 20.0  # patched
    assert table["openai:gpt-5.4"].output_per_million == 20.0  # shared object reflects it immediately


async def test_update_pricing_publishes_a_config_change_event(fake_redis):
    store = PricingStore(fake_redis, table={})
    await store.seed_from_yaml_if_empty(
        _bootstrap(**{"openai:gpt-5.4": ModelPricing(input_per_million=2.5, output_per_million=15.0)})
    )

    pubsub = fake_redis.pubsub()
    await pubsub.subscribe(PricingStore.CONFIG_CHANGE_CHANNEL)
    await pubsub.get_message(timeout=1)

    await store.update_pricing("openai:gpt-5.4", {"input_per_million": 3.0})

    msg = await pubsub.get_message(timeout=1)
    assert msg is not None
    assert msg["data"] == "openai:gpt-5.4"
    await pubsub.unsubscribe(PricingStore.CONFIG_CHANGE_CHANNEL)


async def test_the_empty_string_ollama_fallback_row_round_trips_correctly(fake_redis):
    """config/pricing.yaml's own "ollama:" (empty model-name suffix)
    fallback row is a valid, if unusual, key -- must survive seed+refresh
    without special-casing anywhere in this store."""
    table: PricingTable = {}
    store = PricingStore(fake_redis, table=table)
    await store.seed_from_yaml_if_empty(
        _bootstrap(**{"ollama:": ModelPricing(input_per_million=0.0, output_per_million=0.0)})
    )
    await store.refresh()
    assert "ollama:" in table
    assert await store.all_model_keys() == ["ollama:"]


# -- the in-place-mutation claim itself: FallbackRouter's captured reference sees it too --


async def test_fallback_router_captured_pricing_table_reference_sees_the_update(fake_redis):
    """
    The single most important test in this file: `FallbackRouter` is built
    ONCE, at the top, capturing `table` as `self._pricing_table` -- exactly
    the way app/main.py's lifespan constructs it with `app.state.pricing`.
    A PATCH arriving later (via PricingStore.update_pricing, exactly as the
    Admin API would drive it) must change what THAT SAME router computes,
    with no reconstruction.
    """
    table: PricingTable = {"openai:gpt-5.4": ModelPricing(input_per_million=2.5, output_per_million=15.0)}
    store = PricingStore(fake_redis, table=table)
    await store.seed_from_yaml_if_empty(dict(table))
    await store.refresh()

    router = FallbackRouter(
        circuit_breaker=AlwaysClosedCircuitBreaker(),
        retry_policy=RetryPolicy(max_attempts=1),
        tiers_config=TiersConfig(tiers={}),
        pricing_table=table,
    )

    usage = Usage(input_tokens=1_000_000, output_tokens=0)
    before_cost = router._cost_usd_or_none("openai", "gpt-5.4", usage)
    assert before_cost == pytest.approx(2.5)

    await store.update_pricing("openai:gpt-5.4", {"input_per_million": 9.0})

    after_cost = router._cost_usd_or_none("openai", "gpt-5.4", usage)
    assert after_cost == pytest.approx(9.0), (
        "FallbackRouter's captured pricing_table reference must reflect the live update -- "
        "a stale value here means refresh() reassigned the dict instead of mutating it in place"
    )


async def test_calculate_cost_usd_reads_the_same_mutated_table_directly(fake_redis):
    """Belt-and-suspenders check against the free function every call site
    (fallback.py, v1_chat.py) actually calls, not just FallbackRouter's
    private helper."""
    table: PricingTable = {}
    store = PricingStore(fake_redis, table=table)
    await store.seed_from_yaml_if_empty(
        _bootstrap(**{"openai:gpt-5.4": ModelPricing(input_per_million=2.0, output_per_million=10.0)})
    )
    await store.refresh()

    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert calculate_cost_usd(table, "openai", "gpt-5.4", usage) == pytest.approx(12.0)

    await store.update_pricing("openai:gpt-5.4", {"output_per_million": 20.0})

    assert calculate_cost_usd(table, "openai", "gpt-5.4", usage) == pytest.approx(22.0)
