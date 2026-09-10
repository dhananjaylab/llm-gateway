"""
PricingStore: hot-reload extension for config/pricing.yaml (Phase 9a,
docs/PHASE9A_IMPLEMENTATION_GUIDE.md).

Mirrors `TiersConfigStore`'s RCU design (see that module's docstring for the
full reasoning on why a per-key TTL cache is the wrong shape for a table
that's scanned, not looked up by a single key, on every request) — with one
sharper constraint that has to be gotten right.

`app.state.pricing` is a bare `dict[str, ModelPricing]`, not wrapped in a
Pydantic model, and `FallbackRouter.__init__` CAPTURES A REFERENCE to that
exact dict object (`self._pricing_table = pricing_table`) at construction
time, used later by `_cost_usd_or_none()`. If `refresh()` did
`app.state.pricing = new_dict` — reassigning to a brand-new dict object —
`FallbackRouter` would keep reading the STALE one forever: a real, quiet bug
class (cost figures would go wrong with no error anywhere to notice it by).

`refresh()` therefore mutates the SAME dict object in place:

    table.clear()
    table.update(new_entries)

with no `await` between those two lines, so no concurrent coroutine can ever
observe an empty table mid-refresh. Every existing call site — `v1_chat.py`'s
per-request `pricing_table = app_state.pricing` read, `fallback.py`'s
captured `self._pricing_table`, and every test that does
`client.app.state.pricing["fake:served-model"] = ModelPricing(...)` — keeps
working completely unmodified, because they are all holding (or re-reading)
the identity of the same dict object. `app/core/pricing.py`
(`calculate_cost_usd`, `_lookup`) needs ZERO changes either — it already
takes a `PricingTable` dict fresh on every call.

Redis key schema:

    pricing_config:{model_key}     HASH    input_per_million,
                                            output_per_million,
                                            cache_read_per_million,
                                            cache_write_per_million
                                            (the latter two omitted from the
                                            hash entirely when unset, mirroring
                                            config/pricing.yaml's own
                                            "omit, don't zero-cost" convention
                                            — see that file's header comment)
    pricing_config:__index__       SET     every known model_key, including
                                            the "ollama:" empty-suffix
                                            fallback row — a valid, if
                                            unusual, SET member
    gateway:pricingconfig:changed  PubSub  channel

Same "PATCH only mutates an existing row" contract as tiers/teams/orgs: a
brand-new model_key still needs a config/pricing.yaml edit + a fresh Redis.
"""

from __future__ import annotations

import logging

from redis.asyncio import Redis

from app.core.pricing import ModelPricing, PricingTable

logger = logging.getLogger("gateway.pricing_store")

_PRICING_IDS_KEY = "pricing_config:__index__"
_CONFIG_CHANGE_CHANNEL = "gateway:pricingconfig:changed"


def _pricing_key(model_key: str) -> str:
    return f"pricing_config:{model_key}"


class PricingStore:
    CONFIG_CHANGE_CHANNEL = _CONFIG_CHANGE_CHANNEL

    def __init__(self, redis: Redis, *, table: PricingTable) -> None:
        self._redis = redis
        # The exact dict object app.state.pricing / FallbackRouter's
        # captured self._pricing_table both point at. Mutated in place,
        # never replaced — see module docstring.
        self._table = table

    # -- seeding -------------------------------------------------------------

    async def seed_from_yaml_if_empty(self, bootstrap: PricingTable) -> int:
        existing = await self._redis.scard(_PRICING_IDS_KEY)
        if existing:
            logger.info("pricing_config already present in Redis (%d rows) — skipping seed", existing)
            return 0
        for model_key, pricing in bootstrap.items():
            await self._write_pricing(model_key, pricing)
        logger.info("seeded %d pricing row(s) from pricing.yaml into Redis", len(bootstrap))
        return len(bootstrap)

    async def _write_pricing(self, model_key: str, pricing: ModelPricing) -> None:
        mapping: dict[str, float] = {
            "input_per_million": pricing.input_per_million,
            "output_per_million": pricing.output_per_million,
        }
        if pricing.cache_read_per_million is not None:
            mapping["cache_read_per_million"] = pricing.cache_read_per_million
        if pricing.cache_write_per_million is not None:
            mapping["cache_write_per_million"] = pricing.cache_write_per_million
        pipe = self._redis.pipeline(transaction=True)
        pipe.hset(_pricing_key(model_key), mapping=mapping)
        pipe.sadd(_PRICING_IDS_KEY, model_key)
        await pipe.execute()

    @staticmethod
    def _deserialize(raw: dict) -> ModelPricing:
        return ModelPricing(
            input_per_million=float(raw.get("input_per_million", 0.0)),
            output_per_million=float(raw.get("output_per_million", 0.0)),
            cache_read_per_million=(
                float(raw["cache_read_per_million"])
                if raw.get("cache_read_per_million") is not None
                else None
            ),
            cache_write_per_million=(
                float(raw["cache_write_per_million"])
                if raw.get("cache_write_per_million") is not None
                else None
            ),
        )

    # -- the RCU refresh -------------------------------------------------------

    async def refresh(self) -> None:
        """Reads the ENTIRE table from Redis and mutates `self._table` in
        place. Called once at boot (right after seeding) and once per
        pub/sub change event — never per-request."""
        model_keys = await self._redis.smembers(_PRICING_IDS_KEY)
        new_entries: PricingTable = {}
        for model_key in model_keys:
            raw = await self._redis.hgetall(_pricing_key(model_key))
            if raw:
                new_entries[model_key] = self._deserialize(raw)
        # In-place mutation, NOT reassignment — see module docstring. No
        # `await` between clear() and update(), so no coroutine can ever
        # observe an empty pricing table mid-refresh.
        self._table.clear()
        self._table.update(new_entries)

    # -- writes (Admin API) ----------------------------------------------------

    async def update_pricing(self, model_key: str, patch: dict) -> ModelPricing:
        """Raises KeyError if the model_key doesn't exist. `patch` is a
        dict of already-validated, present-only fields (mirrors every other
        `*Patch.model_dump(exclude_none=True)` call site in this codebase)."""
        exists = await self._redis.sismember(_PRICING_IDS_KEY, model_key)
        if not exists:
            raise KeyError(model_key)

        current_raw = await self._redis.hgetall(_pricing_key(model_key))
        current = self._deserialize(current_raw)
        updated = ModelPricing(
            input_per_million=patch.get("input_per_million", current.input_per_million),
            output_per_million=patch.get("output_per_million", current.output_per_million),
            cache_read_per_million=patch.get("cache_read_per_million", current.cache_read_per_million),
            cache_write_per_million=patch.get("cache_write_per_million", current.cache_write_per_million),
        )
        await self._write_pricing(model_key, updated)
        await self.refresh()

        try:
            await self._redis.publish(self.CONFIG_CHANGE_CHANNEL, model_key)
        except Exception:  # pragma: no cover - defensive
            logger.warning("failed to publish pricing config-change event for %s", model_key, exc_info=True)

        return updated

    async def all_model_keys(self) -> list[str]:
        members = await self._redis.smembers(_PRICING_IDS_KEY)
        return sorted(members)
