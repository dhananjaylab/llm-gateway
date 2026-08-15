"""
FastAPI app factory.

Phase 3 adds to the lifespan:
- `CircuitBreaker` (Redis-backed, app/resilience/circuit_breaker.py) —
  replaces app/resilience/stub.py's module-level always-Closed singleton
  from Phase 1/2. It has to live in app.state now, not as a module-level
  singleton in app/api/v1_chat.py, for the same reason RateLimiter and
  BudgetEnforcer already do: it needs a Redis client, and the Redis
  client is a per-app (per-test, per-process) thing, not a process-wide
  constant.
- `RetryPolicy` (app/resilience/retry.py) and `FallbackRouter`
  (app/resilience/fallback.py), composed from the circuit breaker, the
  retry policy, config/tiers.yaml, and (so passive health monitoring
  observes every attempt, not just the final one) the health tracker.
- `HealthTracker` (app/resilience/health.py) — the rolling-window store
  both passive monitoring (via FallbackRouter) and active probing write
  to.
- A background `HealthChecker` task, started only if
  `settings.health_check_enabled` (default True per the TRD; the test
  suite forces it off — see tests/unit/conftest.py's docstring on why).
  Mirrors the existing `_listen_for_config_changes` background-task
  pattern: created with `asyncio.create_task` in the lifespan, cancelled
  and awaited in the `finally` block on shutdown.

GET /readyz is unchanged — it was already checking the one dependency
(Redis) everything in this file depends on; Phase 3 doesn't add a new
external dependency to check.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis

from app.api.admin import router as admin_router
from app.api.v1_chat import router as v1_chat_router
from app.core.audit import AuditLog
from app.core.config import get_gateway_settings, load_teams_config, load_tiers_config
from app.core.pricing import load_pricing
from app.core.redis_client import build_redis_client
from app.core.team_store import TeamConfigStore
from app.observability.metrics import build_metrics
from app.observability.tracing import init_tracing
from app.providers.registry import all_configured_provider_models, resolve_model
from app.ratelimit.budget import BudgetEnforcer
from app.ratelimit.limiter import RateLimiter
from app.ratelimit.priority_queue import BatchPriorityQueue
from app.resilience.circuit_breaker import CircuitBreaker
from app.resilience.fallback import FallbackRouter
from app.resilience.health import HealthChecker, HealthTracker
from app.resilience.retry import RetryPolicy

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gateway.main")


async def _listen_for_config_changes(app: FastAPI) -> None:
    """
    Background task: invalidate this instance's local TeamConfigStore
    cache the moment ANY instance (including this one, redundantly but
    harmlessly) PATCHes a team via the Admin API. See team_store.py's
    module docstring for why this exists alongside the short cache TTL
    rather than instead of it.
    """
    redis: Redis = app.state.redis
    pubsub = redis.pubsub()
    await pubsub.subscribe(TeamConfigStore.CONFIG_CHANGE_CHANNEL)
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            team_id = message["data"]
            app.state.team_store.invalidate(team_id)
            logger.debug("config-change event: invalidated cache for team=%s", team_id)
    except asyncio.CancelledError:
        pass
    finally:
        await pubsub.unsubscribe(TeamConfigStore.CONFIG_CHANGE_CHANNEL)
        await pubsub.aclose()


def _build_lifespan(redis_client_override: Redis | None):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_gateway_settings()
        owns_redis = redis_client_override is None
        redis_client = redis_client_override or build_redis_client(settings.redis_url)

        app.state.redis = redis_client
        app.state.team_store = TeamConfigStore(redis_client)

        # -- Phase 4: metrics --------------------------------------------------
        # Built early -- BudgetEnforcer, CircuitBreaker, and FallbackRouter
        # below all take it as a constructor arg. Tracing is NOT set up
        # here -- see create_app()'s comment for why FastAPIInstrumentor has
        # to run before the lifespan's first ASGI call, not inside it.
        # app.state.tracer/.tracer_provider are already populated by the
        # time this function runs.
        app.state.metrics = build_metrics() if settings.metrics_enabled else None

        app.state.rate_limiter = RateLimiter(
            redis_client,
            key_ttl_seconds=settings.rate_limit_key_ttl_seconds,
            fail_open=settings.rate_limit_fail_open,
        )
        app.state.budget_enforcer = BudgetEnforcer(
            redis_client, warn_fraction=settings.budget_warn_fraction, metrics=app.state.metrics
        )
        app.state.batch_queue = BatchPriorityQueue(
            redis_client,
            max_wait_seconds=settings.batch_queue_max_wait_seconds,
            poll_interval_seconds=settings.batch_queue_poll_interval_seconds,
            max_queue_length=settings.batch_queue_max_length,
        )
        app.state.audit_log = AuditLog(redis_client)
        app.state.pricing = load_pricing(settings.pricing_path)

        # -- Phase 3: resilience layer --------------------------------------
        app.state.tiers_config = load_tiers_config(settings.tiers_path)

        app.state.circuit_breaker = CircuitBreaker(
            redis_client,
            failure_threshold=settings.circuit_breaker_failure_threshold,
            window_size=settings.circuit_breaker_window_size,
            cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
            metrics=app.state.metrics,
        )
        app.state.health_tracker = HealthTracker(
            redis_client,
            window_seconds=settings.health_window_seconds,
            degraded_error_rate=settings.health_degraded_error_rate,
            down_error_rate=settings.health_down_error_rate,
            degraded_latency_p99_ms=settings.health_degraded_latency_p99_ms,
        )
        retry_policy = RetryPolicy(
            max_attempts=settings.retry_max_attempts,
            base_delay_seconds=settings.retry_base_delay_seconds,
            max_delay_seconds=settings.retry_max_delay_seconds,
        )
        app.state.fallback_router = FallbackRouter(
            circuit_breaker=app.state.circuit_breaker,
            retry_policy=retry_policy,
            tiers_config=app.state.tiers_config,
            health_tracker=app.state.health_tracker,
            tracer=app.state.tracer,
            metrics=app.state.metrics,
            capture_content=settings.otel_capture_message_content,
        )

        seeded = await app.state.team_store.seed_from_yaml_if_empty(load_teams_config())
        if seeded:
            logger.info("bootstrap-seeded %d teams into Redis from config/teams.yaml", seeded)

        listener_task = asyncio.create_task(_listen_for_config_changes(app))

        health_checker_task: asyncio.Task | None = None
        if settings.health_check_enabled:
            provider_models = all_configured_provider_models(app.state.tiers_config.all_links())
            if provider_models:
                health_checker = HealthChecker(
                    app.state.health_tracker,
                    provider_models=provider_models,
                    interval_seconds=settings.health_check_interval_seconds,
                    probe_timeout_seconds=settings.health_check_probe_timeout_seconds,
                )
                health_checker_task = asyncio.create_task(health_checker.run_forever(resolve_model))
            else:
                logger.info(
                    "health checking enabled but config/tiers.yaml has no chain links to probe — "
                    "skipping the background task"
                )
        else:
            logger.info("HEALTH_CHECK_ENABLED=false — active health probing is disabled")

        try:
            yield
        finally:
            listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener_task
            if health_checker_task is not None:
                health_checker_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await health_checker_task
            # Flush any batched-but-not-yet-exported spans before the
            # process/test tears down. A no-op when nothing was ever
            # added as a processor (otlp_endpoint unset AND no test
            # override) -- shutdown() on an empty provider is safe.
            app.state.tracer_provider.shutdown()
            if owns_redis:
                await redis_client.aclose()

    return lifespan


def create_app(*, redis_client: Redis | None = None, span_exporter_override=None) -> FastAPI:
    settings = get_gateway_settings()

    app = FastAPI(
        title="LLM Gateway",
        version="0.4.0",
        description=(
            "Multi-provider LLM API gateway: unified schema, rate limiting, "
            "fallback routing, and observability. Phase 4: OpenTelemetry "
            "distributed tracing, Prometheus metrics, and alert rules on top "
            "of Phase 3's tier-based fallback chains and circuit breakers."
        ),
        lifespan=_build_lifespan(redis_client),
    )

    # Tracing is wired HERE, before the app is ever invoked via ASGI --
    # including before its own lifespan "startup" scope, which is itself
    # the first ASGI call. Starlette lazily builds and *caches*
    # `app.middleware_stack` on that first call; FastAPIInstrumentor works
    # by adding a middleware, so instrumenting from inside the lifespan
    # (as an earlier draft of this did) is one call too late -- the
    # middleware stack would already be built and cached without it,
    # and the root SERVER span would silently never be created. Confirmed
    # by hand against a running app before landing this: moving
    # `init_tracing()` here (from inside `_build_lifespan`) is what fixed
    # zero spans being exported for real HTTP requests.
    app.state.tracer_provider = init_tracing(
        app,
        service_name=settings.otel_service_name,
        service_version=app.version,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        span_exporter_override=span_exporter_override,
    )
    app.state.tracer = app.state.tracer_provider.get_tracer(settings.otel_service_name)

    app.include_router(v1_chat_router)
    app.include_router(admin_router)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        try:
            await app.state.redis.ping()
        except Exception as exc:
            logger.warning("readyz: redis ping failed", exc_info=exc)
            return JSONResponse(
                status_code=503, content={"status": "not_ready", "reason": "redis_unreachable"}
            )
        return {"status": "ready"}

    @app.get("/metrics")
    async def metrics():
        """Prometheus scrape target (TRD: "exposed on GET /metrics for
        scraping every 5-15 seconds"). 404s rather than 200-with-empty-body
        when METRICS_ENABLED=false, so a misconfigured scrape target fails
        loudly in Prometheus rather than silently recording zero series."""
        if app.state.metrics is None:
            return JSONResponse(status_code=404, content={"detail": "metrics are disabled"})
        payload = generate_latest(app.state.metrics.registry)
        return Response(content=payload, media_type=CONTENT_TYPE_LATEST)

    return app

app = create_app()
