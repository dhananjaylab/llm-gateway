"""
Provider registry.

Phase 1/2 scope: a request's `model` field is a provider-qualified id, e.g.
"openai:gpt-5.4", "anthropic:claude-sonnet-5", "ollama:llama3.2" — the
registry just splits on the first ":" and looks up the adapter singleton
for that provider prefix.

Phase 3 adds an abstract-tier layer *on top* of this (app/resilience/
fallback.py, resolving tiers.yaml's tier -> ordered chain of these same
provider-qualified ids). This module deliberately does not know about
tiers — resolve_model() is the exact seam Phase 3 wraps, not replaces:
the fallback router calls resolve_model() once per chain link.
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import ProviderSettings, get_provider_settings
from app.providers.anthropic_adapter import AnthropicAdapter
from app.providers.base import ProviderAdapter
from app.providers.gemini_adapter import GeminiAdapter
from app.providers.ollama_adapter import OllamaAdapter
from app.providers.openai_adapter import OpenAIAdapter


class UnknownProviderError(ValueError):
    def __init__(self, model_id: str, known: tuple[str, ...]):
        self.model_id = model_id
        super().__init__(
            f"'{model_id}' is not a valid provider-qualified model id "
            f"(expected '<provider>:<model>' with provider in {known})"
        )


@lru_cache(maxsize=1)
def _adapters() -> dict[str, ProviderAdapter]:
    settings: ProviderSettings = get_provider_settings()
    adapters: dict[str, ProviderAdapter] = {}
    if settings.openai_api_key:
        adapters["openai"] = OpenAIAdapter(
            api_key=settings.openai_api_key, base_url=settings.openai_base_url
        )
    if settings.anthropic_api_key:
        adapters["anthropic"] = AnthropicAdapter(
            api_key=settings.anthropic_api_key, base_url=settings.anthropic_base_url
        )
    if settings.gemini_api_key:
        adapters["gemini"] = GeminiAdapter(
            api_key=settings.gemini_api_key, base_url=settings.gemini_base_url
        )
    # Ollama never requires a key for a local install, so it is always
    # registered (the base_url alone determines local vs. cloud).
    adapters["ollama"] = OllamaAdapter(
        base_url=settings.ollama_base_url, api_key=settings.ollama_api_key
    )
    return adapters


def resolve_model(model_id: str) -> tuple[ProviderAdapter, str]:
    """
    "openai:gpt-5.4" -> (OpenAIAdapter instance, "gpt-5.4")

    Raises UnknownProviderError if the prefix has no configured adapter
    (either malformed input, or the provider's API key was never set).
    """
    if ":" not in model_id:
        raise UnknownProviderError(model_id, tuple(_adapters().keys()))
    provider_name, _, provider_model = model_id.partition(":")
    adapters = _adapters()
    if provider_name not in adapters or not provider_model:
        raise UnknownProviderError(model_id, tuple(adapters.keys()))
    return adapters[provider_name], provider_model


def all_configured_provider_models(model_ids: list[str]) -> list[tuple[str, str, str]]:
    """
    Resolve a list of "provider:model" ids to (model_id, provider_name,
    provider_model) triples, silently skipping any id whose provider has
    no configured adapter (e.g. a chain entry for a provider whose API key
    was never set in this environment). Used by app/resilience/health.py
    to build the set of provider-model pairs the background health prober
    should probe, without crashing if e.g. GEMINI_API_KEY is unset.
    """
    out: list[tuple[str, str, str]] = []
    for model_id in model_ids:
        try:
            adapter, provider_model = resolve_model(model_id)
        except UnknownProviderError:
            continue
        out.append((model_id, adapter.provider_name, provider_model))
    return out


async def close_all_adapters() -> None:
    """
    Phase 7: release transport resources held by all cached adapters.
    Called during app shutdown (see app/main.py's lifespan). A no-op if
    no adapters were ever instantiated (cache is empty).
    """
    adapters = _adapters()
    for adapter in adapters.values():
        await adapter.aclose()


def reset_registry_cache() -> None:
    """Test-only hook: clear the memoized adapter singletons."""
    # Also clear the provider settings cache so tests that manipulate
    # environment variables (via `monkeypatch.delenv`) get a fresh
    # `ProviderSettings` instance when `_adapters()` is next called.
    from app.core.config import reset_provider_settings_cache

    _adapters.cache_clear()
    reset_provider_settings_cache()
