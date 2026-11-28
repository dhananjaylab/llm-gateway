"""
Semantic (embedding-similarity) cache layer -- Layer 1, checked only after
`ExactMatchCache` misses (Phase 9b, docs/PHASE9B_KICKOFF_SCOPING.md Sec 3.2
/ Sec 6 / Sec 10).

Built on Redis 8's native vector-set type (`VADD`/`VSIM`/`VSETATTR`/`VREM`),
used nowhere else in this codebase. Two Redis keys per scope, not one:

    cache:sem:{scope}         VECTOR SET   element -> embedding + a SMALL
                                            JSON attribute blob (VSETATTR)
    cache:sem:{scope}:meta    HASH         element -> the actual cached
                                            payload, HEXPIRE'd per field

DESIGN-CRITICAL: WHY TWO KEYS, NOT ONE -- READ BEFORE CHANGING THIS FILE.
Redis 8 vector-set members have NO per-member TTL of their own (verified
directly against `fakeredis`'s own implementation while building this
module -- `VADD` has no TTL option, and a vector set is one Redis key
holding an entire HNSW graph, so only the whole key can `EXPIRE`, never one
member of it). If the cached response body lived inside `VSETATTR`
directly, there would be no way to expire one entry without expiring every
entry in the scope. The companion `:meta` HASH is the actual source of
truth for whether an entry is alive, via real per-field `HEXPIRE` (same
mechanism `exact_cache.py` uses) -- `VSETATTR` carries only a tiny
fingerprint/timestamp, cheap enough that `VSIM`'s FILTER expression parser
never has to look at anything bulky.

WRITE ORDERING MATTERS: the meta row is written FIRST, then `VADD` second.
A reader that raced in between would see either "nothing yet" (correct: a
miss) or "both present" (correct: a hit) -- never a vector with no meta
row, which is the one state `get()` cannot safely treat as anything but
"stale, clean it up" (see below). Writing `VADD` first would allow the
opposite, unsafe race.

LAZY, SELF-HEALING EXPIRY: `VSIM` can and will return a vector match whose
meta row has already `HEXPIRE`d away (the vector set has no idea the HASH
row aged out -- they are two independent Redis keys). `get()` treats a
missing meta row as a miss AND opportunistically `VREM`s the stale vector
element on the way out, so a scope's vector set doesn't accumulate
permanently-dead entries just because nothing else ever swept them. This
is correctness-load-bearing, not a nice-to-have: without it, a sufficiently
old entry would keep scoring as a false "hit" against `VSIM` forever, past
its own advertised TTL.

No `FILTER` is used here -- isolation is structural (the scope IS the key
name, per the kickoff doc's Sec 4.1), so there is nothing left for a filter
expression to do at the tenant/policy boundary. `FILTER` stays available
for future intra-scope refinements; it just isn't reached for in v1.
"""

from __future__ import annotations

import json
import logging
import uuid

from redis.asyncio import Redis

logger = logging.getLogger("gateway.caching.semantic")


def _vec_key(scope: str) -> str:
    return f"cache:sem:{scope}"


def _meta_key(scope: str) -> str:
    return f"cache:sem:{scope}:meta"


def _count_key(scope: str) -> str:
    return f"cache:sem:{scope}:count"


class SemanticCache:
    def __init__(self, redis: Redis, *, ttl_seconds: int, similarity_threshold: float, max_entries_per_scope: int) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self._threshold = similarity_threshold
        self._max_entries = max_entries_per_scope

    async def get(self, *, scope: str, vector: list[float]) -> tuple[dict, float] | None:
        """Returns (payload, similarity_score) on a hit, or None. Never
        raises on an empty/nonexistent scope -- a scope's first request
        always misses, and that has to be a normal, expected miss, not an
        error condition.

        VERIFIED CLIENT-LIBRARY QUIRK, READ BEFORE REMOVING THIS GUARD:
        `redis-py`'s own `VSIM ... WITHSCORES` response parser
        (`redis.commands.vectorset.utils.parse_vsim_result`) raises
        `AttributeError` on a genuinely EMPTY result specifically when
        `with_scores=True` -- confirmed directly against this project's
        pinned `redis`/`fakeredis` versions while building this module, not
        assumed from a changelog. A brand-new scope (nothing written yet)
        hits exactly this path on its very first lookup, which would
        otherwise be the single most common call this method ever makes.
        Checking `VCARD` first sidesteps it (an empty vector set never
        reaches `VSIM` at all) and is also strictly cheaper than running a
        similarity search against zero candidates -- not just a workaround
        for the parsing bug, a correct short-circuit regardless of it.
        """
        if await self._redis.vset().vcard(_vec_key(scope)) == 0:
            return None

        result = await self._redis.vset().vsim(_vec_key(scope), vector, count=1, with_scores=True)
        if not result:
            return None

        element_id, score = next(iter(result.items()))
        if score < self._threshold:
            return None

        raw = await self._redis.hget(_meta_key(scope), element_id)
        if raw is None:
            # Vector survived, meta row already expired -- stale. Self-heal
            # rather than leave a permanently-scoring dead entry behind;
            # best-effort (a failed VREM here must never turn a cache miss
            # into a request failure).
            logger.info("semantic cache: stale element %s in scope=%s, removing", element_id, scope)
            try:
                await self._redis.vset().vrem(_vec_key(scope), element_id)
                await self._redis.decr(_count_key(scope))
            except Exception:  # pragma: no cover - defensive, cleanup is best-effort
                logger.warning("semantic cache: cleanup of stale element failed", exc_info=True)
            return None

        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("semantic cache: corrupt meta entry for scope=%s, treating as a miss", scope)
            return None

        return payload, float(score)

    async def set(self, *, scope: str, vector: list[float], payload: dict) -> None:
        """Refuses (logs, does not raise) once a scope is at
        CACHE_MAX_ENTRIES_PER_SCOPE -- v1 caps rather than evicts, per the
        kickoff doc's Sec 10 ("start simple... move to something smarter
        only if it proves too twitchy", the same posture the circuit
        breaker's fixed-threshold-vs-error-rate decision already used)."""
        current = await self._redis.get(_count_key(scope))
        if current is not None and int(current) >= self._max_entries:
            logger.info("semantic cache: scope=%s at capacity (%d), skipping write", scope, self._max_entries)
            return

        element_id = f"c_{uuid.uuid4().hex[:12]}"
        meta_key = _meta_key(scope)

        # Meta row FIRST, then VADD -- see module docstring's write-ordering note.
        await self._redis.hset(meta_key, element_id, json.dumps(payload))
        await self._redis.hexpire(meta_key, self._ttl_seconds, element_id)
        await self._redis.vset().vadd(
            _vec_key(scope), vector, element_id, attributes={"created_at": payload.get("created_at")}
        )
        await self._redis.incr(_count_key(scope))

    async def purge_scope(self, *, scope: str) -> None:
        await self._redis.delete(_vec_key(scope), _meta_key(scope), _count_key(scope))
