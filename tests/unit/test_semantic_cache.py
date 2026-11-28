"""
test_semantic_cache.py

Verifies docs/PHASE9B_KICKOFF_SCOPING.md's semantic cache layer against real
Redis 8 vector-set commands under fakeredis (VADD/VSIM/VSETATTR/VREM):
nearest match above threshold is a hit, below threshold is a miss, a
meta-row-expired candidate is a miss AND self-heals (VREM), and the
write-time cap refuses new entries past CACHE_MAX_ENTRIES_PER_SCOPE.
"""

from __future__ import annotations

import fakeredis
import pytest

from app.caching.semantic_cache import SemanticCache


@pytest.fixture
def fake_redis_v8():
    return fakeredis.FakeAsyncRedis(decode_responses=True, version=(8, 0, 0))


def _cache(fake_redis_v8, **overrides) -> SemanticCache:
    defaults = dict(ttl_seconds=3600, similarity_threshold=0.97, max_entries_per_scope=5000)
    defaults.update(overrides)
    return SemanticCache(fake_redis_v8, **defaults)


async def test_miss_on_an_empty_scope(fake_redis_v8):
    cache = _cache(fake_redis_v8)
    assert await cache.get(scope="s1", vector=[1.0, 0.0, 0.0, 0.0]) is None


async def test_set_then_get_with_the_identical_vector_is_a_hit_at_score_1(fake_redis_v8):
    cache = _cache(fake_redis_v8)
    payload = {"answer": "42", "created_at": 1000.0, "max_tokens": 256, "temperature": None, "top_p": None, "stop": None, "response_format": None}
    await cache.set(scope="s1", vector=[1.0, 0.0, 0.0, 0.0], payload=payload)

    hit = await cache.get(scope="s1", vector=[1.0, 0.0, 0.0, 0.0])
    assert hit is not None
    got_payload, score = hit
    assert got_payload == payload
    assert score == pytest.approx(1.0, abs=1e-6)


async def test_a_near_but_below_threshold_vector_is_a_miss(fake_redis_v8):
    cache = _cache(fake_redis_v8, similarity_threshold=0.999999)
    payload = {"answer": "a", "created_at": 1.0}
    await cache.set(scope="s1", vector=[1.0, 0.0, 0.0, 0.0], payload=payload)

    # A clearly different direction -- cosine similarity well under any
    # sane threshold, not a borderline float-precision case.
    assert await cache.get(scope="s1", vector=[0.0, 1.0, 0.0, 0.0]) is None


async def test_two_scopes_never_see_each_others_vectors(fake_redis_v8):
    cache = _cache(fake_redis_v8)
    await cache.set(scope="scope-a", vector=[1.0, 0.0, 0.0, 0.0], payload={"answer": "from-a", "created_at": 1.0})
    assert await cache.get(scope="scope-b", vector=[1.0, 0.0, 0.0, 0.0]) is None


async def test_a_meta_expired_candidate_is_a_miss_and_self_heals(fake_redis_v8):
    """The correctness-load-bearing case from the module's own docstring:
    VSIM can still return a vector whose companion meta row already
    HEXPIRE'd away -- must be treated as a miss, and the stale vector
    element must be removed so it doesn't keep matching forever."""
    cache = _cache(fake_redis_v8, ttl_seconds=0)
    payload = {"answer": "a", "created_at": 1.0}
    await cache.set(scope="s1", vector=[1.0, 0.0, 0.0, 0.0], payload=payload)

    # Meta row is already gone (ttl_seconds=0 -> HEXPIRE fires immediately)
    # but the vector itself is still sitting in the vector set.
    card_before = await fake_redis_v8.vset().vcard("cache:sem:s1")
    assert card_before == 1

    assert await cache.get(scope="s1", vector=[1.0, 0.0, 0.0, 0.0]) is None

    card_after = await fake_redis_v8.vset().vcard("cache:sem:s1")
    assert card_after == 0, "the stale vector element must have been VREM'd during the miss"


async def test_write_time_cap_refuses_new_entries_past_the_limit(fake_redis_v8):
    cache = _cache(fake_redis_v8, max_entries_per_scope=2)
    await cache.set(scope="s1", vector=[1.0, 0.0, 0.0, 0.0], payload={"answer": "a", "created_at": 1.0})
    await cache.set(scope="s1", vector=[0.0, 1.0, 0.0, 0.0], payload={"answer": "b", "created_at": 2.0})
    # Third write should be silently skipped, not raise.
    await cache.set(scope="s1", vector=[0.0, 0.0, 1.0, 0.0], payload={"answer": "c", "created_at": 3.0})

    card = await fake_redis_v8.vset().vcard("cache:sem:s1")
    assert card == 2


async def test_purge_scope_removes_all_three_keys(fake_redis_v8):
    cache = _cache(fake_redis_v8)
    await cache.set(scope="s1", vector=[1.0, 0.0, 0.0, 0.0], payload={"answer": "a", "created_at": 1.0})
    await cache.purge_scope(scope="s1")

    assert await fake_redis_v8.vset().vcard("cache:sem:s1") == 0
    assert await fake_redis_v8.exists("cache:sem:s1:meta") == 0
    assert await fake_redis_v8.exists("cache:sem:s1:count") == 0
    assert await cache.get(scope="s1", vector=[1.0, 0.0, 0.0, 0.0]) is None
