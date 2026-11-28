"""
Optional background memory-hygiene sweep (Phase 9b,
docs/PHASE9B_KICKOFF_SCOPING.md Sec 6/10) -- same `asyncio.create_task`
lifecycle shape `app/resilience/health.py`'s `HealthChecker` already
established in `app/main.py`'s lifespan.

NOT a correctness dependency: `SemanticCache.get()` already self-heals a
stale (meta-row-expired) vector element the moment it's ever looked up
again (see semantic_cache.py's own docstring). This sweeper exists purely
so a scope that stops receiving lookups for a while doesn't sit holding
dead vector-set members indefinitely just because nothing ever triggered
the lazy cleanup path -- memory hygiene under sustained load, not a
requirement for `get()`/`set()` to behave correctly. A failed sweep round
is logged and skipped, never fatal, matching this codebase's established
"log loudly, don't take the whole system down" posture for every other
optional background concern (Redis fail-open, active health probing).
"""

from __future__ import annotations

import asyncio
import logging

from redis.asyncio import Redis

logger = logging.getLogger("gateway.caching.sweep")


class CacheSweeper:
    def __init__(self, redis: Redis, *, interval_seconds: float = 300.0) -> None:
        self._redis = redis
        self._interval = interval_seconds

    async def run_forever(self) -> None:
        logger.info("cache sweeper starting: sweeping every %.0fs", self._interval)
        try:
            while True:
                await self._run_one_round()
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            logger.info("cache sweeper stopping")
            raise

    async def _run_one_round(self) -> None:
        try:
            swept = 0
            async for vec_key in self._redis.scan_iter(match="cache:sem:*", count=200):
                if vec_key.endswith(":meta") or vec_key.endswith(":count"):
                    continue
                scope = vec_key.removeprefix("cache:sem:")
                meta_key = f"cache:sem:{scope}:meta"
                elements = await self._redis.vset().vrange(vec_key, 0, -1) if hasattr(
                    self._redis.vset(), "vrange"
                ) else []
                for element_id in elements:
                    if await self._redis.hexists(meta_key, element_id):
                        continue
                    await self._redis.vset().vrem(vec_key, element_id)
                    await self._redis.decr(f"cache:sem:{scope}:count")
                    swept += 1
            if swept:
                logger.info("cache sweeper: removed %d stale vector element(s)", swept)
        except Exception:  # pragma: no cover - defensive, a sweep failure must never crash the loop
            logger.warning("cache sweeper: round failed, will retry next interval", exc_info=True)
