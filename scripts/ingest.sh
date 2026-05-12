#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
IMAGE_NAME="policy-rag-ingest:ubuntu"
COLLECTION="${QDRANT_COLLECTION:-company_policies_structural}"
PROJECT_NAME="${COMPOSE_PROJECT_NAME:-policy-rag}"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

main() {
  cd "${REPO_ROOT}"

  [[ -d "${REPO_ROOT}/policies" ]] || die "Missing policies/ directory. Create it and copy approved PDF files into it."

  if ! find "${REPO_ROOT}/policies" -maxdepth 1 -type f -iname '*.pdf' | grep -q .; then
    die "No PDF files found in policies/."
  fi

  if [[ -f "${REPO_ROOT}/.env" ]]; then
    # shellcheck disable=SC1091
    set -a
    . "${REPO_ROOT}/.env"
    set +a
    COLLECTION="${QDRANT_COLLECTION:-${COLLECTION}}"
    PROJECT_NAME="${COMPOSE_PROJECT_NAME:-${PROJECT_NAME}}"
  fi

  bash "${SCRIPT_DIR}/compose.sh" up -d --wait qdrant

  echo "Building ingestion image ${IMAGE_NAME}"
  docker build -f scripts/Dockerfile.ingest -t "${IMAGE_NAME}" .

  echo "Running ingestion into ${COLLECTION}"
  docker run --rm \
    --network "${PROJECT_NAME}_default" \
    -v "${REPO_ROOT}/policies:/app/policies:ro" \
    -v "policy-rag-ingest-cache:/root/.cache/huggingface" \
    "${IMAGE_NAME}" \
    --pdf-dir /app/policies \
    --qdrant-url http://qdrant:6333 \
    --collection "${COLLECTION}" \
    "$@"

  mapfile -t running_services < <(bash "${SCRIPT_DIR}/compose.sh" ps --services --status running | grep -E '^(backend|memory-worker)$' || true)
  if (( ${#running_services[@]} > 0 )); then
    echo "Restarting running app services so metadata caches refresh"
    bash "${SCRIPT_DIR}/compose.sh" restart "${running_services[@]}"
  fi
}

main "$@"
