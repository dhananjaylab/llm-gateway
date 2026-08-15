"""
Health checking (TRD, "Proactive and Passive Health Probing"; Document 05:
`health:{provider}:{model}` SORTED SET, "score=timestamp,
member=JSON{latency_ms, ok}", "Rolling 60s window, trimmed on write
(ZREMRANGEBYSCORE)").

Two complementary signals feed the same rolling window, exactly as the
TRD specifies:

- **Passive**: every real (non-probe) provider call, win or lose, calls
  `record_outcome()` — wired into app/api/v1_chat.py right next to the
  existing circuit-breaker record_success/record_failure calls, since
  it's observing the same events.
- **Proactive**: `HealthChecker` is a background `asyncio.Task` (started
  in app/main.py's lifespan, mirroring the existing
  `_listen_for_config_changes` pattern) that, every
  `health_check_interval_seconds`, sends one minimal completion request
  to every provider-model pair referenced anywhere in config/tiers.yaml,
  and records the outcome exactly the same way passive monitoring does —
  from the rolling window's point of view, a probe result and a real
  request's result are indistinguishable, which is the point (Document
  05 doesn't model them as separate signals, just separate sources
  feeding one signal).

Status derivation (`get_status`) is a pure function of the rolling
window's contents at the moment of the call: an empty window
(provider-model pair never seen) is "unknown" rather than "healthy" — a
gateway shouldn't claim to know something it has no data for. Otherwise:
error rate and P99 latency are computed from the window and compared
against `GatewaySettings.health_degraded_error_rate` /
`health_down_error_rate` / `health_degraded_latency_p99_ms` (see
app/core/config.py for the TRD-derived defaults and the note on which
numbers are this module's own assumption vs. a doc'd figure).

Scope note: this module is intentionally NOT consulted by
app/resilience/fallback.py's routing decision — Document 05's routing
hierarchy table names only the circuit breaker as a gate ("Circuit State
Check... If Closed, proceed; if Open, short-circuit"). Health status here
feeds observability (this phase's GET /admin/health, Phase 4's Grafana
Operations dashboard) rather than acting as a second, possibly
conflicting router. A provider can show "degraded" here while still being
Closed (and thus still receiving traffic) if it hasn't yet crossed the
circuit breaker's own independent failure threshold — that's expected,
not a bug: the two mechanisms answer different questions ("is this
provider having a rough time" vs. "have we decided to stop calling it").
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Literal

from redis import exceptions as redis_exceptions
from redis.asyncio import Redis

from app.core.schema import ChatMessage, UnifiedChatRequest
from app.providers.base import ProviderAdapter, ProviderError

logger = logging.getLogger("gateway.health")

HealthState = Literal["healthy", "degraded", "down", "unknown"]

_PROBE_PROMPT = "ping"


@dataclass
class HealthStatus:
    provider: str
    model: str
    state: HealthState
    sample_count: int
    error_rate: float | None
    p99_latency_ms: float | None
    last_checked_at: float | None


class HealthTracker:
    """Owns the rolling window: writes (passive + active) and reads (status derivation)."""

    def __init__(
        self,
        redis: Redis,
        *,
        window_seconds: int = 60,
        degraded_error_rate: float = 0.3,
        down_error_rate: float = 0.8,
        degraded_latency_p99_ms: float = 5000.0,
        clock=time.time,
    ) -> None:
        self._redis = redis
        self._window_seconds = window_seconds
        self._degraded_error_rate = degraded_error_rate
        self._down_error_rate = down_error_rate
        self._degraded_latency_p99_ms = degraded_latency_p99_ms
        self._clock = clock

    @staticmethod
    def _key(provider: str, model: str) -> str:
        return f"health:{provider}:{model}"

    async def record_outcome(self, *, provider: str, model: str, ok: bool, latency_ms: float) -> None:
        """
        Append one observation to the rolling window and trim anything
        older than `window_seconds`. Best-effort: a Redis hiccup here
        must never fail (or slow down) the request that triggered it —
        this is telemetry, not enforcement.
        """
        now = self._clock()
        key = self._key(provider, model)
        member = json.dumps({"ok": ok, "latency_ms": round(latency_ms, 2), "ts": now})
        try:
            pipe = self._redis.pipeline(transaction=True)
            pipe.zadd(key, {member: now})
            pipe.zremrangebyscore(key, 0, now - self._window_seconds)
            # Belt-and-suspenders TTL so a provider-model pair that stops
            # being called entirely (e.g. removed from tiers.yaml) doesn't
            # leave a stale key around forever.
            pipe.expire(key, self._window_seconds * 4)
            await pipe.execute()
        except (redis_exceptions.ConnectionError, redis_exceptions.TimeoutError):
            logger.warning(
                "redis unavailable while recording health outcome for %s:%s — skipped",
                provider,
                model,
                exc_info=True,
            )

    async def get_status(self, *, provider: str, model: str) -> HealthStatus:
        now = self._clock()
        key = self._key(provider, model)
        try:
            await self._redis.zremrangebyscore(key, 0, now - self._window_seconds)
            raw_entries = await self._redis.zrange(key, 0, -1)
        except (redis_exceptions.ConnectionError, redis_exceptions.TimeoutError):
            logger.warning(
                "redis unavailable while reading health status for %s:%s", provider, model, exc_info=True
            )
            return HealthStatus(
                provider=provider,
                model=model,
                state="unknown",
                sample_count=0,
                error_rate=None,
                p99_latency_ms=None,
                last_checked_at=None,
            )

        entries = [json.loads(e) for e in raw_entries]
        if not entries:
            return HealthStatus(
                provider=provider,
                model=model,
                state="unknown",
                sample_count=0,
                error_rate=None,
                p99_latency_ms=None,
                last_checked_at=None,
            )

        sample_count = len(entries)
        error_rate = sum(1 for e in entries if not e["ok"]) / sample_count
        latencies = sorted(e["latency_ms"] for e in entries)
        p99_index = max(0, min(sample_count - 1, int(round(0.99 * (sample_count - 1)))))
        p99_latency_ms = latencies[p99_index]
        last_checked_at = max(e["ts"] for e in entries)

        if error_rate >= self._down_error_rate:
            state: HealthState = "down"
        elif error_rate >= self._degraded_error_rate or p99_latency_ms >= self._degraded_latency_p99_ms:
            state = "degraded"
        else:
            state = "healthy"

        return HealthStatus(
            provider=provider,
            model=model,
            state=state,
            sample_count=sample_count,
            error_rate=round(error_rate, 4),
            p99_latency_ms=p99_latency_ms,
            last_checked_at=last_checked_at,
        )

    async def list_status(self, provider_models: list[tuple[str, str]]) -> list[HealthStatus]:
        return [await self.get_status(provider=p, model=m) for p, m in provider_models]


def _build_probe_request(provider_model: str) -> UnifiedChatRequest:
    return UnifiedChatRequest(
        model=provider_model,
        messages=[ChatMessage(role="user", content=_PROBE_PROMPT)],
        max_tokens=16,
        stream=False,
    )


async def probe_once(
    tracker: HealthTracker,
    *,
    provider_model_id: str,
    provider_name: str,
    provider_model: str,
    adapter: ProviderAdapter,
    timeout_seconds: float,
    clock=time.time,
) -> None:
    """
    One synthetic "ping" completion against a single provider-model pair
    (TRD: "generating a single token from a baseline prompt like 'ping'").
    Records the outcome into the same rolling window passive monitoring
    writes to. Never raises — a probe failure IS the signal, not an error
    the caller needs to handle; it's recorded and swallowed.
    """
    request = _build_probe_request(provider_model_id)
    payload = adapter.translate_request(request, provider_model=provider_model)
    start = clock()
    try:
        await asyncio.wait_for(adapter.call(payload), timeout=timeout_seconds)
        ok = True
    except (ProviderError, TimeoutError, asyncio.TimeoutError):
        ok = False
    except Exception:  # pragma: no cover - defensive: a probe must never crash the loop
        logger.exception("unexpected error probing %s", provider_model_id)
        ok = False
    latency_ms = (clock() - start) * 1000
    await tracker.record_outcome(provider=provider_name, model=provider_model, ok=ok, latency_ms=latency_ms)


class HealthChecker:
    """Background daemon: probes every configured provider-model pair on a fixed interval."""

    def __init__(
        self,
        tracker: HealthTracker,
        *,
        provider_models: list[tuple[str, str, str]],
        interval_seconds: float = 30.0,
        probe_timeout_seconds: float = 10.0,
    ) -> None:
        """
        `provider_models`: (model_id, provider_name, provider_model)
        triples — see app/providers/registry.py::all_configured_provider_models,
        which is what builds this list from config/tiers.yaml's chains
        while silently skipping any provider whose API key isn't set in
        this environment.
        """
        self._tracker = tracker
        self._provider_models = provider_models
        self._interval = interval_seconds
        self._probe_timeout = probe_timeout_seconds

    async def run_forever(self, resolve_fn) -> None:
        """
        `resolve_fn`: same shape as app/resilience/fallback.py's
        `ResolveFn` — "provider:model" -> (adapter, provider_model). Kept
        as a parameter rather than importing the registry directly for
        the same testability reason fallback.py takes one.
        """
        logger.info(
            "health checker starting: probing %d provider-model pair(s) every %.0fs",
            len(self._provider_models),
            self._interval,
        )
        try:
            while True:
                await self._run_one_round(resolve_fn)
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            logger.info("health checker stopping")
            raise

    async def _run_one_round(self, resolve_fn) -> None:
        for model_id, _provider_name, _provider_model in self._provider_models:
            try:
                adapter, provider_model = resolve_fn(model_id)
            except Exception:  # pragma: no cover - defensive
                logger.exception("health checker: failed to resolve %s, skipping this round", model_id)
                continue
            await probe_once(
                self._tracker,
                provider_model_id=model_id,
                provider_name=adapter.provider_name,
                provider_model=provider_model,
                adapter=adapter,
                timeout_seconds=self._probe_timeout,
            )
