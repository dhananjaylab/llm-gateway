from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MOCK_PROVIDERS_DIR = _REPO_ROOT / "deploy" / "mock-providers"
# deploy/mock-providers/main.py is a standalone service (own Dockerfile,
# own requirements.txt) -- it isn't part of the `app` package's import
# path. Adding it to sys.path here, rather than shipping an __init__.py
# that would make it importable as a submodule of something, keeps it
# genuinely standalone (the container that runs it never has the
# gateway's own app/ on its PYTHONPATH at all) while still letting these
# tests import it directly for the in-process wire-compat checks in
# test_mock_provider_wire_compat.py.
sys.path.insert(0, str(_MOCK_PROVIDERS_DIR))

# Live-stack integration tests (test_full_stack_integration.py,
# test_dashboard_accuracy.py) target a real `docker-compose up` gateway,
# not the fakeredis in-process app the unit suite uses. Wire-compat tests
# do NOT need this -- they run the real adapters against the mock
# service's ASGI app directly, no network, no live stack -- so only the
# two live-stack files import `requires_live_stack` below.
GATEWAY_BASE_URL = os.environ.get("GATEWAY_BASE_URL", "http://localhost:8000")
MOCK_PROVIDERS_BASE_URL = os.environ.get("MOCK_PROVIDERS_BASE_URL", "http://localhost:9000")
PROMETHEUS_BASE_URL = os.environ.get("PROMETHEUS_BASE_URL", "http://localhost:9090")


def _reachable(url: str, timeout: float = 1.0) -> bool:
    try:
        return httpx.get(url, timeout=timeout).status_code < 500
    except httpx.HTTPError:
        return False


requires_live_stack = pytest.mark.skipif(
    not _reachable(f"{GATEWAY_BASE_URL}/healthz"),
    reason=(
        f"gateway not reachable at {GATEWAY_BASE_URL} -- this test exercises the real "
        "docker-compose stack (Phase 5), not the fakeredis in-process app the unit suite "
        "uses. Run `docker compose up -d` first, or see README's 'Run the integration "
        "suite' section."
    ),
)


@pytest.fixture
def mock_providers_client() -> httpx.Client:
    with httpx.Client(base_url=MOCK_PROVIDERS_BASE_URL, timeout=10.0) as client:
        yield client


@pytest.fixture
def gateway_client() -> httpx.Client:
    with httpx.Client(base_url=GATEWAY_BASE_URL, timeout=30.0) as client:
        yield client
