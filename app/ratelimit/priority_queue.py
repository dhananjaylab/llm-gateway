"""
Tiered priority (TRD, "Tiered Priority Queuing"; PRD: "Priority tiers so
high-priority (real-time) traffic is served ahead of batch traffic under
load").

Scope, matching the TRD's own instruction ("a Redis LIST... is sufficient
for v1; a full priority-queue broker is out of scope"): this is
queueing behavior, not a separate reserved-capacity pool. `normal`/`high`
requests always call `RateLimiter.check()` directly and get an immediate
allow/deny — they are never queued and never wait. `batch` requests that
get denied are instead pushed onto a per-team Redis LIST and retried on a
short poll loop for a bounded window, so a Batch request only fails
outright if capacity doesn't free up within that window (default 2s,
`BATCH_QUEUE_MAX_WAIT_SECONDS`) — "delay... rather than rejecting
outright" per the TRD.

This does NOT carve out a capacity reservation exclusively for
High/Normal (e.g. "batch may only use 70% of the bucket") — all three
priorities share one team-level token bucket. Under sustained Batch
pressure a High-priority request issued at the exact instant the bucket
is empty will still see that empty bucket; it just isn't made to wait
behind a queue the way Batch is. A true priority-weighted capacity split
is flagged as a natural v2 extension, not built here, consistent with the
TRD's explicit v1 scope cut.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from redis.asyncio import Redis

from app.core.config import TeamConfig
from app.ratelimit.limiter import RateLimitDecision, RateLimiter

logger = logging.getLogger("gateway.priority_queue")


class BatchPriorityQueue:
    QUEUE_KEY_TMPL = "priqueue:batch:{team_id}"

    def __init__(
        self,
        redis: Redis,
        *,
        max_wait_seconds: float = 2.0,
        poll_interval_seconds: float = 0.1,
        max_queue_length: int = 200,
    ) -> None:
        self._redis = redis
        self._max_wait = max_wait_seconds
        self._poll_interval = poll_interval_seconds
        self._max_queue_length = max_queue_length

    async def run_with_queueing(
        self, *, team: TeamConfig, estimated_tokens: int, limiter: RateLimiter
    ) -> RateLimitDecision:
        decision = await limiter.check(team=team, estimated_tokens=estimated_tokens, priority="batch")
        if decision.allowed:
            return decision

        queue_key = self.QUEUE_KEY_TMPL.format(team_id=team.team_id)
        queue_len = await self._redis.llen(queue_key)
        if queue_len >= self._max_queue_length:
            logger.warning(
                "batch queue full for team=%s (len=%d) — surfacing 429 without queueing",
                team.team_id,
                queue_len,
            )
            return decision

        ticket = f"{time.time():.6f}:{uuid.uuid4().hex[:8]}"
        await self._redis.rpush(queue_key, ticket)
        try:
            deadline = time.monotonic() + self._max_wait
            while time.monotonic() < deadline:
                await asyncio.sleep(self._poll_interval)
                decision = await limiter.check(
                    team=team, estimated_tokens=estimated_tokens, priority="batch"
                )
                if decision.allowed:
                    return decision
            return decision
        finally:
            await self._redis.lrem(queue_key, 1, ticket)

    async def queue_depth(self, team_id: str) -> int:
        return await self._redis.llen(self.QUEUE_KEY_TMPL.format(team_id=team_id))
