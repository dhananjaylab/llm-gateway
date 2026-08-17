#!/usr/bin/env bash
# scripts/verify_stack_healthy.sh
#
# Document 06 Phase 5 test plan: "docker-compose up cold-start check --
# A machine with none of the images cached reaches a fully healthy stack
# (all healthchecks green) within a documented time budget, with zero
# manual steps beyond docker-compose up."
#
# Polls each service's own health endpoint (not `docker compose ps`'s
# healthcheck status directly, so this script works identically whether
# it's run from inside CI, a developer's shell, or anywhere else that has
# network access to the published ports but not necessarily the Docker
# socket).
#
# Usage:
#     docker compose up -d
#     ./scripts/verify_stack_healthy.sh
#
# Exit code 0 once every service answers healthy; non-zero (with the
# name of whichever service never came up) if TIMEOUT_SECONDS elapses
# first.

set -uo pipefail

TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-120}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-3}"

declare -A ENDPOINTS=(
  [redis]="tcp://localhost:6379"
  [mock-providers]="http://localhost:9000/healthz"
  [gateway]="http://localhost:8000/readyz"
  [prometheus]="http://localhost:9090/-/healthy"
  [alertmanager]="http://localhost:9093/-/healthy"
  [jaeger]="http://localhost:16686"
  [grafana]="http://localhost:3000/api/health"
)

_check_one() {
  local url="$1"
  if [[ "$url" == tcp://* ]]; then
    local hostport="${url#tcp://}"
    (exec 3<>"/dev/tcp/${hostport/:/\/}") 2>/dev/null && exec 3>&- 3<&-
    return $?
  fi
  curl -fsS -o /dev/null -m 3 "$url" 2>/dev/null
}

echo "Waiting up to ${TIMEOUT_SECONDS}s for ${#ENDPOINTS[@]} services to become healthy..."
start_ts=$(date +%s)
pending=("${!ENDPOINTS[@]}")

while [ "${#pending[@]}" -gt 0 ]; do
  now=$(date +%s)
  elapsed=$((now - start_ts))
  if [ "$elapsed" -ge "$TIMEOUT_SECONDS" ]; then
    echo "TIMEOUT after ${elapsed}s -- still waiting on: ${pending[*]}"
    exit 1
  fi

  still_pending=()
  for name in "${pending[@]}"; do
    if _check_one "${ENDPOINTS[$name]}"; then
      echo "  [ok]  ${name} (${elapsed}s)"
    else
      still_pending+=("$name")
    fi
  done
  pending=("${still_pending[@]}")

  if [ "${#pending[@]}" -gt 0 ]; then
    sleep "$POLL_INTERVAL_SECONDS"
  fi
done

total=$(( $(date +%s) - start_ts ))
echo "All services healthy after ${total}s."
exit 0
