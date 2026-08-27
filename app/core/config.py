"""
Configuration loading.

Phase 1/2 scope (unchanged): `load_teams_config()` reads config/teams.yaml
once, used only as the bootstrap seed for Redis. `TeamConfig` / `TeamPolicy`
/ `GatewayConfig` are unchanged in shape.

Phase 3 additions:
- `load_tiers_config()`: loads config/tiers.yaml, the tier-name -> ordered
  fallback-chain mapping app/resilience/fallback.py resolves against.
  Not cached (like load_pricing() before it) — app/main.py's lifespan
  calls it once per app instance and stores the result on app.state, so
  a second layer of caching here would just be dead weight, and tests
  that build a fresh app per test (see tests/unit/conftest.py) get a
  clean slate for free rather than needing an explicit cache reset.
- `GatewaySettings` gains three new settings groups, all with defaults
  matching the TRD's own stated recommendations so the gateway is safe out
  of the box, and all overridable via environment variable without a code
  change (see .env.example):
    * health_check_* — active/passive health monitoring cadence and
      thresholds (app/resilience/health.py).
    * circuit_breaker_* — the "simple fixed threshold" the TRD Appendix A
      recommends for v1 (5 failures in a 10-request rolling window,
      60s cooldown) rather than a rolling error-rate percentage.
    * retry_* — max attempts and backoff bounds for
      app/resilience/retry.py's full-jitter exponential backoff.
Phase 8 addition:
- `org_id` on `TeamConfig` (default `"default-org"`, fully backward
  compatible — every Phase 1-7 team config without an explicit org_id
  gets this default) plus `OrgConfig`/`load_orgs_config()`, mirroring
  `TeamConfig`/`load_teams_config()` for the org-level quota tier
  (docs/PHASE8_KICKOFF_SCOPING.md §6, Option A — org-level only, per
  explicit developer sign-off). `OrgConfigStore` (app/core/org_store.py)
  is the Redis-backed, hot-reloadable runtime store, mirroring
  `TeamConfigStore` exactly.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "teams.yaml"
_DEFAULT_PRICING_PATH = Path(__file__).resolve().parents[2] / "config" / "pricing.yaml"
_DEFAULT_TIERS_PATH = Path(__file__).resolve().parents[2] / "config" / "tiers.yaml"
_DEFAULT_ORGS_PATH = Path(__file__).resolve().parents[2] / "config" / "orgs.yaml"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_last_loaded_dotenv_root: Path | None = None
DEFAULT_ORG_ID = "default-org"


def _load_project_dotenv(force: bool = False) -> None:
    global _last_loaded_dotenv_root
    if not force and _last_loaded_dotenv_root == _PROJECT_ROOT:
        return
    load_dotenv(_PROJECT_ROOT / ".env", override=False)
    _last_loaded_dotenv_root = _PROJECT_ROOT


_load_project_dotenv()


class TeamPolicy(BaseModel):
    system_prompt_prefix: str | None = None
    pii_redaction: bool = False


class TeamConfig(BaseModel):
    team_id: str
    api_key_hash: str  # "sha256:<hex>" — see app/core/auth.py for the check
    allowed_models: list[str] = Field(default_factory=list)
    rpm_cap: int = 60
    tpm_cap: int = 100_000
    budget_cap_usd: float = 50.0
    budget_period: str = "monthly"
    priority_tier: str = "realtime"
    policy: TeamPolicy = Field(default_factory=TeamPolicy)
    org_id: str = DEFAULT_ORG_ID


class OrgConfig(BaseModel):
    """
    The org-level quota tier above team (Phase 8, Option A — see module
    docstring). Every team belongs to exactly one org (`TeamConfig.org_id`,
    defaulting to `DEFAULT_ORG_ID`) and every deployment always has at
    least `DEFAULT_ORG_ID` seeded (see `OrgConfigStore.seed_from_yaml_if_empty`
    in app/core/org_store.py) — so org-level enforcement is always active,
    not an opt-in a team can silently fall outside of.
    """

    org_id: str
    rpm_cap: int = 1000
    tpm_cap: int = 500_000
    budget_cap_usd: float = 5000.0
    budget_period: str = "monthly"


class GatewayConfig(BaseModel):
    teams: dict[str, TeamConfig]

    def team_by_api_key_hash(self, api_key_hash: str) -> TeamConfig | None:
        for team in self.teams.values():
            if team.api_key_hash == api_key_hash:
                return team
        return None


def load_teams_config(path: str | Path | None = None) -> GatewayConfig:
    config_path = Path(path) if path else Path(os.environ.get("CONFIG_PATH", _DEFAULT_CONFIG_PATH))
    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    teams: dict[str, TeamConfig] = {}
    for team_id, team_raw in (raw.get("teams") or {}).items():
        teams[team_id] = TeamConfig(team_id=team_id, **team_raw)
    return GatewayConfig(teams=teams)


@lru_cache(maxsize=1)
def get_gateway_config() -> GatewayConfig:
    return load_teams_config()


def reset_config_cache() -> None:
    get_gateway_config.cache_clear()


class GatewayOrgsConfig(BaseModel):
    orgs: dict[str, OrgConfig]


def load_orgs_config(path: str | Path | None = None) -> GatewayOrgsConfig:
    """Bootstrap-seed loader for config/orgs.yaml, mirroring
    load_teams_config() exactly — Redis is the runtime source of truth
    once seeded (see app/core/org_store.py::OrgConfigStore)."""
    config_path = Path(path) if path else Path(os.environ.get("ORGS_CONFIG_PATH", _DEFAULT_ORGS_PATH))
    if not config_path.exists():
        # A deployment with no orgs.yaml at all still works: DEFAULT_ORG_ID
        # is synthesized with the OrgConfig model's own generous defaults
        # so org-level enforcement never blocks on missing config.
        return GatewayOrgsConfig(orgs={DEFAULT_ORG_ID: OrgConfig(org_id=DEFAULT_ORG_ID)})
    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    orgs: dict[str, OrgConfig] = {}
    for org_id, org_raw in (raw.get("orgs") or {}).items():
        orgs[org_id] = OrgConfig(org_id=org_id, **(org_raw or {}))
    if DEFAULT_ORG_ID not in orgs:
        orgs[DEFAULT_ORG_ID] = OrgConfig(org_id=DEFAULT_ORG_ID)
    return GatewayOrgsConfig(orgs=orgs)


class ProviderSettings(BaseModel):
    # Phase 7 ("search first" pass, Aug 2026): max_connections /
    # max_keepalive_connections size the shared httpx.AsyncClient each
    # adapter now builds ONCE at construction time (app/providers/base.py
    # / registry.py) instead of a fresh client per call -- see
    # docs/PHASE7_IMPLEMENTATION_GUIDE.md for the bug this replaces
    # ("socket exhaustion (TIME_WAIT) at 5,000+ RPS" from constructing a
    # brand-new TCP connection pool on every single provider call).
    # Per-provider, not one shared pair, matching this class's existing
    # per-provider api_key/base_url pattern -- Ollama's default is smaller
    # since it's typically a single local instance, not a horizontally
    # scaled hosted API.
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com"
    openai_max_connections: int = 100
    openai_max_keepalive_connections: int = 20
    anthropic_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_max_connections: int = 100
    anthropic_max_keepalive_connections: int = 20
    gemini_api_key: str | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_max_connections: int = 100
    gemini_max_keepalive_connections: int = 20
    ollama_base_url: str = "http://localhost:11434"
    ollama_api_key: str | None = None
    ollama_max_connections: int = 50
    ollama_max_keepalive_connections: int = 10


@lru_cache(maxsize=1)
def get_provider_settings() -> ProviderSettings:
    return ProviderSettings(
        openai_api_key=os.environ.get("OPENAI_API_KEY"),
        openai_base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com"),
        openai_max_connections=int(os.environ.get("OPENAI_MAX_CONNECTIONS", "100")),
        openai_max_keepalive_connections=int(os.environ.get("OPENAI_MAX_KEEPALIVE_CONNECTIONS", "20")),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        anthropic_base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        anthropic_max_connections=int(os.environ.get("ANTHROPIC_MAX_CONNECTIONS", "100")),
        anthropic_max_keepalive_connections=int(
            os.environ.get("ANTHROPIC_MAX_KEEPALIVE_CONNECTIONS", "20")
        ),
        gemini_api_key=os.environ.get("GEMINI_API_KEY"),
        gemini_base_url=os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"),
        gemini_max_connections=int(os.environ.get("GEMINI_MAX_CONNECTIONS", "100")),
        gemini_max_keepalive_connections=int(os.environ.get("GEMINI_MAX_KEEPALIVE_CONNECTIONS", "20")),
        ollama_base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        ollama_api_key=os.environ.get("OLLAMA_API_KEY"),
        ollama_max_connections=int(os.environ.get("OLLAMA_MAX_CONNECTIONS", "50")),
        ollama_max_keepalive_connections=int(os.environ.get("OLLAMA_MAX_KEEPALIVE_CONNECTIONS", "10")),
    )


def reset_provider_settings_cache() -> None:
    get_provider_settings.cache_clear()


# -- Phase 3: tiers.yaml (tier name -> ordered fallback chain) --------------


class TiersConfig(BaseModel):
    tiers: dict[str, list[str]] = Field(default_factory=dict)

    def chain_for(self, tier_name: str) -> list[str] | None:
        return self.tiers.get(tier_name)

    def all_links(self) -> list[str]:
        """
        Every "provider:model" id that appears in any tier's chain,
        deduplicated, order-preserving. Used by app/resilience/health.py
        to build the set of provider-model pairs the background prober
        should probe — the prober cares about every link a request could
        ever be routed to, not just tier names.
        """
        seen: dict[str, None] = {}
        for chain in self.tiers.values():
            for link in chain:
                seen[link] = None
        return list(seen.keys())


def load_tiers_config(path: str | Path | None = None) -> TiersConfig:
    config_path = Path(path) if path else Path(os.environ.get("TIERS_CONFIG_PATH", _DEFAULT_TIERS_PATH))
    if not config_path.exists():
        # A gateway that never got a tiers.yaml still works fine — every
        # request is just treated as a literal provider:model id with a
        # single-link "chain" (see app/resilience/fallback.py). This
        # matters for tests/environments that don't ship the file.
        return TiersConfig(tiers={})
    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    # Matches the TRD's own config/tiers.yaml example (Document 05 /
    # Document 06 Phase 3): each tier is a small object with a `chain`
    # list, not a bare list, leaving room for a later `description` or
    # per-tier override field without a schema break.
    tiers = {name: list((entry or {}).get("chain") or []) for name, entry in (raw.get("tiers") or {}).items()}
    return TiersConfig(tiers=tiers)


class GatewaySettings(BaseModel):
    """
    Infrastructure settings. See module docstring for what's new in Phase 3.
    """

    redis_url: str = "redis://localhost:6379/0"
    gateway_admin_key: str | None = None

    rate_limit_fail_open: bool = True
    rate_limit_key_ttl_seconds: int = 7200

    budget_warn_fraction: float = 0.8

    batch_queue_max_wait_seconds: float = 2.0
    batch_queue_poll_interval_seconds: float = 0.1
    batch_queue_max_length: int = 200

    pricing_path: str = str(_DEFAULT_PRICING_PATH)
    tiers_path: str = str(_DEFAULT_TIERS_PATH)
    orgs_path: str = str(_DEFAULT_ORGS_PATH)

    # -- Phase 3: health checking -----------------------------------------
    # TRD: "send lightweight test requests every 30 seconds... maintain a
    # status (healthy/degraded/down)". Enabled by default per developer
    # sign-off — this makes REAL calls to configured providers, so every
    # environment that enables it needs live credentials (or should point
    # `*_BASE_URL` at a mock). The test suite forces this to "false" (see
    # tests/unit/conftest.py) since it uses fake keys.
    health_check_enabled: bool = True
    health_check_interval_seconds: float = 30.0
    health_check_probe_timeout_seconds: float = 10.0
    # Rolling window (Document 05: health:{provider}:{model} sorted set,
    # "rolling 60s window, trimmed on write").
    health_window_seconds: int = 60
    # TRD: "if OpenAI's GPT-4 is timing out or returning 500 errors >30%
    # of the time, mark it degraded". No doc'd number for "down"; picked a
    # clearly-worse-than-degraded default (80%) so the three statuses are
    # meaningfully distinct — flagged as an assumption, tune freely.
    health_degraded_error_rate: float = 0.3
    health_down_error_rate: float = 0.8
    # No doc'd P99 SLA number either; 5s is a generous default for a chat
    # completion — flagged as an assumption, same as above.
    health_degraded_latency_p99_ms: float = 5000.0

    # -- Phase 3: circuit breaker ------------------------------------------
    # TRD Appendix A, resolved per its own v1 recommendation: "Start with
    # a simple fixed threshold (e.g. 5 failures in 10 requests) for v1;
    # move to a rolling error-rate percentage only if the fixed threshold
    # proves too twitchy under real traffic patterns."
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_window_size: int = 10
    circuit_breaker_cooldown_seconds: float = 60.0

    # Phase 7: the v2 extension the TRD's own Appendix A anticipated
    # ("move to a rolling error-rate percentage only if the fixed
    # threshold proves too twitchy"). "error_rate" mode reuses the exact
    # sorted-set rolling-window pattern app/resilience/health.py already
    # established (score=timestamp, ZREMRANGEBYSCORE trims to the last N
    # seconds) instead of circuit_record.lua's fixed-size LIST -- a
    # request-count window and a time window answer different questions
    # ("last 10 calls" vs "last 30 seconds", which differ a lot under
    # bursty or sparse traffic) and Doc 1's own critique was specifically
    # about the time-window case. Default mode is unchanged
    # ("fixed_count") so switching this on is opt-in, not a behavior
    # change for anyone who doesn't touch it.
    circuit_breaker_mode: str = "fixed_count"  # "fixed_count" | "error_rate"
    circuit_breaker_error_rate_window_seconds: float = 30.0
    circuit_breaker_error_rate_threshold: float = 0.5  # matches fixed_count's default 5-in-10 = 50%
    circuit_breaker_error_rate_minimum_samples: int = 5

    # -- Phase 3: retry with backoff ----------------------------------------
    # TRD: "retry the primary with exponential backoff (up to 3 retries)".
    # base/max delay bound the AWS-style "full jitter" formula in
    # app/resilience/retry.py: delay = random(0, min(max_delay, base * 2^n)).
    retry_max_attempts: int = 3
    retry_base_delay_seconds: float = 0.2
    retry_max_delay_seconds: float = 4.0

    # -- Phase 4: observability -----------------------------------------
    # Resource identity for every span this process emits.
    otel_service_name: str = "llm-gateway"
    # OTLP/HTTP traces endpoint (e.g. http://localhost:4318/v1/traces). If
    # unset, tracing.py still builds a real TracerProvider (so spans exist
    # and can be asserted on in-process, e.g. by a test's own
    # InMemorySpanExporter) but exports nothing — logged loudly once at
    # boot, same "log loudly either way" posture as every other optional
    # dependency in this codebase (Redis fail-open, budget fail-closed).
    otel_exporter_otlp_endpoint: str | None = None
    # Deliberately NOT "gen_ai_latest_experimental" by default — see
    # app/observability/tracing.py's module docstring for the "search
    # first" finding this pins against (nothing in the dedicated
    # semantic-conventions-genai repo is Stable as of Phase 4 sign-off,
    # and there's no versioned schema yet to opt into). Exposed as a
    # setting anyway (not hardcoded) so a developer can flip it later
    # without a code change once upstream stabilizes.
    otel_semconv_stability_opt_in: str | None = None
    # Compliance default per the TRD: prompt/completion text is NEVER a
    # span attribute, and not even an opt-in span EVENT unless explicitly
    # turned on here. See app/observability/tracing.py's
    # `maybe_capture_content` — the event is deliberately NOT gen_ai.*
    # namespaced (see that module's docstring) so this flag's blast radius
    # is fully self-contained.
    otel_capture_message_content: bool = False

    # Prometheus /metrics endpoint. Scrape interval is a Prometheus-side
    # concern (deploy/prometheus/prometheus.yml, Phase 5's container), not
    # something the gateway process controls — kept here only as the one
    # setting that IS the gateway's concern: whether /metrics exists at all.
    metrics_enabled: bool = True

    # Alerting (Phase 4 build task: "Route alerts to Slack... alert rule
    # thresholds"). Alertmanager itself is Phase 5's container; these are
    # the threshold VALUES the shipped deploy/prometheus/alerts.yml rules
    # are written against, kept as settings (not hardcoded into the YAML)
    # so re-tuning them doesn't require hand-editing PromQL — see
    # deploy/prometheus/alerts.yml's own header for how these map in.
    # health_down_error_rate (0.8, already defined above) doubles as the
    # "provider error rate above threshold" alert's trigger value per
    # explicit Phase 4 kickoff sign-off (keep Phase 3's assumed defaults
    # as-is) — not re-declared here to avoid two names for one number.
    alert_latency_p99_seconds: float = 5.0  # matches health_degraded_latency_p99_ms
    slack_webhook_url: str | None = None


@lru_cache(maxsize=1)
def get_gateway_settings() -> GatewaySettings:
    return GatewaySettings(
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        gateway_admin_key=os.environ.get("GATEWAY_ADMIN_KEY"),
        rate_limit_fail_open=_env_bool("RATE_LIMIT_FAIL_OPEN", True),
        rate_limit_key_ttl_seconds=int(os.environ.get("RATE_LIMIT_KEY_TTL_SECONDS", "7200")),
        budget_warn_fraction=float(os.environ.get("BUDGET_WARN_FRACTION", "0.8")),
        batch_queue_max_wait_seconds=float(os.environ.get("BATCH_QUEUE_MAX_WAIT_SECONDS", "2.0")),
        batch_queue_poll_interval_seconds=float(
            os.environ.get("BATCH_QUEUE_POLL_INTERVAL_SECONDS", "0.1")
        ),
        batch_queue_max_length=int(os.environ.get("BATCH_QUEUE_MAX_LENGTH", "200")),
        pricing_path=os.environ.get("PRICING_PATH", str(_DEFAULT_PRICING_PATH)),
        tiers_path=os.environ.get("TIERS_CONFIG_PATH", str(_DEFAULT_TIERS_PATH)),
        orgs_path=os.environ.get("ORGS_CONFIG_PATH", str(_DEFAULT_ORGS_PATH)),
        health_check_enabled=_env_bool("HEALTH_CHECK_ENABLED", True),
        health_check_interval_seconds=float(os.environ.get("HEALTH_CHECK_INTERVAL_SECONDS", "30.0")),
        health_check_probe_timeout_seconds=float(
            os.environ.get("HEALTH_CHECK_PROBE_TIMEOUT_SECONDS", "10.0")
        ),
        health_window_seconds=int(os.environ.get("HEALTH_WINDOW_SECONDS", "60")),
        health_degraded_error_rate=float(os.environ.get("HEALTH_DEGRADED_ERROR_RATE", "0.3")),
        health_down_error_rate=float(os.environ.get("HEALTH_DOWN_ERROR_RATE", "0.8")),
        health_degraded_latency_p99_ms=float(
            os.environ.get("HEALTH_DEGRADED_LATENCY_P99_MS", "5000.0")
        ),
        circuit_breaker_failure_threshold=int(
            os.environ.get("CIRCUIT_BREAKER_FAILURE_THRESHOLD", "5")
        ),
        circuit_breaker_window_size=int(os.environ.get("CIRCUIT_BREAKER_WINDOW_SIZE", "10")),
        circuit_breaker_cooldown_seconds=float(
            os.environ.get("CIRCUIT_BREAKER_COOLDOWN_SECONDS", "60.0")
        ),
        circuit_breaker_mode=os.environ.get("CIRCUIT_BREAKER_MODE", "fixed_count"),
        circuit_breaker_error_rate_window_seconds=float(
            os.environ.get("CIRCUIT_BREAKER_ERROR_RATE_WINDOW_SECONDS", "30.0")
        ),
        circuit_breaker_error_rate_threshold=float(
            os.environ.get("CIRCUIT_BREAKER_ERROR_RATE_THRESHOLD", "0.5")
        ),
        circuit_breaker_error_rate_minimum_samples=int(
            os.environ.get("CIRCUIT_BREAKER_ERROR_RATE_MINIMUM_SAMPLES", "5")
        ),
        retry_max_attempts=int(os.environ.get("RETRY_MAX_ATTEMPTS", "3")),
        retry_base_delay_seconds=float(os.environ.get("RETRY_BASE_DELAY_SECONDS", "0.2")),
        retry_max_delay_seconds=float(os.environ.get("RETRY_MAX_DELAY_SECONDS", "4.0")),
        otel_service_name=os.environ.get("OTEL_SERVICE_NAME", "llm-gateway"),
        otel_exporter_otlp_endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None,
        otel_semconv_stability_opt_in=os.environ.get("OTEL_SEMCONV_STABILITY_OPT_IN") or None,
        otel_capture_message_content=_env_bool("OTEL_CAPTURE_MESSAGE_CONTENT", False),
        metrics_enabled=_env_bool("METRICS_ENABLED", True),
        alert_latency_p99_seconds=float(os.environ.get("ALERT_LATENCY_P99_SECONDS", "5.0")),
        slack_webhook_url=os.environ.get("SLACK_WEBHOOK_URL") or None,
    )


def reset_gateway_settings_cache() -> None:
    get_gateway_settings.cache_clear()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
