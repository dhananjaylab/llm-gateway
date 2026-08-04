"""
ProviderAdapter interface.

Per the TRD's Concise Implementation Guide: "Build providers/base.py as an
abstract base class (or Protocol) with translate_request(unified) -> dict,
call(payload) -> raw_response, translate_response(raw) -> unified." This
file is that interface, plus `stream()` for the SSE passthrough path and a
shared ProviderError so the route handler and (in Phase 3) the retry/
fallback layer can classify failures without importing any provider SDK.

No provider SDK type may appear outside this package — the route handler
only ever imports ProviderAdapter and UnifiedChat* types.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from app.core.schema import UnifiedChatRequest, UnifiedChatResponse, UnifiedStreamChunk


class ProviderError(Exception):
    """
    Raised by an adapter's call()/stream() on any non-2xx upstream response
    or transport failure.

    `retryable` is set by the adapter based on the TRD's error
    classification table (429/502/503/504/timeouts = retryable;
    400/401/403 = not). Phase 1 stubs the retry/fallback layer, so this
    classification is inert for now — the route handler just reads it back
    off the exception to build a clean error body. Phase 3 wires it into
    real retry/circuit-breaker decisions without changing this class.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        error_type: str = "provider_error",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.error_type = error_type


class ProviderAdapter(ABC):
    """One adapter per upstream provider. Stateless aside from config."""

    provider_name: str

    @abstractmethod
    def translate_request(self, request: UnifiedChatRequest, *, provider_model: str) -> dict:
        """Unified schema -> provider-native JSON payload (non-streaming or streaming)."""

    @abstractmethod
    async def call(self, payload: dict) -> dict:
        """Execute the non-streaming call. Returns the provider's raw JSON body."""

    @abstractmethod
    def translate_response(
        self, raw: dict, *, request: UnifiedChatRequest, provider_model: str
    ) -> UnifiedChatResponse:
        """Provider-native JSON body -> unified schema."""

    @abstractmethod
    def stream(
        self, payload: dict, *, request: UnifiedChatRequest, provider_model: str
    ) -> AsyncIterator[UnifiedStreamChunk]:
        """
        Execute the streaming call. Yields normalized chunks as they arrive
        from upstream — implementations must not buffer the full response
        before yielding the first chunk (this is what keeps TTFT low).
        """
