"""
app/ratelimit/output_tokenizer.py

Phase 7: partial-output token counting for "Advanced Token Accounting for
Cancelled and Aborted Streams" (see docs/PHASE7_IMPLEMENTATION_GUIDE.md and
app/api/v1_chat.py's `_reconcile_and_bill_partial`).

Distinct from app/ratelimit/estimator.py's PRE-flight input-token estimate
(sizes a reservation before a call starts): this module counts OUTPUT text
that has ALREADY been generated and streamed to the client, after a
disconnect or mid-stream failure cut a response short and no terminal
usage chunk from the provider ever arrived to report the real count.

Per-provider strategy is "most accurate available," not one-size-fits-all
— confirmed by actually loading each library in this pass, not assumed:

  - openai:     tiktoken (offline, exact — but see NETWORK NOTE below)
  - gemini:     google-genai's local_tokenizer (offline, exact, no network
                fetch needed once `sentencepiece` is installed — confirmed
                working end-to-end). Google's own SDK marks this
                ExperimentalWarning ("may change in the future").
  - anthropic:  Anthropic publishes NO offline tokenizer at all (confirmed
                against their current docs). The only exact option is a
                network round trip to their `count_tokens` API, which is
                the wrong trade for a best-effort cleanup path that's
                already mid-teardown on a dropped connection. Uses
                Anthropic's OWN documented ~3.5-characters-per-token
                approximation instead — not a shortcut invented here,
                their suggested fallback ratio for exactly this situation.
  - anything else (ollama, an unrecognized provider, or a tiktoken/gemini
    load failure): the same 4-characters-per-token heuristic
    app/ratelimit/estimator.py already uses for pre-flight reservation.
    Ollama is $0/token per config/pricing.yaml (self-hosted) — precision
    here affects only the TPM bucket refund, never billing, and there is
    no single tokenizer across arbitrary community-uploaded local models
    to standardize on anyway.

NETWORK NOTE (confirmed by actually running this, not assumed): tiktoken's
encoding tables are NOT bundled in the pip package — `tiktoken.get_encoding()`
/ `encoding_for_model()` fetch the BPE merge file from
`openaipublic.blob.core.windows.net` on first use per process, caching it
locally after that (respects `TIKTOKEN_CACHE_DIR`). In a restricted-egress
environment — see Phase 10's enterprise/air-gapped deployment track — that
fetch fails unless the cache is pre-populated at image-build time. This
module treats ANY load or encode failure — network, unrecognized model,
missing optional dependency, whatever — as a signal to fall back to the
heuristic, never as a reason to raise and break the disconnect-cleanup
path it's called from. If you're deploying somewhere restricted-egress,
either pre-warm `TIKTOKEN_CACHE_DIR` at image build time, or accept that
OpenAI partial-stream accounting will use the heuristic there too — both
are fine; the heuristic is Anthropic's own suggested class of fallback,
not a degraded/broken state.
"""

from __future__ import annotations

import logging
from functools import lru_cache

logger = logging.getLogger("gateway.output_tokenizer")

_CHARS_PER_TOKEN_DEFAULT = 4  # matches app/ratelimit/estimator.py exactly
_CHARS_PER_TOKEN_ANTHROPIC = 3.5  # Anthropic's own documented approximation


def _heuristic(text: str, chars_per_token: float = _CHARS_PER_TOKEN_DEFAULT) -> int:
    return max(1, int(len(text) / chars_per_token))


@lru_cache(maxsize=8)
def _tiktoken_encoding_for_model(model_served: str):
    """
    Cached per model_served so the (network-fetching, on first call)
    encoding-table load happens at most once per distinct model string
    per process, not once per disconnect. Returns None — never raises —
    on any failure, so callers always have a clean "fall back" signal.
    """
    try:
        import tiktoken
    except ImportError:
        logger.warning("tiktoken is not installed — falling back to the heuristic for openai")
        return None

    try:
        return tiktoken.encoding_for_model(model_served)
    except KeyError:
        # Unrecognized/dated model string (e.g. "gpt-5.6-sol-2026-03-05")
        # — tiktoken's own model-to-encoding map won't have every dated
        # snapshot any more than config/pricing.yaml's lookup table does
        # (see app/core/pricing.py's own note on this exact problem).
        # o200k_base is the current-generation GPT encoding family; close
        # enough for an estimate that only ever affects a TPM refund and
        # best-effort partial billing, never the provider's own invoice.
        try:
            return tiktoken.get_encoding("o200k_base")
        except Exception:
            logger.warning(
                "tiktoken encoding unavailable (restricted-egress environment? "
                "see output_tokenizer.py's NETWORK NOTE) — falling back to the "
                "character-ratio heuristic for openai partial-stream accounting",
                exc_info=True,
            )
            return None
    except Exception:
        logger.warning(
            "tiktoken encoding unavailable — falling back to the character-ratio "
            "heuristic for openai partial-stream accounting",
            exc_info=True,
        )
        return None


@lru_cache(maxsize=8)
def _gemini_local_tokenizer(model_served: str):
    """Same caching / never-raises contract as _tiktoken_encoding_for_model."""
    try:
        from google.genai import local_tokenizer
    except ImportError:
        logger.warning(
            "google-genai (or its sentencepiece dependency) is not installed — "
            "falling back to the character-ratio heuristic for gemini partial-stream accounting"
        )
        return None
    try:
        return local_tokenizer.LocalTokenizer(model_name=model_served)
    except Exception:
        logger.warning(
            "google-genai LocalTokenizer unavailable for model=%s — falling back "
            "to the character-ratio heuristic for gemini partial-stream accounting",
            model_served,
            exc_info=True,
        )
        return None


def count_partial_output_tokens(provider: str, model_served: str, text: str) -> int:
    """
    Best-available estimate of how many OUTPUT tokens `text` (an
    already-streamed partial completion) represents. Never raises — every
    failure mode degrades to the same character-ratio heuristic
    app/ratelimit/estimator.py already uses for pre-flight reservation, so
    a tokenizer-library problem can never turn a disconnect-cleanup path
    into an unhandled exception.
    """
    if not text:
        return 0

    if provider == "openai":
        encoding = _tiktoken_encoding_for_model(model_served)
        if encoding is not None:
            try:
                return max(1, len(encoding.encode(text)))
            except Exception:
                logger.warning("tiktoken.encode failed — falling back to heuristic", exc_info=True)
        return _heuristic(text)

    if provider == "gemini":
        tokenizer = _gemini_local_tokenizer(model_served)
        if tokenizer is not None:
            try:
                result = tokenizer.count_tokens(text)
                return max(1, result.total_tokens)
            except Exception:
                logger.warning(
                    "google-genai LocalTokenizer.count_tokens failed — falling back to heuristic",
                    exc_info=True,
                )
        return _heuristic(text)

    if provider == "anthropic":
        return _heuristic(text, chars_per_token=_CHARS_PER_TOKEN_ANTHROPIC)

    return _heuristic(text)


def reset_tokenizer_caches() -> None:
    """Test-only hook, same pattern as app/providers/registry.py's
    reset_registry_cache() — lets a test force a fresh load attempt (e.g.
    to simulate a load failure via monkeypatch) instead of reusing
    whatever an earlier test in the same process already cached.

    Defensive about `cache_clear` existing: a test that monkeypatches
    `_tiktoken_encoding_for_model`/`_gemini_local_tokenizer` to a plain
    lambda (to simulate a load failure) replaces the lru_cache wrapper
    entirely for the duration of that test — if this fixture's teardown
    runs before monkeypatch's own revert (fixture teardown ordering is
    not something to depend on here), the module attribute briefly has
    no `cache_clear` at all. Skipping cleanly in that case is correct:
    there is nothing to clear on a plain lambda.
    """
    if hasattr(_tiktoken_encoding_for_model, "cache_clear"):
        _tiktoken_encoding_for_model.cache_clear()
    if hasattr(_gemini_local_tokenizer, "cache_clear"):
        _gemini_local_tokenizer.cache_clear()
