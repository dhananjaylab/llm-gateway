"""
OrgConfigStore: the org-level counterpart to app/core/team_store.py's
TeamConfigStore — Phase 8, Option A (docs/PHASE8_KICKOFF_SCOPING.md §6).

Deliberately mirrors TeamConfigStore's design exactly rather than
inventing a new pattern: Redis is the runtime source of truth for org
config from the moment the process boots; config/orgs.yaml is only the
bootstrap seed, written into Redis once on a fresh deployment
(`seed_from_yaml_if_empty`); the Admin API
(`GET/PATCH /admin/orgs/{org_id}`) is the only mutation path after that;
and a short (2s) TTL local read-through cache keeps the hot request path
(every request calls `get_org`) off a Redis round-trip without becoming
the source of truth itself.

Simpler than TeamConfigStore in one respect: orgs aren't authenticated
directly (a request always authenticates as a team, never as an org), so
there is no `api_key_hash` / secondary key-index concept here at all —
just `org_config:{org_id}` HASH rows plus an `org_config:__index__` SET
for enumeration.

Redis key schema:

    org_config:{org_id}      HASH   org_id, rpm_cap, tpm_cap,
                                     budget_cap_usd, budget_period
    org_config:__index__     SET    every known org_id

Hot-reload mechanics are identical to TeamConfigStore's: `update_org()`
writes Redis, invalidates this process's local cache immediately, and
PUBLISHes on `gateway:orgconfig:changed` so every other gateway
instance's background listener invalidates its own cache within one
message round-trip.
"""

from __future__ import annotations

import logging
import time

from redis.asyncio import Redis

from app.core.config import DEFAULT_ORG_ID, GatewayOrgsConfig, OrgConfig

logger = logging.getLogger("gateway.org_store")

_ORG_IDS_KEY = "org_config:__index__"
_CONFIG_CHANGE_CHANNEL = "gateway:orgconfig:changed"


def _org_key(org_id: str) -> str:
    return f"org_config:{org_id}"


class OrgConfigStore:
    CACHE_TTL_SECONDS = 2.0
    CONFIG_CHANGE_CHANNEL = _CONFIG_CHANGE_CHANNEL

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._cache: dict[str, tuple[float, OrgConfig]] = {}

    # -- seeding ---------------------------------------------------------

    async def seed_from_yaml_if_empty(self, orgs_config: GatewayOrgsConfig) -> int:
        """Idempotent: writes nothing (returns 0) if any org already
        exists. Unlike TeamConfigStore's seeding, this ALSO runs a
        cheap top-up: if `default-org` specifically is somehow still
        missing (e.g. an existing Redis from before Phase 8 that has
        other org rows but not this one — not expected in practice, but
        cheap to guard), it is written on its own so org-level
        enforcement is never silently absent for teams that default to
        it."""
        existing = await self._redis.scard(_ORG_IDS_KEY)
        if existing:
            has_default = await self._redis.sismember(_ORG_IDS_KEY, DEFAULT_ORG_ID)
            if not has_default and DEFAULT_ORG_ID in orgs_config.orgs:
                await self._write_org(orgs_config.orgs[DEFAULT_ORG_ID])
                logger.info("top-up seeded missing %s into an existing org_config set", DEFAULT_ORG_ID)
                return 1
            logger.info("org_config already present in Redis (%d orgs) — skipping seed", existing)
            return 0
        for org in orgs_config.orgs.values():
            await self._write_org(org)
        logger.info("seeded %d org(s) from orgs.yaml into Redis", len(orgs_config.orgs))
        return len(orgs_config.orgs)

    async def _write_org(self, org: OrgConfig) -> None:
        key = _org_key(org.org_id)
        pipe = self._redis.pipeline(transaction=True)
        pipe.hset(
            key,
            mapping={
                "org_id": org.org_id,
                "rpm_cap": org.rpm_cap,
                "tpm_cap": org.tpm_cap,
                "budget_cap_usd": org.budget_cap_usd,
                "budget_period": org.budget_period,
            },
        )
        pipe.sadd(_ORG_IDS_KEY, org.org_id)
        await pipe.execute()

    @staticmethod
    def _deserialize(raw: dict) -> OrgConfig:
        return OrgConfig(
            org_id=raw["org_id"],
            rpm_cap=int(raw.get("rpm_cap", 1000)),
            tpm_cap=int(raw.get("tpm_cap", 500_000)),
            budget_cap_usd=float(raw.get("budget_cap_usd", 5000.0)),
            budget_period=raw.get("budget_period", "monthly"),
        )

    # -- reads -------------------------------------------------------------

    async def get_org(self, org_id: str, *, use_cache: bool = True) -> OrgConfig | None:
        if use_cache:
            cached = self._cache.get(org_id)
            if cached is not None and (time.monotonic() - cached[0]) < self.CACHE_TTL_SECONDS:
                return cached[1]

        raw = await self._redis.hgetall(_org_key(org_id))
        if not raw:
            self._cache.pop(org_id, None)
            return None

        org = self._deserialize(raw)
        self._cache[org_id] = (time.monotonic(), org)
        return org

    async def all_org_ids(self) -> list[str]:
        members = await self._redis.smembers(_ORG_IDS_KEY)
        return sorted(members)

    # -- writes (Admin API) ------------------------------------------------

    async def update_org(self, org_id: str, patch: dict) -> OrgConfig:
        """Partial update. Raises KeyError if the org doesn't exist — the
        Admin API turns that into a 404, matching TeamConfigStore's own
        update_team() contract exactly (no org auto-creation via PATCH)."""
        current = await self.get_org(org_id, use_cache=False)
        if current is None:
            raise KeyError(org_id)

        updated = current.model_copy(update=patch)
        await self._write_org(updated)
        self.invalidate(org_id)

        try:
            await self._redis.publish(self.CONFIG_CHANGE_CHANNEL, org_id)
        except Exception:  # pragma: no cover - defensive, publish is best-effort
            logger.warning("failed to publish org config-change event for %s", org_id, exc_info=True)

        return updated

    def invalidate(self, org_id: str | None = None) -> None:
        if org_id is None:
            self._cache.clear()
        else:
            self._cache.pop(org_id, None)
