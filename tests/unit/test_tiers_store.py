"""
test_tiers_store.py

Phase 9a: verifies TiersConfigStore's seed/hot-reload mechanics and, most
importantly, the RCU (read-copy-update) claim app/core/tiers_store.py's own
docstring makes — that a `FallbackRouter` constructed BEFORE a PATCH still
sees the update afterward, with zero reconstruction, because `refresh()`
mutates the shared `TiersConfig` instance in place rather than replacing it.
"""

from __future__ import annotations

import pytest

from app.core.config import TiersConfig
from app.core.tiers_store import TiersConfigStore
from app.resilience.fallback import FallbackRouter
from app.resilience.retry import RetryPolicy
from app.resilience.stub import CircuitBreaker as AlwaysClosedCircuitBreaker


def _bootstrap(**tiers: list[str]) -> TiersConfig:
    return TiersConfig(tiers=tiers)


async def test_seed_from_yaml_if_empty_writes_every_configured_tier(fake_redis):
    store = TiersConfigStore(fake_redis, tiers_config=TiersConfig(tiers={}))
    bootstrap = _bootstrap(**{"tier-1-reasoning": ["openai:gpt-5.4", "anthropic:claude-sonnet-5"]})

    seeded = await store.seed_from_yaml_if_empty(bootstrap)

    assert seeded == 1
    assert await store.all_tier_names() == ["tier-1-reasoning"]


async def test_seed_is_idempotent_and_skips_when_tiers_already_exist(fake_redis):
    store = TiersConfigStore(fake_redis, tiers_config=TiersConfig(tiers={}))
    bootstrap = _bootstrap(**{"tier-1-reasoning": ["openai:gpt-5.4"]})

    first = await store.seed_from_yaml_if_empty(bootstrap)
    second = await store.seed_from_yaml_if_empty(bootstrap)

    assert first == 1
    assert second == 0


async def test_refresh_populates_the_shared_tiers_config_object(fake_redis):
    shared = TiersConfig(tiers={})
    store = TiersConfigStore(fake_redis, tiers_config=shared)
    await store.seed_from_yaml_if_empty(_bootstrap(**{"tier-3-local": ["ollama:llama3.2"]}))

    assert shared.chain_for("tier-3-local") is None  # not yet refreshed
    await store.refresh()
    assert shared.chain_for("tier-3-local") == ["ollama:llama3.2"]


async def test_update_tier_raises_key_error_for_an_unknown_tier(fake_redis):
    store = TiersConfigStore(fake_redis, tiers_config=TiersConfig(tiers={}))
    with pytest.raises(KeyError):
        await store.update_tier("does-not-exist", ["openai:gpt-5.4"])


async def test_update_tier_persists_and_the_shared_object_reflects_it_immediately(fake_redis):
    shared = TiersConfig(tiers={})
    store = TiersConfigStore(fake_redis, tiers_config=shared)
    await store.seed_from_yaml_if_empty(_bootstrap(**{"tier-2-fast": ["openai:gpt-5.6-terra"]}))
    await store.refresh()

    updated = await store.update_tier("tier-2-fast", ["anthropic:claude-haiku-4-5", "ollama:llama3.2"])

    assert updated == ["anthropic:claude-haiku-4-5", "ollama:llama3.2"]
    # update_tier() calls refresh() internally -- no separate refresh() call
    # should be needed for the shared object to already reflect the change.
    assert shared.chain_for("tier-2-fast") == ["anthropic:claude-haiku-4-5", "ollama:llama3.2"]


async def test_update_tier_publishes_a_config_change_event(fake_redis):
    store = TiersConfigStore(fake_redis, tiers_config=TiersConfig(tiers={}))
    await store.seed_from_yaml_if_empty(_bootstrap(**{"tier-1-reasoning": ["openai:gpt-5.4"]}))

    pubsub = fake_redis.pubsub()
    await pubsub.subscribe(TiersConfigStore.CONFIG_CHANGE_CHANNEL)
    await pubsub.get_message(timeout=1)  # drain the subscribe confirmation

    await store.update_tier("tier-1-reasoning", ["ollama:llama3.2"])

    msg = await pubsub.get_message(timeout=1)
    assert msg is not None
    assert msg["type"] == "message"
    assert msg["data"] == "tier-1-reasoning"
    await pubsub.unsubscribe(TiersConfigStore.CONFIG_CHANGE_CHANNEL)


async def test_all_tier_names_lists_every_seeded_tier_sorted(fake_redis):
    store = TiersConfigStore(fake_redis, tiers_config=TiersConfig(tiers={}))
    await store.seed_from_yaml_if_empty(
        _bootstrap(**{"tier-3-local": ["ollama:llama3.2"], "tier-1-reasoning": ["openai:gpt-5.4"]})
    )
    assert await store.all_tier_names() == ["tier-1-reasoning", "tier-3-local"]


# -- the RCU claim itself: a FallbackRouter built before the PATCH sees it too ---


async def test_a_fallback_router_constructed_before_a_patch_still_sees_the_update(fake_redis):
    """
    The single most important test in this file: proves app/resilience/
    fallback.py needed ZERO changes for this phase. `FallbackRouter` is
    built ONCE, at the top, holding a reference to `shared` -- exactly the
    way app/main.py's lifespan builds it once at boot. A PATCH arriving
    later (via `TiersConfigStore.update_tier`, exactly as the Admin API
    would drive it) must be visible to that SAME, already-constructed
    router with no reconstruction and no code change to `resolve_chain()`.
    """
    shared = TiersConfig(tiers={})
    store = TiersConfigStore(fake_redis, tiers_config=shared)
    await store.seed_from_yaml_if_empty(_bootstrap(**{"tier-1-reasoning": ["openai:gpt-5.4"]}))
    await store.refresh()

    router = FallbackRouter(
        circuit_breaker=AlwaysClosedCircuitBreaker(),
        retry_policy=RetryPolicy(max_attempts=1),
        tiers_config=shared,
    )

    assert router.resolve_chain("tier-1-reasoning") == ["openai:gpt-5.4"]

    await store.update_tier("tier-1-reasoning", ["anthropic:claude-sonnet-5", "ollama:llama3.2"])

    # Same router instance, zero reconstruction, zero changes to
    # resolve_chain()'s own code -- yet it now sees the new chain.
    assert router.resolve_chain("tier-1-reasoning") == ["anthropic:claude-sonnet-5", "ollama:llama3.2"]


async def test_a_literal_model_id_is_unaffected_by_any_of_this(fake_redis):
    """Regression guard: resolve_chain()'s existing one-link-chain fallback
    for a literal "provider:model" id (never a tier name) must still work
    unchanged once TiersConfigStore is in the picture."""
    shared = TiersConfig(tiers={})
    store = TiersConfigStore(fake_redis, tiers_config=shared)
    await store.refresh()  # empty table, nothing seeded

    router = FallbackRouter(
        circuit_breaker=AlwaysClosedCircuitBreaker(),
        retry_policy=RetryPolicy(max_attempts=1),
        tiers_config=shared,
    )
    assert router.resolve_chain("openai:gpt-5.4") == ["openai:gpt-5.4"]
