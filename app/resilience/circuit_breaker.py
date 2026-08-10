"""
Phase 3 circuit breaker — replaces app/resilience/stub.py's permissive
always-Closed stub with the real Redis-backed three-state state machine
Document 05 and the TRD describe.

Preserves the stub's exact method signatures (`allow_request` /
`record_success` / `record_failure`) — per the stub's own docstring, that
was always the seam Phase 3 would fill in, not replace, so
app/resilience/fallback.py's call sites don't need to know which
implementation they're holding.

State machine (Document 05, "Stateful Three-State Circuit Breakers"):

    Closed ----(>= failure_threshold failures in the last window_size
                calls)----------------------------------------------> Open
    Open   ----(cooldown_seconds elapsed since it opened)-----------> Half-Open
    Half-Open ----(the single probe call succeeds)-------------------> Closed
    Half-Open ----(the single probe call fails)----------------------> Open

Threshold strategy (TRD Appendix A, resolved per its own v1
recommendation rather than the alternative rolling-error-rate-percentage
design): a fixed count over a fixed-size rolling window
(failure_threshold=5, window_size=10 by default — a 50% failure rate),
not a time-windowed percentage. Simpler to reason about and to test
deterministically; the TRD explicitly flags moving to a rolling
percentage as a *later* change if this proves too twitchy in production,
not a Phase 3 requirement.

All the state-transition logic and the "exactly one Half-Open probe in
flight" invariant live in circuit_check.lua / circuit_record.lua, run
atomically via EVALSHA (the same LuaScript wrapper Phase 2's rate limiter
and budget enforcer already use) — see those files for the detailed
reasoning on why this has to be server-side Lua rather than Python
read-modify-write.

Redis-down behavior: unlike budget enforcement (fail-closed, Phase 2's
explicit "safer for budget" call) and consistent with the rate limiter's
default (fail-open), a Redis outage here makes `allow_request` return
True — a broken circuit-breaker store must not itself become an outage
that makes every provider look permanently tripped. This wasn't
explicitly specified in the TRD (Appendix A only covers rate-limit and
budget fail modes); flagged here as the Phase 3 interpretation, made for
consistency with the rate limiter rather than budget enforcement, since a
circuit breaker's job is availability, not spend control.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from redis import exceptions as redis_exceptions
from redis.asyncio import Redis

from app.core.redis_script import LuaScript

logger = logging.getLogger("gateway.circuit_breaker")

_SCRIPT_DIR = Path(__file__).resolve().parent

# circuit_check.lua's state_code return values.
_STATE_CLOSED = 0
_STATE_OPEN = 1
_STATE_HALF_OPEN = 2


@dataclass
class CircuitStatus:
    """Read-only snapshot for admin visibility (GET /admin/circuits)."""

    provider: str
    model: str
    state: str  # "closed" | "open" | "half_open"
    opened_at: float | None
    failures_in_window: int
    window_size: int


class CircuitBreaker:
    def __init__(
        self,
        redis: Redis,
        *,
        failure_threshold: int = 5,
        window_size: int = 10,
        cooldown_seconds: float = 60.0,
        clock=time.time,
    ) -> None:
        self._redis = redis
        self._failure_threshold = failure_threshold
        self._window_size = window_size
        self._cooldown = cooldown_seconds
        self._clock = clock

        check_text = (_SCRIPT_DIR / "circuit_check.lua").read_text(encoding="utf-8")
        self._check_script = LuaScript(redis, check_text, name="circuit_check")

        record_text = (_SCRIPT_DIR / "circuit_record.lua").read_text(encoding="utf-8")
        self._record_script = LuaScript(redis, record_text, name="circuit_record")

    @staticmethod
    def _key(provider: str, model: str) -> str:
        return f"circuit:{provider}:{model}"

    @staticmethod
    def _window_key(provider: str, model: str) -> str:
        return f"circuit:{provider}:{model}:window"

    # -- the interface app/resilience/stub.py established ---------------------

    async def allow_request(self, *, provider: str, model: str) -> bool:
        now = self._clock()
        try:
            allowed, state_code = await self._check_script.eval(
                keys=[self._key(provider, model)], args=[now, self._cooldown]
            )
        except (redis_exceptions.ConnectionError, redis_exceptions.TimeoutError):
            logger.warning(
                "redis unavailable during circuit-breaker check for %s:%s — failing open",
                provider,
                model,
                exc_info=True,
            )
            return True

        allowed = bool(int(allowed))
        state_code = int(state_code)

        if allowed and state_code == _STATE_HALF_OPEN:
            # This call didn't just observe a Half-Open circuit — it IS
            # the single probe (circuit_check.lua only returns
            # allowed=1/half_open on the exact call that either just
            # tripped Open -> Half-Open, or claimed an as-yet-unclaimed
            # probe slot). Worth a log line every time since it's rare by
            # construction (at most once per cooldown window per provider).
            logger.warning(
                "circuit breaker: %s:%s cooldown elapsed, issuing Half-Open probe request",
                provider,
                model,
                extra={"provider": provider, "model": model, "event": "half_open_probe_issued"},
            )
        return allowed

    async def record_success(self, *, provider: str, model: str) -> None:
        await self._record(provider, model, outcome=0)

    async def record_failure(self, *, provider: str, model: str) -> None:
        await self._record(provider, model, outcome=1)

    async def _record(self, provider: str, model: str, *, outcome: int) -> None:
        now = self._clock()
        try:
            prev_state, new_state = await self._record_script.eval(
                keys=[self._key(provider, model), self._window_key(provider, model)],
                args=[outcome, now, self._window_size, self._failure_threshold],
            )
        except (redis_exceptions.ConnectionError, redis_exceptions.TimeoutError):
            logger.warning(
                "redis unavailable while recording circuit outcome for %s:%s — outcome not recorded",
                provider,
                model,
                exc_info=True,
            )
            return

        if prev_state != new_state:
            logger.warning(
                "circuit breaker state transition: %s:%s %s -> %s (trigger=%s)",
                provider,
                model,
                prev_state,
                new_state,
                "success" if outcome == 0 else "failure",
                extra={
                    "provider": provider,
                    "model": model,
                    "from_state": prev_state,
                    "to_state": new_state,
                    "trigger": "success" if outcome == 0 else "failure",
                    "event": "circuit_state_transition",
                },
            )

    # -- admin visibility (Phase 3 addition, ahead of Phase 4 Grafana) --------

    async def get_status(self, *, provider: str, model: str) -> CircuitStatus:
        raw = await self._redis.hgetall(self._key(provider, model))
        state = raw.get("state", "closed")
        opened_at = float(raw["opened_at"]) if raw.get("opened_at") else None
        window = await self._redis.lrange(self._window_key(provider, model), 0, -1)
        failures_in_window = sum(1 for v in window if v == "1")
        return CircuitStatus(
            provider=provider,
            model=model,
            state=state,
            opened_at=opened_at,
            failures_in_window=failures_in_window,
            window_size=len(window),
        )

    async def list_status(self, provider_models: list[tuple[str, str]]) -> list[CircuitStatus]:
        return [await self.get_status(provider=p, model=m) for p, m in provider_models]
