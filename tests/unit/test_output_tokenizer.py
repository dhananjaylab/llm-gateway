"""
test_output_tokenizer.py

Phase 7: verifies app/ratelimit/output_tokenizer.py's per-provider
dispatch and — the part that matters most — that every failure mode
degrades to the character-ratio heuristic instead of raising. A
tokenizer-library problem must never turn a disconnect-cleanup path into
an unhandled exception.

The "real tiktoken produces an exact count" test is network-dependent
(tiktoken fetches its BPE merge file from openaipublic.blob.core.windows.net
on first use per process — see output_tokenizer.py's own NETWORK NOTE) and
skips gracefully, same pattern tests/integration/conftest.py's
`requires_live_stack` already establishes for "needs external reachability"
tests, rather than failing the whole suite in a restricted-egress
environment (exactly the kind of environment the Phase 10 on-prem track
is about). The gemini local tokenizer needs no network (confirmed by
running it), so its test always runs.
"""

from __future__ import annotations

import pytest

from app.ratelimit.output_tokenizer import (
    _CHARS_PER_TOKEN_ANTHROPIC,
    _CHARS_PER_TOKEN_DEFAULT,
    count_partial_output_tokens,
    reset_tokenizer_caches,
)


@pytest.fixture(autouse=True)
def _fresh_tokenizer_caches():
    reset_tokenizer_caches()
    yield
    reset_tokenizer_caches()


def _tiktoken_reachable() -> bool:
    try:
        import tiktoken

        tiktoken.get_encoding("o200k_base")
        return True
    except Exception:
        return False


def test_empty_text_is_always_zero_tokens_regardless_of_provider():
    assert count_partial_output_tokens("openai", "gpt-5.6-sol", "") == 0
    assert count_partial_output_tokens("anthropic", "claude-sonnet-5", "") == 0
    assert count_partial_output_tokens("unknown-provider", "whatever", "") == 0


def test_anthropic_uses_its_own_documented_heuristic_ratio_not_openais():
    """Anthropic publishes no offline tokenizer (confirmed against their
    current docs) -- this is their own suggested ~3.5 chars/token
    approximation, not the project's generic 4 chars/token default."""
    text = "x" * 35  # 35 chars
    anthropic_count = count_partial_output_tokens("anthropic", "claude-sonnet-5", text)
    default_count = int(35 / _CHARS_PER_TOKEN_DEFAULT)
    anthropic_expected = int(35 / _CHARS_PER_TOKEN_ANTHROPIC)

    assert anthropic_count == anthropic_expected
    assert anthropic_count != default_count, "must use Anthropic's own ratio, not the generic default"


def test_ollama_and_unrecognized_providers_use_the_generic_default_heuristic():
    text = "x" * 40
    expected = max(1, int(40 / _CHARS_PER_TOKEN_DEFAULT))
    assert count_partial_output_tokens("ollama", "llama3.2", text) == expected
    assert count_partial_output_tokens("some-future-provider", "whatever-model", text) == expected


def test_gemini_uses_the_real_local_tokenizer_when_available():
    """google-genai's LocalTokenizer needs no network fetch (confirmed by
    running it) -- unlike tiktoken, this test always runs for real."""
    text = "The quick brown fox jumps over the lazy dog."
    count = count_partial_output_tokens("gemini", "gemini-2.0-flash", text)
    # A real SentencePiece tokenization of this sentence is nowhere near
    # a naive 4-chars-per-token guess (45 chars -> heuristic would say
    # ~11) -- assert it's in a sane real-tokenizer ballpark instead of a
    # brittle exact pin (Gemini's SDK marks this ExperimentalWarning:
    # "may change in the future"), while still proving it's NOT just
    # silently falling back to the heuristic.
    heuristic_count = max(1, int(len(text) / _CHARS_PER_TOKEN_DEFAULT))
    assert count > 0
    assert count != heuristic_count, (
        "expected the real local tokenizer's count, not a silent fallback to the heuristic -- "
        "if this fails, check whether sentencepiece/google-genai are actually installed"
    )


def test_gemini_falls_back_to_heuristic_when_the_local_tokenizer_cannot_load(monkeypatch):
    """Simulates the library being unavailable (e.g. sentencepiece not
    installed) -- must degrade cleanly, never raise."""
    import app.ratelimit.output_tokenizer as tok_module

    monkeypatch.setattr(tok_module, "_gemini_local_tokenizer", lambda model_served: None)
    text = "x" * 20
    assert count_partial_output_tokens("gemini", "gemini-2.0-flash", text) == max(
        1, int(20 / _CHARS_PER_TOKEN_DEFAULT)
    )


def test_gemini_falls_back_to_heuristic_when_count_tokens_itself_raises(monkeypatch):
    import app.ratelimit.output_tokenizer as tok_module

    class _BrokenTokenizer:
        def count_tokens(self, text):
            raise RuntimeError("simulated SDK failure")

    monkeypatch.setattr(tok_module, "_gemini_local_tokenizer", lambda model_served: _BrokenTokenizer())
    text = "x" * 20
    assert count_partial_output_tokens("gemini", "gemini-2.0-flash", text) == max(
        1, int(20 / _CHARS_PER_TOKEN_DEFAULT)
    )


def test_openai_falls_back_to_heuristic_when_tiktoken_cannot_load(monkeypatch):
    """The load-failure path this project will actually exercise in a
    restricted-egress deployment (see output_tokenizer.py's NETWORK NOTE)
    -- simulated here so the test is deterministic regardless of whether
    THIS process happens to have network access to fetch tiktoken's
    encoding table."""
    import app.ratelimit.output_tokenizer as tok_module

    monkeypatch.setattr(tok_module, "_tiktoken_encoding_for_model", lambda model_served: None)
    text = "x" * 20
    assert count_partial_output_tokens("openai", "gpt-5.6-sol", text) == max(
        1, int(20 / _CHARS_PER_TOKEN_DEFAULT)
    )


def test_openai_falls_back_to_heuristic_when_encode_itself_raises(monkeypatch):
    import app.ratelimit.output_tokenizer as tok_module

    class _BrokenEncoding:
        def encode(self, text):
            raise RuntimeError("simulated tiktoken failure")

    monkeypatch.setattr(tok_module, "_tiktoken_encoding_for_model", lambda model_served: _BrokenEncoding())
    text = "x" * 20
    assert count_partial_output_tokens("openai", "gpt-5.6-sol", text) == max(
        1, int(20 / _CHARS_PER_TOKEN_DEFAULT)
    )


@pytest.mark.skipif(
    not _tiktoken_reachable(),
    reason=(
        "tiktoken's encoding table needs network access on first use in this environment "
        "(see output_tokenizer.py's NETWORK NOTE) -- not reachable here"
    ),
)
def test_openai_uses_the_real_tiktoken_encoding_when_reachable():
    text = "The quick brown fox jumps over the lazy dog."
    count = count_partial_output_tokens("openai", "gpt-5.6-sol", text)
    heuristic_count = max(1, int(len(text) / _CHARS_PER_TOKEN_DEFAULT))
    assert count > 0
    assert count != heuristic_count, "expected the real tiktoken count, not a silent fallback"


def test_tokenizer_loaders_are_cached_per_model_not_reloaded_every_call():
    """The (potentially network-fetching) load only needs to happen once
    per distinct model_served per process -- not once per disconnect."""
    import app.ratelimit.output_tokenizer as tok_module

    tok_module._gemini_local_tokenizer("gemini-2.0-flash")
    tok_module._gemini_local_tokenizer("gemini-2.0-flash")
    info = tok_module._gemini_local_tokenizer.cache_info()
    assert info.hits == 1
    assert info.misses == 1
