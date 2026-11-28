"""
Exact-match cache layer (Phase 9b, docs/PHASE9B_KICKOFF_SCOPING.md Sec 3.2 /
Sec 6) -- Layer 0, checked before the (more expensive) embedding-similarity
layer ever runs. Zero false-positive risk by construction: a hit only ever
happens when the fingerprint (app.caching.fingerprint.request_fingerprint)
is byte-identical to a previously-cached request's.

Storage: one Redis HASH per scope (`cache:exact:{scope}`), field = the
request fingerprint, value = the serialized cache payload. Each field gets
its OWN `HEXPIRE`, not a whole-key `EXPIRE` -- Redis 8's per-hash-field TTL,
which this project's own TRD flagged as available ("Redis 8 adds HEXPIRE...
which simplifies time-bound bucket state") but never actually used until
now. This is what lets one HASH serve an entire scope's worth of distinct
cached requests, each aging out independently, instead of either a
key-per-request layout (sprawl) or a single whole-key TTL that would evict
everything in a scope at once regardless of how recently each entry was
written.
"""

from __future__ import annotations

import json
import logging

from redis.asyncio import Redis

from app.caching.fingerprint import request_fingerprint

logger = logging.getLogger("gateway.caching.exact")


def _key(scope: str) -> str:
    return f"cache:exact:{scope}"


class ExactMatchCache:
    def __init__(self, redis: Redis, *, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def get(self, *, scope: str, request) -> dict | None:
        fingerprint = request_fingerprint(request)
        raw = await self._redis.hget(_key(scope), fingerprint)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            # A malformed field should degrade to a miss, never a 500 --
            # same "corrupt/unexpected state fails open toward the request
            # still succeeding" posture this codebase already applies to a
            # circuit breaker's unknown state code.
            logger.warning("exact cache: corrupt entry for scope=%s, treating as a miss", scope)
            return None

    async def set(self, *, scope: str, request, payload: dict) -> None:
        fingerprint = request_fingerprint(request)
        key = _key(scope)
        await self._redis.hset(key, fingerprint, json.dumps(payload))
        await self._redis.hexpire(key, self._ttl_seconds, fingerprint)

    async def purge_scope(self, *, scope: str) -> None:
        await self._redis.delete(_key(scope))
