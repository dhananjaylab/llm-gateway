"""
Configuration loading.

Phase 1 scope: load config/teams.yaml once at process startup into memory.
This is deliberately a plain load, not a hot-reload watcher — the TRD
folder structure's final config.py docstring ("YAML load + hot-reload
watcher") describes where this file ends up after Phase 2, which adds the
watchdog/inotify-based reload and wires it to the Admin API's PATCH
endpoints. Building the watcher now would have no admin endpoint to
trigger it and no rate-limit state to actually reload, so Phase 1 stops at
a clean, swappable load function (`load_teams_config`) that Phase 2 calls
on a timer/file-event instead of once.

Provider credentials are read from environment variables per the TRD's
env var table and never live in teams.yaml.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "teams.yaml"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Load environment variables from the workspace .env file before any
# provider settings are read. This allows the app to pick up Gemini and the
# other provider credentials when started locally without manual export.
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
    # Re-load the workspace .env values each time the cached settings are
    # requested so local development and tests can rely on the committed
    # .env file even after environment variables were cleared or mutated.
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
