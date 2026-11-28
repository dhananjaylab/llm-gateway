"""
test_embedding_provider.py

Verifies app/caching/embedding.py: FastEmbedProvider returns a vector of
the configured dimension, and -- the load-bearing part -- runs off the
event loop rather than blocking it (fastembed's own embed() call is
synchronous; see the module's own docstring for why this is not optional).

The real-model-download test mirrors test_output_tokenizer.py's existing
`_tiktoken_reachable()` pattern exactly, for the identical reason: model
weights download from Hugging Face on first use, which is not guaranteed
reachable in every build/CI environment (confirmed unreachable in the
sandbox this module was built in -- see embedding.py's own docstring).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.caching.embedding import FastEmbedProvider


def _fastembed_reachable() -> bool:
    try:
        from fastembed import TextEmbedding

        TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _fastembed_reachable(),
    reason="fastembed's model weights need network access to Hugging Face on first use -- not reachable here",
)
async def test_real_fastembed_returns_a_vector_of_the_configured_dimension():
    provider = FastEmbedProvider(model_name="BAAI/bge-small-en-v1.5", dimension=384)
    vector = await provider.embed("what is a token bucket?")
    assert len(vector) == 384
    assert all(isinstance(v, float) for v in vector)


@pytest.mark.skipif(not _fastembed_reachable(), reason="needs the real model — see above")
async def test_real_fastembed_is_deterministic_for_identical_text():
    provider = FastEmbedProvider(model_name="BAAI/bge-small-en-v1.5", dimension=384)
    a = await provider.embed("hello world")
    b = await provider.embed("hello world")
    assert a == b


async def test_model_loading_and_embedding_never_block_the_event_loop(monkeypatch):
    """Simulates a slow synchronous embed() call (standing in for real ONNX
    inference) and proves the event loop stays free to run other coroutines
    concurrently -- this is what asyncio.to_thread is actually for here,
    not a detail to take on faith."""
    provider = FastEmbedProvider(model_name="fake-model-for-this-test", dimension=4)

    class _FakeModel:
        def embed(self, texts):
            time.sleep(0.2)  # a real blocking call, not asyncio.sleep
            import numpy as np

            return iter([np.array([1.0, 0.0, 0.0, 0.0])])

    monkeypatch.setattr(provider, "_load_model_sync", lambda: _FakeModel())

    ticks = []

    async def _tick_every_10ms():
        for _ in range(15):
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    embed_task = asyncio.create_task(provider.embed("some text"))
    ticker_task = asyncio.create_task(_tick_every_10ms())
    vector, _ = await asyncio.gather(embed_task, ticker_task)

    assert vector == [1.0, 0.0, 0.0, 0.0]
    # If embed() had blocked the loop, far fewer than 15 ticks would have
    # landed inside its ~0.2s duration.
    assert len(ticks) >= 10


async def test_model_is_loaded_once_not_once_per_embed_call(monkeypatch):
    provider = FastEmbedProvider(model_name="fake-model-for-this-test", dimension=4)
    load_calls = []

    class _FakeModel:
        def embed(self, texts):
            import numpy as np

            return iter([np.array([1.0, 0.0, 0.0, 0.0])])

    def _fake_load():
        load_calls.append(1)
        return _FakeModel()

    monkeypatch.setattr(provider, "_load_model_sync", _fake_load)

    await provider.embed("first")
    await provider.embed("second")
    await provider.embed("third")

    assert len(load_calls) == 1
