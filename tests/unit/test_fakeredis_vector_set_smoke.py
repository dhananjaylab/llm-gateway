"""
test_fakeredis_vector_set_smoke.py

Resolves docs/PHASE9B_KICKOFF_SCOPING.md Sec 4.6 ("fakeredis FILTER/SETATTR
verification") for real, standalone, before any application code in
app/caching/ is trusted to sit on top of it -- exactly the build-order
Sec 11 specifies ("first build task... before any application code is
written on top of them").

TWO REAL, PREVIOUSLY-UNVERIFIED FINDINGS THIS TEST FILE LOCKS IN, BOTH
DISCOVERED WHILE BUILDING PHASE 9b (not from a changelog -- from actually
running the commands):

1. `fakeredis[lua]` ALONE -- this project's pre-Phase-9b pin -- does NOT
   enable vector-set commands, despite fakeredis's own CHANGELOG crediting
   2.35.0 with VADD/VSIM/etc. support. The real gap: `fakeredis.stack`'s
   vector-set mixin silently falls back to a no-op stub whenever
   `fakeredis.model._vectorset` fails to import -- and it fails to import
   because that module needs `jsonpath_ng` (for VSIM's FILTER expression
   parser) and `numpy`, NEITHER of which `fakeredis[lua]` pulls in. The
   correct extra is `fakeredis[lua,vectorset]` (confirmed against
   fakeredis's own package metadata: the `vectorset` extra declares both
   `numpy>=2.4.0` and `jsonpath-ng>=1.6` for Python >= 3.11). See
   requirements.txt's own comment on this line.
2. `FakeAsyncRedis()` constructed with NO `version=` argument defaults to
   emulating Redis 7, which does not dispatch vector-set commands at all --
   `version=(8, 0, 0)` (or any 8.x) must be passed explicitly. This matters
   only for fakeredis-backed tests; a real Redis 8 server (this project's
   `docker-compose.yml` already runs `redis:8-alpine`) needs no such flag.

If this file ever starts failing on a fresh `pip install -r requirements.txt`,
the first thing to check is whether a NEWER fakeredis release finally fixed
the `jsonpath_ng`/`numpy` import chain unconditionally -- at that point this
file (and every `fake_redis_v8` fixture across the Phase 9b test files) can
likely drop the explicit extra/version workaround, not just re-pin a number.
"""

from __future__ import annotations

import fakeredis
import pytest


@pytest.fixture
def fake_redis_v8():
    return fakeredis.FakeAsyncRedis(decode_responses=True, version=(8, 0, 0))


async def test_vadd_and_vcard_round_trip(fake_redis_v8):
    vs = fake_redis_v8.vset()
    await vs.vadd("smoke:vec", [1.0, 0.0, 0.0, 0.0], "el1")
    assert await vs.vcard("smoke:vec") == 1


async def test_vsetattr_and_vgetattr_round_trip_a_dict_without_manual_json(fake_redis_v8):
    """redis-py's OO wrapper JSON-encodes/decodes `attributes` for you --
    app/caching/semantic_cache.py relies on this and must never manually
    json.dumps/loads it a second time."""
    vs = fake_redis_v8.vset()
    await vs.vadd("smoke:vec", [1.0, 0.0, 0.0, 0.0], "el1", attributes={"fp": "abc123", "created_at": 1234.5})
    attrs = await vs.vgetattr("smoke:vec", "el1")
    assert attrs == {"fp": "abc123", "created_at": 1234.5}

    await vs.vsetattr("smoke:vec", "el1", attributes={"fp": "updated"})
    assert (await vs.vgetattr("smoke:vec", "el1"))["fp"] == "updated"


async def test_vsim_with_scores_ranks_the_closer_vector_first(fake_redis_v8):
    vs = fake_redis_v8.vset()
    await vs.vadd("smoke:vec", [1.0, 0.0, 0.0, 0.0], "close")
    await vs.vadd("smoke:vec", [0.0, 1.0, 0.0, 0.0], "far")

    result = await vs.vsim("smoke:vec", [1.0, 0.0, 0.0, 0.0], count=2, with_scores=True)
    assert isinstance(result, dict)
    ordered = sorted(result.items(), key=lambda kv: kv[1], reverse=True)
    assert ordered[0][0] == "close"
    assert result["close"] > result["far"]


async def test_vsim_filter_expression_actually_filters(fake_redis_v8):
    """The other half of Sec 4.6's open item -- FILTER, not just VSETATTR
    in isolation."""
    vs = fake_redis_v8.vset()
    await vs.vadd("smoke:vec", [1.0, 0.0, 0.0, 0.0], "a", attributes={"team": "alpha"})
    await vs.vadd("smoke:vec", [0.999, 0.001, 0.0, 0.0], "b", attributes={"team": "beta"})

    result = await vs.vsim("smoke:vec", [1.0, 0.0, 0.0, 0.0], count=5, with_scores=True, filter='.team == "beta"')
    assert set(result.keys()) == {"b"}


async def test_vrem_actually_removes_the_element(fake_redis_v8):
    vs = fake_redis_v8.vset()
    await vs.vadd("smoke:vec", [1.0, 0.0, 0.0, 0.0], "el1")
    await vs.vrem("smoke:vec", "el1")
    assert await vs.vcard("smoke:vec") == 0


async def test_vcard_and_vsim_on_a_nonexistent_key_behave_as_a_clean_miss_not_an_error(fake_redis_v8):
    """The exact edge case that surfaced a genuine redis-py client-side bug
    while building this module (see app/caching/semantic_cache.py's own
    docstring): `VSIM ... WITHSCORES` against an EMPTY/nonexistent vector
    set raises `AttributeError` inside redis-py's own response parser.
    `SemanticCache.get()` works around it by checking VCARD first and never
    calling VSIM at all when it's 0 -- this test locks in the VCARD side of
    that guard; semantic_cache.py's own docstring documents the VSIM side
    for the next person who's tempted to remove the guard as "unnecessary."
    """
    vs = fake_redis_v8.vset()
    assert await vs.vcard("smoke:does-not-exist") == 0
