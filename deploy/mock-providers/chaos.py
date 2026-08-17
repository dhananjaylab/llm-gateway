"""
Chaos-injection controller for the Phase 5 mock-providers service.

Lets integration tests and the k6 chaos scenario force a specific
provider (or one provider:model pair) into a failure/latency mode via a
live HTTP control plane (POST /_chaos/config), rather than a static env
var baked in at container start. Document 06's own chaos scenarios need
failure injected and cleared *mid-run* -- "simulate a provider outage
live" during a k6 run, or a single test asserting failover then recovery
-- and a container restart can't do that.

Rule resolution: an exact "{provider}:{model}" rule wins over a
"{provider}:*" (every model for that provider) wildcard rule, which wins
over "no rule at all" (chaos-free, the default state every provider
starts in).

STATUS CODE DEFAULT -- READ BEFORE CHANGING: Document 06 literally says
"dynamically injecting 500 error responses" for the outage scenario, but
every adapter in app/providers/*.py defines
`_RETRYABLE_STATUS = {429, 502, 503, 504}` -- 500 is NOT in that set. A
literal 500 would make the gateway treat the failure as non-retryable and
bubble it straight up as a 502 with zero retry and zero fallback attempt,
which defeats the entire point of an "outage triggers failover" scenario.
This is a real gap between the Phase 1 adapter code and Document 06's
Phase 5 prose, not a hypothetical -- caught by actually building this
against the real adapters, same as the Phase 4 GPT-5.6 temperature/top_p
bug. `ChaosRule`'s default is 503 (Service Unavailable) instead: it is
both retryable across all four adapters AND a more literal simulation of
"the provider is down" than a generic 500 would be. Pass status_code=500
explicitly to a caller that specifically wants to exercise the
non-retryable-bubbles-immediately path instead.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import asdict, dataclass, field
from threading import Lock

_DEFAULT_RETRYABLE_STATUS = 503


@dataclass
class ChaosRule:
    error_rate: float = 0.0  # 0..1 probability of forcing a failure on a given call
    latency_ms: float = 0.0  # added delay, applied on EVERY call this rule matches (success or failure)
    status_code: int = _DEFAULT_RETRYABLE_STATUS
    error_type: str = "mock_chaos_injected"
    set_at: float = field(default_factory=time.time)


class ChaosInjectedError(Exception):
    """Raised by `ChaosController.apply()`; caught by main.py's exception
    handler and turned into a provider-shaped error response."""

    def __init__(self, *, status_code: int, error_type: str) -> None:
        self.status_code = status_code
        self.error_type = error_type
        super().__init__(f"chaos-injected {status_code} ({error_type})")


class ChaosController:
    """
    In-memory, process-lifetime chaos state -- one instance per
    mock-providers process, shared across every request handler in
    main.py. Not persisted anywhere: a container restart clears it, which
    is the correct behavior for a test double (no state should leak
    between a k6 run and the next `docker-compose up`).
    """

    def __init__(self) -> None:
        self._rules: dict[str, ChaosRule] = {}
        self._lock = Lock()

    @staticmethod
    def _key(provider: str, model: str) -> str:
        return f"{provider}:{model}"

    def set_rule(
        self,
        *,
        provider: str,
        model: str = "*",
        error_rate: float = 0.0,
        latency_ms: float = 0.0,
        status_code: int = _DEFAULT_RETRYABLE_STATUS,
        error_type: str = "mock_chaos_injected",
    ) -> ChaosRule:
        rule = ChaosRule(
            error_rate=error_rate, latency_ms=latency_ms, status_code=status_code, error_type=error_type
        )
        with self._lock:
            self._rules[self._key(provider, model)] = rule
        return rule

    def clear(self, *, provider: str | None = None, model: str = "*") -> None:
        with self._lock:
            if provider is None:
                self._rules.clear()
            else:
                self._rules.pop(self._key(provider, model), None)

    def snapshot(self) -> dict:
        with self._lock:
            return {key: asdict(rule) for key, rule in self._rules.items()}

    def _resolve(self, provider: str, model: str) -> ChaosRule | None:
        with self._lock:
            return self._rules.get(self._key(provider, model)) or self._rules.get(self._key(provider, "*"))

    async def apply(self, *, provider: str, model: str) -> None:
        """
        Call once per incoming request, before building the response.
        Sleeps `latency_ms` first (on both the success and failure path,
        so a latency-only rule doesn't need a second error-rate rule to
        express "this provider is just slow right now"), then rolls
        `error_rate` and raises `ChaosInjectedError` if it fires. A no-op
        when no rule matches this provider/model -- the default,
        chaos-free state.
        """
        rule = self._resolve(provider, model)
        if rule is None:
            return
        if rule.latency_ms:
            await asyncio.sleep(rule.latency_ms / 1000)
        if rule.error_rate > 0 and random.random() < rule.error_rate:
            raise ChaosInjectedError(status_code=rule.status_code, error_type=rule.error_type)
