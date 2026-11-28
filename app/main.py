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

Phase 9a adds `TiersConfigStore`/`PricingStore`, the RCU-pattern hot-reload
stores for config/tiers.yaml and config/pricing.yaml (see those modules'
own docstrings for why they're a different shape than TeamConfigStore's
per-key cache), plus their own pub/sub refresh listeners.

Phase 9b adds the two-layer exact/semantic response cache
(docs/PHASE9B_KICKOFF_SCOPING.md Sec 6/11): `FastEmbedProvider`,
`ExactMatchCache`, `SemanticCache`, composed into one `CacheGateway` on
`app.state.cache_gateway`, plus an optional background `CacheSweeper`
task started only when caching is enabled. See the inline comment at its
construction site below for why it's built unconditionally regardless of
`settings.cache_enabled`.

GET /readyz is unchanged — it was already checking the one dependency
(Redis) everything in this file depends on; no phase since has added a
new external dependency to check (FastEmbed's model download is lazy,
on first cache lookup, not at boot -- see embedding.py's own docstring
for why, and why that's a deliberate, not accidental, choice).
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
from app.caching.cache_gateway import CacheGateway
from app.caching.embedding import FastEmbedProvider
from app.caching.exact_cache import ExactMatchCache
from app.caching.semantic_cache import SemanticCache
from app.caching.sweep import CacheSweeper
from app.core.audit import AuditLog
from app.core.config import get_gateway_settings, load_orgs_config, load_teams_config, load_tiers_config
from app.core.org_store import OrgConfigStore
from app.core.pricing import load_pricing
from app.core.pricing_store import PricingStore
from app.core.redis_client import build_redis_client
from app.core.team_store import TeamConfigStore
from app.core.tiers_store import TiersConfigStore
from app.observability.metrics import build_metrics
from app.observability.tracing import init_tracing
from app.providers.registry import all_configured_provider_models, close_all_adapters, resolve_model
from app.ratelimit.budget import BudgetEnforcer
from app.ratelimit.combined import CombinedQuotaChecker
from app.ratelimit.limiter import RateLimiter
from app.ratelimit.priority_queue import BatchPriorityQueue
from app.resilience.circuit_breaker import CircuitBreaker
from app.resilience.fallback import FallbackRouter
from app.resilience.health import HealthChecker, HealthTracker
from app.resilience.retry import RetryPolicy

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gateway.main")


async def _listen_for_store_changes(app: FastAPI, *, channel: str, invalidate) -> None:
    """
    Generic background listener shared by the team-config channel (Phase
    2) and the org-config channel (Phase 8, docs/PHASE8_KICKOFF_SCOPING.md
    §6) — both TeamConfigStore and OrgConfigStore PUBLISH their own
    changed id as the message body on their own channel, so `invalidate`
    is just `store.invalidate` bound to whichever store owns `channel`.
    """
    redis: Redis = app.state.redis
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            entity_id = message["data"]
            invalidate(entity_id)
            logger.debug("config-change event on %s: invalidated cache for %s", channel, entity_id)
    except asyncio.CancelledError:
        pass
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()


async def _listen_for_full_refresh(app: FastAPI, *, channel: str, refresh_fn) -> None:
    """
    Phase 9a counterpart to `_listen_for_store_changes` above, for stores
    that keep the WHOLE table resident in memory (`TiersConfigStore`,
    `PricingStore` — see those modules' docstrings for why) rather than a
    per-key cache. On any change event, regardless of which single key
    changed, the listener just re-reads the entire table via `refresh_fn()`
    — cheap at this table's size (a handful of tiers/pricing rows) and
    keeps both stores simple, avoiding a second, per-key-diff code path.

    Deliberately a NEW, separate helper rather than extending
    `_listen_for_store_changes` itself: that helper's `invalidate` callback
    is called synchronously, without `await` (TeamConfigStore.invalidate /
    OrgConfigStore.invalidate are plain sync methods) — `refresh_fn` here
    has to be awaited (it reads Redis to rebuild the in-memory table), and
    changing the existing helper's contract would touch two already-tested,
    already-locked-down call sites for no benefit to them.
    """
    redis: Redis = app.state.redis
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            await refresh_fn()
            logger.debug("config-change event on %s: refreshed full table", channel)
    except asyncio.CancelledError:
        pass
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()


def _build_lifespan(redis_client_override: Redis | None):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_gateway_settings()
        owns_redis = redis_client_override is None
        redis_client = redis_client_override or build_redis_client(
            settings.redis_url,
            socket_timeout=settings.redis_socket_timeout_seconds,
            socket_connect_timeout=settings.redis_socket_connect_timeout_seconds,
            max_connections=settings.redis_max_connections,
        )

        app.state.redis = redis_client
        app.state.team_store = TeamConfigStore(redis_client)
        app.state.org_store = OrgConfigStore(redis_client)

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
        # Phase 7: single-round-trip fast path for the normal/high-priority
        # hot path, wrapping the two objects above rather than replacing
        # them — the batch-priority path (BatchPriorityQueue below) keeps
        # calling rate_limiter.check() directly, and CombinedQuotaChecker's
        # own Redis-outage fallback calls budget_enforcer.precheck() +
        # rate_limiter.check() exactly as before. See
        # app/ratelimit/combined.py's module docstring.
        app.state.combined_quota_checker = CombinedQuotaChecker(
            redis_client,
            rate_limiter=app.state.rate_limiter,
            budget_enforcer=app.state.budget_enforcer,
            rl_key_ttl_seconds=settings.rate_limit_key_ttl_seconds,
        )
        app.state.batch_queue = BatchPriorityQueue(
            redis_client,
            max_wait_seconds=settings.batch_queue_max_wait_seconds,
            poll_interval_seconds=settings.batch_queue_poll_interval_seconds,
            max_queue_length=settings.batch_queue_max_length,
        )
        app.state.audit_log = AuditLog(redis_client)
        app.state.pricing = load_pricing(settings.pricing_path)
        # Phase 9a: hot-reload for $ pricing (docs/PHASE9A_IMPLEMENTATION_GUIDE.md).
        # `table=app.state.pricing` — PricingStore.refresh() mutates THIS
        # exact dict object in place; it is never reassigned. See that
        # module's docstring for why FallbackRouter's own captured
        # `self._pricing_table` reference depends on that distinction.
        app.state.pricing_store = PricingStore(redis_client, table=app.state.pricing)
        seeded_pricing = await app.state.pricing_store.seed_from_yaml_if_empty(
            load_pricing(settings.pricing_path)
        )
        if seeded_pricing:
            logger.info(
                "bootstrap-seeded %d pricing row(s) into Redis from config/pricing.yaml", seeded_pricing
            )
        # Always refresh, even when nothing was just seeded (e.g. Redis
        # already had rows written by a PATCH from another instance before
        # this one booted) — Redis, not the YAML file, is the runtime
        # source of truth from here on, same rule teams/orgs already follow.
        await app.state.pricing_store.refresh()

        # -- Phase 3: resilience layer --------------------------------------
        app.state.tiers_config = load_tiers_config(settings.tiers_path)
        # Phase 9a: hot-reload for tier -> fallback-chain config. Same RCU
        # pattern as pricing above — see app/core/tiers_store.py's docstring.
        # Constructed and refreshed BEFORE `FallbackRouter` below so the
        # object it's handed is already Redis-authoritative, not the raw
        # YAML snapshot, from the very first request.
        app.state.tiers_store = TiersConfigStore(redis_client, tiers_config=app.state.tiers_config)
        seeded_tiers = await app.state.tiers_store.seed_from_yaml_if_empty(app.state.tiers_config)
        if seeded_tiers:
            logger.info("bootstrap-seeded %d tier(s) into Redis from config/tiers.yaml", seeded_tiers)
        await app.state.tiers_store.refresh()

        app.state.circuit_breaker = CircuitBreaker(
            redis_client,
            failure_threshold=settings.circuit_breaker_failure_threshold,
            window_size=settings.circuit_breaker_window_size,
            cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
            metrics=app.state.metrics,
            mode=settings.circuit_breaker_mode,
            error_rate_window_seconds=settings.circuit_breaker_error_rate_window_seconds,
            error_rate_threshold=settings.circuit_breaker_error_rate_threshold,
            error_rate_minimum_samples=settings.circuit_breaker_error_rate_minimum_samples,
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
            pricing_table=app.state.pricing,
        )

        # -- Phase 9b: semantic response cache -------------------------------
        # See docs/PHASE9B_KICKOFF_SCOPING.md Sec 6/11. `cache_gateway` is
        # ALWAYS constructed here, regardless of `settings.cache_enabled` --
        # CacheGateway's own eligibility gate (app/caching/cache_gateway.py)
        # already checks that flag (and TeamConfig.semantic_cache_enabled)
        # on every call and returns "ineligible" without touching Redis or
        # the embedder when caching is off. Two independent on/off switches
        # for the same feature -- this attribute being None vs. the settings
        # flag -- would be a second, redundant source of truth; settings.
        # cache_enabled stays the only one that matters. The
        # `getattr(state, "cache_gateway", None)` guards at the
        # v1_chat.py/admin.py call sites exist purely as defensive
        # robustness for an app.state assembled without this lifespan at
        # all (e.g. a narrow test harness), not as this feature's real
        # on/off mechanism.
        #
        # FastEmbedProvider's model load is LAZY (first cache lookup, not
        # here) -- see embedding.py's own docstring: the ONNX weights
        # download from Hugging Face on first use, and failing that at
        # boot would make a transient or restricted-egress network hiccup
        # take the whole gateway process down over a feature that's
        # supposed to be a pure optimization. CacheGateway's own fail-open
        # wrapper (see cache_gateway.py) is what actually absorbs a load
        # failure when it happens, not this constructor.
        embedding_provider = FastEmbedProvider(
            model_name=settings.embedding_model, dimension=settings.embedding_dimension
        )
        app.state.exact_cache = ExactMatchCache(redis_client, ttl_seconds=settings.cache_ttl_seconds)
        app.state.semantic_cache = SemanticCache(
            redis_client,
            ttl_seconds=settings.cache_ttl_seconds,
            similarity_threshold=settings.cache_similarity_threshold,
            max_entries_per_scope=settings.cache_max_entries_per_scope,
        )
        app.state.cache_gateway = CacheGateway(
            exact_cache=app.state.exact_cache,
            semantic_cache=app.state.semantic_cache,
            embedding_provider=embedding_provider,
            settings=settings,
        )

        seeded = await app.state.team_store.seed_from_yaml_if_empty(load_teams_config())
        if seeded:
            logger.info("bootstrap-seeded %d teams into Redis from config/teams.yaml", seeded)

        seeded_orgs = await app.state.org_store.seed_from_yaml_if_empty(load_orgs_config(settings.orgs_path))
        if seeded_orgs:
            logger.info("bootstrap-seeded %d org(s) into Redis from config/orgs.yaml", seeded_orgs)

        listener_task = asyncio.create_task(
            _listen_for_store_changes(
                app,
                channel=TeamConfigStore.CONFIG_CHANGE_CHANNEL,
                invalidate=app.state.team_store.invalidate,
            )
        )
        org_listener_task = asyncio.create_task(
            _listen_for_store_changes(
                app, channel=OrgConfigStore.CONFIG_CHANGE_CHANNEL, invalidate=app.state.org_store.invalidate
            )
        )
        # Phase 9a: full-table-refresh listeners (see _listen_for_full_refresh's
        # own docstring for why these are a separate helper from the two above).
        tiers_listener_task = asyncio.create_task(
            _listen_for_full_refresh(
                app, channel=TiersConfigStore.CONFIG_CHANGE_CHANNEL, refresh_fn=app.state.tiers_store.refresh
            )
        )
        pricing_listener_task = asyncio.create_task(
            _listen_for_full_refresh(
                app, channel=PricingStore.CONFIG_CHANGE_CHANNEL, refresh_fn=app.state.pricing_store.refresh
            )
        )

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

        # Phase 9b: optional memory-hygiene sweep (app/caching/sweep.py) --
        # only worth running when caching is actually on; NOT a correctness
        # dependency either way (SemanticCache.get() already self-heals a
        # stale entry lazily the moment it's next looked up — see that
        # module's own docstring).
        cache_sweeper_task: asyncio.Task | None = None
        if settings.cache_enabled:
            cache_sweeper = CacheSweeper(redis_client, interval_seconds=300.0)
            cache_sweeper_task = asyncio.create_task(cache_sweeper.run_forever())
        else:
            logger.info("CACHE_ENABLED=false — the cache sweeper background task is not started")

        try:
            yield
        finally:
            listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener_task
            org_listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await org_listener_task
            tiers_listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tiers_listener_task
            pricing_listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pricing_listener_task
            if health_checker_task is not None:
                health_checker_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await health_checker_task
            if cache_sweeper_task is not None:
                cache_sweeper_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cache_sweeper_task
            # Flush any batched-but-not-yet-exported spans before the
            # process/test tears down. A no-op when nothing was ever
            # added as a processor (otlp_endpoint unset AND no test
            # override) -- shutdown() on an empty provider is safe.
            app.state.tracer_provider.shutdown()
            # Phase 7: close every provider adapter's shared, pooled
            # httpx.AsyncClient (the counterpart to constructing it once
            # in each adapter's __init__ instead of once per call — see
            # app/providers/registry.py::close_all_adapters and
            # docs/PHASE7_IMPLEMENTATION_GUIDE.md). A no-op if no chat
            # request was ever routed this process lifetime.
            await close_all_adapters()
            if owns_redis:
                await redis_client.aclose()

    return lifespan


def create_app(*, redis_client: Redis | None = None, span_exporter_override=None) -> FastAPI:
    settings = get_gateway_settings()

    app = FastAPI(
        title="LLM Gateway",
        version="0.5.0",
        description=(
            "Multi-provider LLM API gateway: unified schema, rate limiting, "
            "fallback routing, and observability on top of Phase 3's tier-based "
            "fallback chains and circuit breakers, Phase 9a's config hot-reload, "
            "and Phase 9b's two-layer exact/semantic response cache."
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
