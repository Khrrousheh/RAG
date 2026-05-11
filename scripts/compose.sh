#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"

export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-policy-rag}"

exec docker compose \
  -f docker-compose.yml \
  -f scripts/docker-compose.ubuntu.yml \
  "$@"
