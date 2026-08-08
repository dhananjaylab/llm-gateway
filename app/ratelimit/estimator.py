"""
Pre-execution token estimation (TRD, "Dual-Key Estimation vs. Post-
Execution Reconciliation", Phase 1 of the two-phase reservation protocol).

The TRD's formula is `estimate = input_tokens_from_payload +
requested_max_tokens`. Getting `input_tokens_from_payload` exactly right
needs a real tokenizer per provider (tiktoken for OpenAI, Anthropic's
`count_tokens` endpoint for Claude, SentencePiece for Llama/Ollama) — three
extra dependencies and, for Anthropic, a network round-trip before the
real call even starts. That's disproportionate for what this number is
used for: it only sizes a *reservation* that `RateLimiter.reconcile()`
corrects the moment real usage is known (see app/ratelimit/limiter.py). An
over-estimate costs the team some burst headroom for a few hundred
milliseconds, not a wrong answer.

So: a cheap, provider-agnostic heuristic (~4 characters per token for
English text — the same rule of thumb OpenAI's own docs use, and what the
project's reference HTML prototype already used for its live demo cost
estimate). If this gateway later serves heavily non-English or code-heavy
traffic, swapping this for tiktoken is a one-function change — nothing
else in the rate-limit pipeline depends on the estimate being exact,
by design.
"""

from __future__ import annotations

from app.core.schema import UnifiedChatRequest

_CHARS_PER_TOKEN = 4


def estimate_input_tokens(request: UnifiedChatRequest) -> int:
    chars = sum(len(m.content) for m in request.messages)
    if request.system:
        chars += len(request.system)
    return max(1, chars // _CHARS_PER_TOKEN)


def estimate_reserved_tokens(request: UnifiedChatRequest) -> int:
    """Total TPM reservation: estimated input + the client's requested ceiling."""
    return estimate_input_tokens(request) + request.max_tokens
