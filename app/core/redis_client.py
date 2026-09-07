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
from redis.asyncio.connection import ConnectionPool


def build_redis_client(
    redis_url: str,
    socket_timeout: float = 5.0,
    socket_connect_timeout: float = 5.0,
    max_connections: int = 50,
) -> Redis:
    # Use explicit connection pool for better control over resource limits
    pool = ConnectionPool.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=socket_connect_timeout,
        socket_timeout=socket_timeout,
        health_check_interval=30,
        max_connections=max_connections,
        retry_on_timeout=True,
    )
    return Redis(connection_pool=pool)
