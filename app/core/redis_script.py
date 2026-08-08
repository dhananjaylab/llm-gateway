"""
LuaScript: a small, hand-rolled EVALSHA/NOSCRIPT-resilient wrapper.

Per the TRD's Phase 2 Concise Implementation Guide: "Wrap EVALSHA in a
limiter.py client that caches the script SHA at startup and retries once
via EVAL + SCRIPT LOAD on a NOSCRIPT error — this is the standard EVALSHA
resilience pattern and avoids a hard dependency on Redis never restarting
its script cache."

redis-py ships a higher-level `Redis.register_script()` convenience that
does the same thing internally, but building it explicitly here is what
the TRD instructs and is worth the ~20 lines: it's the actual mechanism
every distributed rate limiter needs to reason about (a cache miss here
means silently falling through to a slow, still-correct EVAL path, not an
error), and it's shared by both `app/ratelimit/limiter.py` (token bucket)
and `app/ratelimit/budget.py` (spend ledger) so the pattern is written
once, not twice.
"""

from __future__ import annotations

import logging

from redis import exceptions as redis_exceptions
from redis.asyncio import Redis

logger = logging.getLogger("gateway.redis_script")


class LuaScript:
    """One compiled Lua script, callable via EVALSHA with automatic reload."""

    def __init__(self, redis: Redis, script_text: str, *, name: str) -> None:
        self._redis = redis
        self._script_text = script_text
        self._name = name
        self._sha: str | None = None

    async def _load(self) -> str:
        self._sha = await self._redis.script_load(self._script_text)
        logger.debug("loaded lua script", extra={"script": self._name, "sha": self._sha})
        return self._sha

    async def eval(self, keys: list[str], args: list):
        """
        Run the script via EVALSHA, loading it first if this is the first
        call, and transparently falling back to SCRIPT LOAD + EVALSHA
        exactly once if Redis reports NOSCRIPT (e.g. after a Redis
        restart flushed the script cache, or in a fresh test double).
        """
        if self._sha is None:
            await self._load()
        try:
            return await self._redis.evalsha(self._sha, len(keys), *keys, *args)
        except redis_exceptions.NoScriptError:
            logger.warning(
                "NOSCRIPT for %s — reloading and retrying once", self._name
            )
            await self._load()
            return await self._redis.evalsha(self._sha, len(keys), *keys, *args)
