#!/usr/bin/env python3
"""
scripts/setup_demo_teams.py

Document 06 Phase 5 build task, verbatim: "a setup script that creates
demo teams with different rate limits so reviewers can see the system in
action immediately." Run once by the `demo-seed` service in
docker-compose.yml, after Redis is healthy and the gateway itself is
already answering /readyz.

This is a thin wrapper, not new seeding logic: the actual write path is
the exact same `TeamConfigStore.seed_from_yaml_if_empty()`
scripts/seed_teams.py already calls (and that app/main.py's own lifespan
already calls automatically on first boot -- see that module's Phase 2
docstring). Seeding was never the gap; the gap is that config/teams.yaml
only stores SHA-256 hashes (per Document 05's own SECURITY NOTE), so
nothing before this script ever told a reviewer what the *raw* keys are.
The three keys below are already public -- they're the exact values
config/teams.yaml's own header comments and this project's README curl
example both already print in plain text -- this script isn't
introducing a new secret, just collecting what a reviewer needs into one
place at the end of `docker-compose up`.

Usage (inside the demo-seed container; also runnable locally):
    python scripts/setup_demo_teams.py [--redis-url redis://localhost:6379/0]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_gateway_settings, load_teams_config
from app.core.redis_client import build_redis_client
from app.core.team_store import TeamConfigStore

# Matches tests/unit/conftest.py's own DATA_SCIENCE_KEY / PRODUCT_ENG_KEY /
# BATCH_DEVS_KEY constants and config/teams.yaml's committed hashes --
# see this file's docstring for why declaring them again here (rather
# than importing from tests/, which isn't shipped in the runtime image
# and shouldn't be a script's dependency) is not a new secret exposure.
_DEMO_RAW_KEYS = {
    "data-science": "sk-gw-datascience-demo-001",
    "product-eng": "sk-gw-producteng-demo-002",
    "batch-devs": "sk-gw-batchdevs-demo-003",
}

_BANNER_WIDTH = 78


def _rule() -> str:
    return "=" * _BANNER_WIDTH


async def main(redis_url: str | None, gateway_url: str) -> None:
    settings = get_gateway_settings()
    redis = build_redis_client(redis_url or settings.redis_url)
    try:
        store = TeamConfigStore(redis)
        seeded = await store.seed_from_yaml_if_empty(load_teams_config())
        if seeded:
            print(f"Seeded {seeded} team(s) into Redis from config/teams.yaml.")
        else:
            print("Redis already had team config -- reusing it (flush Redis to reseed).")

        print()
        print(_rule())
        print("  LLM GATEWAY -- DEMO IS READY")
        print(_rule())
        print(f"  Gateway:      {gateway_url}")
        print(f"  Metrics:      {gateway_url}/metrics")
        print("  Grafana:      http://localhost:3000  (admin/admin)")
        print("  Jaeger UI:    http://localhost:16686")
        print("  Prometheus:   http://localhost:9090")
        print()
        print("  Demo teams (raw API key -- these are public demo values,")
        print("  already printed in config/teams.yaml's own header comment):")
        print()

        for team_id, raw_key in _DEMO_RAW_KEYS.items():
            team = await store.get_team(team_id, use_cache=False)
            if team is None:
                print(f"    ! {team_id}: not found in Redis (unexpected -- check config/teams.yaml)")
                continue
            print(f"    {team_id:<14} {raw_key}")
            print(
                f"    {'':<14} rpm_cap={team.rpm_cap}  tpm_cap={team.tpm_cap}  "
                f"budget_cap_usd={team.budget_cap_usd}  priority={team.priority_tier}"
            )

        print()
        print("  Try it:")
        print(f'    curl -s {gateway_url}/v1/chat/completions \\')
        print(f'      -H "X-Gateway-API-Key: {_DEMO_RAW_KEYS["data-science"]}" \\')
        print('      -H "Content-Type: application/json" \\')
        print(
            "      -d '{\"model\": \"tier-1-reasoning\", "
            '"messages": [{"role": "user", "content": "hi"}]}\''
        )
        print("    (data-science is granted the tier-1-reasoning tier directly -- see config/teams.yaml)")

        if settings.gateway_admin_key:
            print()
            print("  Admin API:")
            print(f'    curl -s {gateway_url}/admin/limits/data-science \\')
            print(f'      -H "X-Gateway-Admin-Key: {settings.gateway_admin_key}"')

        print()
        print("  Simulate an outage (chaos control on mock-providers):")
        print('    curl -s -X POST http://localhost:9000/_chaos/config \\')
        print(
            "      -H \"Content-Type: application/json\" "
            '-d \'{"provider": "openai", "error_rate": 1.0}\''
        )
        print(_rule())
    finally:
        await redis.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--redis-url", default=None)
    parser.add_argument(
        "--gateway-url", default=os.environ.get("DEMO_GATEWAY_URL", "http://localhost:8000")
    )
    args = parser.parse_args()
    asyncio.run(main(args.redis_url, args.gateway_url))
