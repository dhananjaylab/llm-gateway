#!/usr/bin/env python3
"""
Hash a raw team API key for storage in config/teams.yaml.

Usage:
    python scripts/hash_api_key.py "sk-gw-my-raw-key"

Per Document 05's SECURITY NOTE: teams.yaml stores only the hash. Show the
raw key to the team once, at provisioning time, and never persist it
anywhere else. In Phase 2 this same hashing logic runs inside the Admin
API's team-provisioning endpoint instead of being run by hand.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.auth import hash_api_key


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python scripts/hash_api_key.py <raw-api-key>", file=sys.stderr)
        raise SystemExit(1)
    print(hash_api_key(sys.argv[1]))


if __name__ == "__main__":
    main()
