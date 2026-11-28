"""
Embedding providers for the semantic cache layer (Phase 9b,
docs/PHASE9B_KICKOFF_SCOPING.md Sec 5.3/6).

`EmbeddingProvider` mirrors `app.providers.base.ProviderAdapter`'s own shape
deliberately -- one small async interface, one concrete implementation
wired in v1, room for more later without touching call sites (the kickoff
doc's open item 3: hosted embeddings as a v2 follow-on, not built here).

FASTEMBED IS SYNCHRONOUS -- READ BEFORE TOUCHING THIS FILE: `fastembed`'s
own `TextEmbedding.embed()` is a plain blocking generator (ONNX Runtime
CPU inference, no asyncio anywhere in that library). Calling it directly
from an `async def` would block the whole event loop for the duration of
every embed call -- exactly the kind of bug this codebase's own Phase 7
connection-pooling fix was written to eliminate elsewhere. `FastEmbedProvider`
therefore always runs the blocking call via `asyncio.to_thread`, never
inline. This is not optional and not a performance tweak to consider later.

MODEL LOADING IS NETWORK-DEPENDENT ON FIRST USE, VERIFIED AT BUILD TIME:
`TextEmbedding(model_name=...)` downloads its ONNX weights from Hugging
Face on first construction, then caches them locally (`fastembed`'s own
cache dir) for every process afterward -- the exact same "downloads once,
runs fully offline after that" shape `output_tokenizer.py`'s tiktoken
integration already has, and the exact same caveat: a restricted-egress
build/CI environment needs that cache pre-warmed at image-build time, or
this provider's first real call fails loudly (never silently) with
whatever `fastembed` itself raises. Confirmed directly against this
project's own sandboxed build environment while writing this module: the
package installs cleanly from PyPI, but the model download itself failed
because `huggingface.co` isn't in that environment's egress allowlist --
see `tests/unit/test_embedding_provider.py`'s own skip-if-unreachable
guard, which mirrors `test_output_tokenizer.py`'s existing
`_tiktoken_reachable()` pattern exactly, for the same reason.

The model is loaded ONCE per distinct `model_name` and cached on the
instance -- not once per call -- matching every other lazy-load-then-cache
pattern already established in this codebase (registry.py's
`@lru_cache`-wrapped adapter singletons, output_tokenizer.py's
`@lru_cache`-wrapped tokenizer loaders).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

logger = logging.getLogger("gateway.caching.embedding")


class EmbeddingProvider(Protocol):
    """One method, deliberately -- mirrors ProviderAdapter's minimalism.
    Implementations must be safe to call concurrently from multiple
    coroutines against the same instance (FastAPI hands one instance to
    every request via app.state)."""

    async def embed(self, text: str) -> list[float]:
        """Return a fixed-length embedding vector for `text`. Must always
        return the same dimension for a given instance -- callers size
        their Redis vector-set keys against it once and never re-check."""
        ...

    @property
    def dimension(self) -> int: ...


class FastEmbedProvider:
    """Default (and, in v1, only) `EmbeddingProvider` -- local ONNX
    inference via `fastembed`, zero per-call cost, zero provider
    credential. See module docstring for the synchronous-call and
    model-download caveats; both are handled here, not left to callers."""

    def __init__(self, *, model_name: str = "BAAI/bge-small-en-v1.5", dimension: int = 384) -> None:
        self._model_name = model_name
        self._dimension = dimension
        self._model = None  # lazy -- constructed on first embed(), not __init__
        self._load_lock = asyncio.Lock()

    @property
    def dimension(self) -> int:
        return self._dimension

    def _load_model_sync(self):
        from fastembed import TextEmbedding

        return TextEmbedding(model_name=self._model_name)

    async def _ensure_model(self):
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:  # re-check inside the lock -- classic double-checked init
                logger.info("embedding: loading %s (first use, may download once)", self._model_name)
                self._model = await asyncio.to_thread(self._load_model_sync)
        return self._model

    def _embed_sync(self, model, text: str) -> list[float]:
        # fastembed's embed() is a generator; a single string in, a single
        # vector out -- next() over it rather than list() to avoid building
        # an unused list wrapper for the common one-string call shape.
        vector = next(iter(model.embed([text])))
        return vector.tolist()

    async def embed(self, text: str) -> list[float]:
        model = await self._ensure_model()
        return await asyncio.to_thread(self._embed_sync, model, text)
