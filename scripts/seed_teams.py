#!/usr/bin/env python3
"""
One-shot: seed Redis's team_config:* keys from config/teams.yaml.

Usually unnecessary — app/main.py's lifespan already does this
automatically on startup if Redis has no teams yet (see
TeamConfigStore.seed_from_yaml_if_empty). This script exists for the
Phase 5 docker-compose "demo teams" init step (a one-shot container that
runs before the gateway starts, per Document 06 Phase 5's
scripts/setup_demo_teams.py) and for manually re-seeding a fresh Redis
during local development without starting the whole app.

Usage:
    python scripts/seed_teams.py [--redis-url redis://localhost:6379/0]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_gateway_settings, load_teams_config
from app.core.redis_client import build_redis_client
from app.core.team_store import TeamConfigStore


async def main(redis_url: str | None) -> None:
    settings = get_gateway_settings()
    redis = build_redis_client(redis_url or settings.redis_url)
    try:
        store = TeamConfigStore(redis)
        gateway_config = load_teams_config()
        seeded = await store.seed_from_yaml_if_empty(gateway_config)
        if seeded:
            print(f"Seeded {seeded} teams into Redis.")
        else:
            print("Redis already has team config — nothing to do (flush Redis to reseed).")
    finally:
        await redis.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--redis-url", default=None)
    args = parser.parse_args()
    asyncio.run(main(args.redis_url))
