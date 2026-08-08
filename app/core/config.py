"""
Configuration loading.

Phase 1 scope (unchanged): `load_teams_config()` reads config/teams.yaml
once. Phase 2 uses this ONLY as the bootstrap seed for Redis (see
app/core/team_store.py) — after first boot, Redis is the runtime source
of truth for team config, and the Admin API is the only mutation path.
`TeamConfig` / `TeamPolicy` / `GatewayConfig` are unchanged in shape from
Phase 1, so nothing downstream (schema, policy, adapters) had to change.

New in Phase 2: `GatewaySettings` — Redis connection, admin key, and the
knobs for the two "open decisions" the TRD Appendix A explicitly left for
developer sign-off (Redis fail-open/fail-closed for rate limiting; the
budget warn threshold). Defaults below match the TRD's own recommendation
so the gateway has a safe, documented behavior out of the box, but every
one of these is overridable via environment variable without a code
change — see .env.example.
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
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(_PROJECT_ROOT / ".env", override=False)


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


class ProviderSettings(BaseModel):
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com"
    anthropic_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    gemini_api_key: str | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    ollama_base_url: str = "http://localhost:11434"
    ollama_api_key: str | None = None


@lru_cache(maxsize=1)
def get_provider_settings() -> ProviderSettings:
    load_dotenv(_PROJECT_ROOT / ".env", override=False)
    return ProviderSettings(
        openai_api_key=os.environ.get("OPENAI_API_KEY"),
        openai_base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        anthropic_base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        gemini_api_key=os.environ.get("GEMINI_API_KEY"),
        gemini_base_url=os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"),
        ollama_base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        ollama_api_key=os.environ.get("OLLAMA_API_KEY"),
    )


def reset_provider_settings_cache() -> None:
    get_provider_settings.cache_clear()


class GatewaySettings(BaseModel):
    """
    Phase 2 infrastructure settings. Two fields directly resolve open
    decisions flagged in Document 06 Appendix A ("Open decisions requiring
    developer sign-off") — resolved here per the TRD's own recommendation,
    not left dangling, but fully overridable:

    - `rate_limit_fail_open`: TRD recommendation "fail-open with a
      conservative local limiter for rate limiting" -> default True.
    - Budget enforcement is *always* fail-closed (TRD: "Fail-closed for
      budget enforcement") — this is not a toggle; see app/ratelimit/budget.py.
    """

    redis_url: str = "redis://localhost:6379/0"
    gateway_admin_key: str | None = None

    rate_limit_fail_open: bool = True
    rate_limit_key_ttl_seconds: int = 7200  # Document 05: rl:{team}:{rpm,tpm} TTL

    budget_warn_fraction: float = 0.8  # Document 05/TRD: 80% soft-warning threshold

    batch_queue_max_wait_seconds: float = 2.0
    batch_queue_poll_interval_seconds: float = 0.1
    batch_queue_max_length: int = 200

    pricing_path: str = str(_DEFAULT_PRICING_PATH)


@lru_cache(maxsize=1)
def get_gateway_settings() -> GatewaySettings:
    load_dotenv(_PROJECT_ROOT / ".env", override=False)
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
    )


def reset_gateway_settings_cache() -> None:
    get_gateway_settings.cache_clear()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
