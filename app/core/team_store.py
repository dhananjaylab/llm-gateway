"""
TeamConfigStore: the Phase 2 replacement for Phase 1's "load teams.yaml
once at startup, never touch it again."

Design (see Phase 2 README, "Config hot-reload" section, for the full
rationale): Redis is the runtime source of truth for team config from the
moment the process boots. `config/teams.yaml` is only the *bootstrap
seed* — `seed_from_yaml_if_empty()` writes it into Redis once, the first
time a fresh deployment starts with no `team_config:*` keys present.
After that, the Admin API (app/api/admin.py) is the only mutation path,
and every gateway instance reads live from Redis.

Redis key schema (extends Document 05's `team_config:{team_id}` HASH):

    team_config:{team_id}        HASH   team_id, api_key_hash, allowed_models
                                         (JSON), rpm_cap, tpm_cap,
                                         budget_cap_usd, budget_period,
                                         priority_tier, policy (JSON)
    team_config:__index__        SET    every known team_id (membership
                                         check for "is Redis empty?" and
                                         enumeration)
    team_key_index:{api_key_hash} STRING team_id  (secondary index so auth
                                         doesn't have to scan every team on
                                         every request — not spelled out
                                         verbatim in Document 05 but a
                                         direct, necessary consequence of
                                         moving the hashed-key lookup from
                                         an in-memory dict scan to Redis)

Hot-reload mechanics: `update_team()` writes Redis, invalidates this
process's local cache immediately, and PUBLISHes on
`gateway:config:changed` so every *other* gateway instance's background
listener (see app/main.py's `_listen_for_config_changes`) invalidates its
own cache within one message round-trip — no polling, no restart. The
local cache itself is a short (2s) TTL read-through cache purely to keep
the hot path (every request calls `get_team`/`team_by_api_key_hash`) off
a Redis round-trip; it is not the source of truth.
"""

from __future__ import annotations

import json
import logging
import time

from redis.asyncio import Redis

from app.core.config import GatewayConfig, TeamConfig, TeamPolicy

logger = logging.getLogger("gateway.team_store")

_TEAM_IDS_KEY = "team_config:__index__"
_CONFIG_CHANGE_CHANNEL = "gateway:config:changed"


def _team_key(team_id: str) -> str:
    return f"team_config:{team_id}"


def _key_index_key(api_key_hash: str) -> str:
    return f"team_key_index:{api_key_hash}"


class TeamConfigStore:
    CACHE_TTL_SECONDS = 2.0
    CONFIG_CHANGE_CHANNEL = _CONFIG_CHANGE_CHANNEL

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._cache: dict[str, tuple[float, TeamConfig]] = {}

    # -- seeding ---------------------------------------------------------

    async def seed_from_yaml_if_empty(self, gateway_config: GatewayConfig) -> int:
        """Idempotent: writes nothing (returns 0) if any team already exists."""
        existing = await self._redis.scard(_TEAM_IDS_KEY)
        if existing:
            logger.info("team_config already present in Redis (%d teams) — skipping seed", existing)
            return 0
        for team in gateway_config.teams.values():
            await self._write_team(team)
        logger.info("seeded %d teams from teams.yaml into Redis", len(gateway_config.teams))
        return len(gateway_config.teams)

    async def _write_team(self, team: TeamConfig) -> None:
        key = _team_key(team.team_id)
        pipe = self._redis.pipeline(transaction=True)
        pipe.hset(
            key,
            mapping={
                "team_id": team.team_id,
                "api_key_hash": team.api_key_hash,
                "allowed_models": json.dumps(team.allowed_models),
                "rpm_cap": team.rpm_cap,
                "tpm_cap": team.tpm_cap,
                "budget_cap_usd": team.budget_cap_usd,
                "budget_period": team.budget_period,
                "priority_tier": team.priority_tier,
                "policy": team.policy.model_dump_json(),
                "org_id": team.org_id,
            },
        )
        pipe.sadd(_TEAM_IDS_KEY, team.team_id)
        pipe.set(_key_index_key(team.api_key_hash), team.team_id)
        await pipe.execute()

    @staticmethod
    def _deserialize(raw: dict) -> TeamConfig:
        return TeamConfig(
            team_id=raw["team_id"],
            api_key_hash=raw["api_key_hash"],
            allowed_models=json.loads(raw.get("allowed_models") or "[]"),
            rpm_cap=int(raw.get("rpm_cap", 60)),
            tpm_cap=int(raw.get("tpm_cap", 100_000)),
            budget_cap_usd=float(raw.get("budget_cap_usd", 50.0)),
            budget_period=raw.get("budget_period", "monthly"),
            priority_tier=raw.get("priority_tier", "realtime"),
            policy=TeamPolicy.model_validate_json(raw.get("policy") or "{}"),
            # Phase 8: absent for any team written to Redis before Phase 8
            # shipped — falls back to TeamConfig's own DEFAULT_ORG_ID
            # default via config.py's import, kept local here to avoid a
            # second import just for the one constant.
            org_id=raw.get("org_id") or "default-org",
        )

    # -- reads -------------------------------------------------------------

    async def get_team(self, team_id: str, *, use_cache: bool = True) -> TeamConfig | None:
        if use_cache:
            cached = self._cache.get(team_id)
            if cached is not None and (time.monotonic() - cached[0]) < self.CACHE_TTL_SECONDS:
                return cached[1]

        raw = await self._redis.hgetall(_team_key(team_id))
        if not raw:
            self._cache.pop(team_id, None)
            return None

        team = self._deserialize(raw)
        self._cache[team_id] = (time.monotonic(), team)
        return team

    async def team_by_api_key_hash(self, api_key_hash: str) -> TeamConfig | None:
        team_id = await self._redis.get(_key_index_key(api_key_hash))
        if not team_id:
            return None
        return await self.get_team(team_id)

    async def all_team_ids(self) -> list[str]:
        members = await self._redis.smembers(_TEAM_IDS_KEY)
        return sorted(members)

    # -- writes (Admin API) ------------------------------------------------

    async def update_team(self, team_id: str, patch: dict) -> TeamConfig:
        """
        Partial update. Raises KeyError if the team doesn't exist — the
        Admin API turns that into a 404, never silently creates a team via
        PATCH (team provisioning is a distinct, deliberately out-of-scope-
        for-Phase-2 operation; see README "What's still stubbed").
        """
        current = await self.get_team(team_id, use_cache=False)
        if current is None:
            raise KeyError(team_id)

        updated = current.model_copy(update=patch)
        await self._write_team(updated)
        self.invalidate(team_id)

        try:
            await self._redis.publish(self.CONFIG_CHANGE_CHANNEL, team_id)
        except Exception:  # pragma: no cover - defensive, publish is best-effort
            logger.warning("failed to publish config-change event for %s", team_id, exc_info=True)

        return updated

    def invalidate(self, team_id: str | None = None) -> None:
        if team_id is None:
            self._cache.clear()
        else:
            self._cache.pop(team_id, None)
