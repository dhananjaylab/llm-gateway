"""
Admin audit log — Redis Stream, append-only, trimmed by size not time.

Document 05's key-space table lists `audit:{admin_action_id}` as a
STREAM. Read literally that implies one stream per action, which doesn't
match how Redis Streams are meant to be used (a stream models an ordered
*sequence* of entries; XADD already mints a unique per-entry ID, so one
entry-per-action already gets a unique, orderable identifier for free).
This module implements the practical realization of that key pattern: a
single stream, `audit:admin_actions`, holding every admin action ever
taken, each entry carrying who/what/before/after/ts exactly as specified.
`XTRIM ... MAXLEN ~ N` (approximate trimming, per Document 05: "trimmed by
size (XTRIM) not time") keeps it bounded without a background job.
"""

from __future__ import annotations

import json
import time

from redis.asyncio import Redis

_STREAM_KEY = "audit:admin_actions"
_MAX_LENGTH = 5000


class AuditLog:
    def __init__(self, redis: Redis, *, max_length: int = _MAX_LENGTH) -> None:
        self._redis = redis
        self._max_length = max_length

    async def record(
        self,
        *,
        actor: str,
        action: str,
        team_id: str,
        before: dict | None,
        after: dict | None,
    ) -> str:
        entry_id = await self._redis.xadd(
            _STREAM_KEY,
            {
                "actor": actor,
                "action": action,
                "team_id": team_id,
                "before": json.dumps(before) if before is not None else "",
                "after": json.dumps(after) if after is not None else "",
                "ts": str(time.time()),
            },
            maxlen=self._max_length,
            approximate=True,
        )
        return entry_id

    async def recent(self, limit: int = 50) -> list[dict]:
        # XREVRANGE walks newest-first, which is what an audit view wants.
        entries = await self._redis.xrevrange(_STREAM_KEY, count=limit)
        out = []
        for entry_id, fields in entries:
            out.append(
                {
                    "id": entry_id,
                    "actor": fields.get("actor"),
                    "action": fields.get("action"),
                    "team_id": fields.get("team_id"),
                    "before": json.loads(fields["before"]) if fields.get("before") else None,
                    "after": json.loads(fields["after"]) if fields.get("after") else None,
                    "ts": float(fields.get("ts", 0.0)),
                }
            )
        return out
