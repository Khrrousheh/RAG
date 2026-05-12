#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -f "${REPO_ROOT}/.env" ]]; then
  # shellcheck disable=SC1091
  set -a
  . "${REPO_ROOT}/.env"
  set +a
fi

FRONTEND_URL="http://127.0.0.1:${FRONTEND_HOST_PORT:-8080}"
BACKEND_URL="http://127.0.0.1:${BACKEND_HOST_PORT:-8000}"
QDRANT_URL="http://127.0.0.1:${QDRANT_HOST_PORT:-6334}"
COLLECTION="${QDRANT_COLLECTION:-company_policies_structural}"

failures=0
warnings=0

check() {
  local name="$1"
  shift
  printf '%-36s' "${name}"
  if "$@" >/tmp/policy-rag-verify.out 2>/tmp/policy-rag-verify.err; then
    echo "OK"
  else
    echo "FAIL"
    sed 's/^/  /' /tmp/policy-rag-verify.err >&2 || true
    failures=$((failures + 1))
  fi
}

warn_check() {
  local name="$1"
  shift
  printf '%-36s' "${name}"
  if "$@" >/tmp/policy-rag-verify.out 2>/tmp/policy-rag-verify.err; then
    echo "OK"
  else
    echo "WARN"
    warnings=$((warnings + 1))
  fi
}

check "Docker daemon" docker version
check "Docker Compose config" bash "${SCRIPT_DIR}/compose.sh" config
check "Docker Model Runner status" docker model status
check "Compose services" bash "${SCRIPT_DIR}/compose.sh" ps
check "Frontend health" curl -fsS "${FRONTEND_URL}/healthz"
check "Backend health" curl -fsS "${BACKEND_URL}/health"
check "Frontend API proxy" curl -fsS "${FRONTEND_URL}/api/health"
check "Qdrant collections" curl -fsS "${QDRANT_URL}/collections"
warn_check "Qdrant policy collection" curl -fsS "${QDRANT_URL}/collections/${COLLECTION}"
warn_check "Backend metadata" curl -fsS "${BACKEND_URL}/metadata"

if (( failures > 0 )); then
  cat <<EOF >&2

Verification failed.

Useful next commands:
  bash scripts/logs.sh
  bash scripts/compose.sh ps
  docker model logs
  curl -fsS ${BACKEND_URL}/health | jq .

Common fixes:
  - Run bash scripts/deploy.sh if services are not started.
  - Run docker model pull ${OLLAMA_MODEL:-ai/gemma3-qat} if the model is missing.
  - Copy PDFs into policies/ and run bash scripts/ingest.sh --recreate if the Qdrant collection is missing or empty.
EOF
  exit 1
fi

echo
if (( warnings > 0 )); then
  echo "Core deployment checks passed with ${warnings} warning(s)."
  echo "If policies are not ingested yet, copy PDFs into policies/ and run: bash scripts/ingest.sh --recreate"
else
  echo "All deployment checks passed."
fi
echo "App: ${FRONTEND_URL}"
