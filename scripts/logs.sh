#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$#" -eq 0 ]]; then
  set -- --tail=200 -f
fi

exec "${SCRIPT_DIR}/compose.sh" logs "$@"

