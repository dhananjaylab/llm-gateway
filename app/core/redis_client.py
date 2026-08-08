"""
Async Redis client construction.

One place that decides connection parameters, so app/main.py's lifespan
and any script (scripts/seed_teams.py) build a client the same way.
`decode_responses=True` matters: every consumer in this codebase (Lua
script args/returns, HGETALL results, pub/sub messages) works with `str`,
not `bytes` — flipping this flag would break `team_store.py`'s
`hgetall()` parsing silently.
"""

from __future__ import annotations

from redis.asyncio import Redis


def build_redis_client(redis_url: str) -> Redis:
    return Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
        health_check_interval=30,
    )
