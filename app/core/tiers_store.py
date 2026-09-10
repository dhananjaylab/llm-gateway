"""
TiersConfigStore: hot-reload extension for config/tiers.yaml (Phase 9a,
docs/PHASE9A_IMPLEMENTATION_GUIDE.md).

This is NOT a copy of TeamConfigStore's (Phase 2) or OrgConfigStore's
(Phase 8) per-key, short-TTL read-through cache — that shape is wrong here.
Team/org config is looked up by one specific id per request; tiers config is
consulted via `FallbackRouter.resolve_chain()` on the hot path of every
single chat completion, and `TiersConfig.chain_for()` needs the WHOLE table
resident to do a plain dict lookup. A per-key TTL cache would mean a Redis
round trip (or reconstructing the table from many keys) on every request —
directly working against the gateway's own <10ms overhead SLA.

Instead this store keeps the full table in memory and uses the RCU
(read-copy-update) pattern PHASE7_PLUS_ADOPTION_PLAN.md itself named for
this feature: build the new table off to the side, then swap it into the
SAME `TiersConfig` instance `FallbackRouter` already holds a reference to,
in one GIL-atomic attribute reassignment
(`self._tiers_config.tiers = new_dict`). No lock is needed — under CPython's
GIL, a single attribute set is atomic from every concurrent coroutine's
point of view, and there is no `await` between building `new_dict` and
assigning it, so no reader can ever observe a half-built table.

Net effect: `app/resilience/fallback.py` needs ZERO changes for this phase.
`resolve_chain()`'s body (`self._tiers_config.chain_for(model_id)`) is
calling the exact same method on the exact same object it always has — only
that object's internal `.tiers` dict has been hot-swapped underneath it.
Continues the byte-identical `fallback.py` streak from Phase 8 -> 8b.

Redis key schema:

    tier_config:{tier_name}      HASH    tier_name, chain (JSON list)
    tier_config:__index__        SET     every known tier_name
    gateway:tiersconfig:changed  PubSub  message body is the changed
                                          tier_name — unused by the listener
                                          beyond triggering a full refresh
                                          (see refresh(); re-reading the
                                          whole table is cheap at this
                                          table's size and keeps this store
                                          simple, avoiding a second,
                                          per-key-diff code path).

Same "PATCH only mutates an existing row" contract as every other config
store in this codebase (teams, orgs): adding a brand-new tier still needs a
config/tiers.yaml edit + a fresh Redis, consistent with that existing,
deliberate limitation — not a new one introduced here.
"""

from __future__ import annotations

import json
import logging

from redis.asyncio import Redis

from app.core.config import TiersConfig

logger = logging.getLogger("gateway.tiers_store")

_TIER_IDS_KEY = "tier_config:__index__"
_CONFIG_CHANGE_CHANNEL = "gateway:tiersconfig:changed"


def _tier_key(tier_name: str) -> str:
    return f"tier_config:{tier_name}"


class TiersConfigStore:
    CONFIG_CHANGE_CHANNEL = _CONFIG_CHANGE_CHANNEL

    def __init__(self, redis: Redis, *, tiers_config: TiersConfig) -> None:
        self._redis = redis
        # The exact instance FallbackRouter is (or will be) constructed
        # with. refresh() mutates this object's `.tiers` field in place —
        # it never replaces `self._tiers_config` itself. See module
        # docstring for why that distinction is the whole point.
        self._tiers_config = tiers_config

    # -- seeding -------------------------------------------------------------

    async def seed_from_yaml_if_empty(self, bootstrap: TiersConfig) -> int:
        """Idempotent: writes nothing (returns 0) if any tier already
        exists in Redis. Mirrors TeamConfigStore.seed_from_yaml_if_empty."""
        existing = await self._redis.scard(_TIER_IDS_KEY)
        if existing:
            logger.info("tier_config already present in Redis (%d tiers) — skipping seed", existing)
            return 0
        for tier_name, chain in bootstrap.tiers.items():
            await self._write_tier(tier_name, chain)
        logger.info("seeded %d tier(s) from tiers.yaml into Redis", len(bootstrap.tiers))
        return len(bootstrap.tiers)

    async def _write_tier(self, tier_name: str, chain: list[str]) -> None:
        pipe = self._redis.pipeline(transaction=True)
        pipe.hset(_tier_key(tier_name), mapping={"tier_name": tier_name, "chain": json.dumps(chain)})
        pipe.sadd(_TIER_IDS_KEY, tier_name)
        await pipe.execute()

    # -- the RCU refresh -------------------------------------------------------

    async def refresh(self) -> None:
        """
        Reads the ENTIRE table from Redis and swaps it into the shared
        `TiersConfig` instance in one atomic attribute assignment. Called
        once at boot (right after seeding) and once per pub/sub change
        event — never per-request.
        """
        tier_names = await self._redis.smembers(_TIER_IDS_KEY)
        new_tiers: dict[str, list[str]] = {}
        for tier_name in tier_names:
            raw = await self._redis.hget(_tier_key(tier_name), "chain")
            if raw is not None:
                new_tiers[tier_name] = json.loads(raw)
        # Single reassignment — atomic under the GIL, and there is no
        # `await` between building `new_tiers` above and this line, so no
        # concurrent `resolve_chain()` call can observe a partially-built
        # table (classic read-copy-update).
        self._tiers_config.tiers = new_tiers

    # -- writes (Admin API) ----------------------------------------------------

    async def update_tier(self, tier_name: str, chain: list[str]) -> list[str]:
        """Raises KeyError if the tier doesn't exist — the Admin API turns
        that into a 404, same contract as TeamConfigStore.update_team /
        OrgConfigStore.update_org."""
        exists = await self._redis.sismember(_TIER_IDS_KEY, tier_name)
        if not exists:
            raise KeyError(tier_name)

        await self._write_tier(tier_name, chain)
        await self.refresh()

        try:
            await self._redis.publish(self.CONFIG_CHANGE_CHANNEL, tier_name)
        except Exception:  # pragma: no cover - defensive, publish is best-effort
            logger.warning("failed to publish tier config-change event for %s", tier_name, exc_info=True)

        return chain

    async def all_tier_names(self) -> list[str]:
        members = await self._redis.smembers(_TIER_IDS_KEY)
        return sorted(members)
