"""
test_exact_cache.py

Verifies docs/PHASE9B_KICKOFF_SCOPING.md's exact-match cache layer: set-then-
get round-trips, and a field past its HEXPIRE reads as a miss, not a stale
hit -- Redis 8's per-hash-field TTL, this codebase's first real use of it.
"""

from __future__ import annotations

import fakeredis
import pytest

from app.caching.exact_cache import ExactMatchCache
from app.core.schema import ChatMessage, UnifiedChatRequest


@pytest.fixture
def fake_redis_v8():
    # version=(8, 0, 0) is required for fakeredis to dispatch HEXPIRE/HTTL
    # (and, in semantic_cache's own test file, VADD/VSIM) at all -- its
    # default client version is 7. See docs/PHASE9B_KICKOFF_SCOPING.md's
    # build notes: this is a real, verified requirement, not a guess.
    return fakeredis.FakeAsyncRedis(decode_responses=True, version=(8, 0, 0))


def _req(text: str = "hi") -> UnifiedChatRequest:
    return UnifiedChatRequest(model="openai:gpt-5.4", messages=[ChatMessage(role="user", content=text)])


async def test_miss_on_an_empty_cache(fake_redis_v8):
    cache = ExactMatchCache(fake_redis_v8, ttl_seconds=3600)
    assert await cache.get(scope="s1", request=_req()) is None


async def test_set_then_get_round_trips(fake_redis_v8):
    cache = ExactMatchCache(fake_redis_v8, ttl_seconds=3600)
    payload = {"answer": "42", "provider": "openai", "cost_usd": 0.001}
    await cache.set(scope="s1", request=_req(), payload=payload)
    got = await cache.get(scope="s1", request=_req())
    assert got == payload


async def test_a_different_request_text_is_a_miss_even_in_the_same_scope(fake_redis_v8):
    cache = ExactMatchCache(fake_redis_v8, ttl_seconds=3600)
    await cache.set(scope="s1", request=_req("hello"), payload={"answer": "a"})
    assert await cache.get(scope="s1", request=_req("goodbye")) is None


async def test_two_scopes_never_see_each_others_entries(fake_redis_v8):
    cache = ExactMatchCache(fake_redis_v8, ttl_seconds=3600)
    await cache.set(scope="scope-a", request=_req(), payload={"answer": "from-a"})
    assert await cache.get(scope="scope-b", request=_req()) is None


async def test_an_entry_past_its_ttl_reads_as_a_miss_not_a_stale_hit(fake_redis_v8):
    cache = ExactMatchCache(fake_redis_v8, ttl_seconds=0)
    await cache.set(scope="s1", request=_req(), payload={"answer": "a"})
    # ttl_seconds=0 -> HEXPIRE fires immediately, same behavior verified
    # directly against fakeredis's HEXPIRE implementation.
    assert await cache.get(scope="s1", request=_req()) is None


async def test_purge_scope_removes_the_whole_hash(fake_redis_v8):
    cache = ExactMatchCache(fake_redis_v8, ttl_seconds=3600)
    await cache.set(scope="s1", request=_req(), payload={"answer": "a"})
    await cache.purge_scope(scope="s1")
    assert await cache.get(scope="s1", request=_req()) is None


async def test_a_corrupt_entry_degrades_to_a_miss_not_a_crash(fake_redis_v8):
    cache = ExactMatchCache(fake_redis_v8, ttl_seconds=3600)
    from app.caching.fingerprint import request_fingerprint

    fp = request_fingerprint(_req())
    await fake_redis_v8.hset("cache:exact:s1", fp, "{not valid json")
    assert await cache.get(scope="s1", request=_req()) is None
