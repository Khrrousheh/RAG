#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
MODEL_NAME="${OLLAMA_MODEL:-ai/gemma3-qat}"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  echo "==> $*"
}

ensure_env() {
  if [[ ! -f "${REPO_ROOT}/.env" ]]; then
    bash "${SCRIPT_DIR}/bootstrap-env.sh"
  fi
  # shellcheck disable=SC1091
  set -a
  . "${REPO_ROOT}/.env"
  set +a
  MODEL_NAME="${OLLAMA_MODEL:-${MODEL_NAME}}"

  for required_name in POSTGRES_PASSWORD JWT_ACCESS_SECRET JWT_REFRESH_SECRET; do
    if [[ -z "${!required_name:-}" ]]; then
      die "${required_name} is missing from .env. Move the old .env aside and run scripts/bootstrap-env.sh, or add the value manually."
    fi
  done

  case "${POSTGRES_PASSWORD}" in
    rag_password|change-me-*|replace-with-*)
      die "POSTGRES_PASSWORD still uses an example value. Replace it with a strong secret."
      ;;
  esac
  if [[ ! "${POSTGRES_PASSWORD}" =~ ^[A-Za-z0-9._~-]+$ ]]; then
    die "POSTGRES_PASSWORD must be URI-safe because it is interpolated into DATABASE_URL. Use letters, numbers, '.', '_', '~', or '-'."
  fi

  case "${JWT_ACCESS_SECRET}" in
    dev-change-me-*|change-me-*|replace-with-*)
      die "JWT_ACCESS_SECRET still uses an example value. Replace it with a strong secret."
      ;;
  esac

  case "${JWT_REFRESH_SECRET}" in
    dev-change-me-*|change-me-*|replace-with-*)
      die "JWT_REFRESH_SECRET still uses an example value. Replace it with a strong secret."
      ;;
  esac
}

ensure_model_runner() {
  command -v docker >/dev/null 2>&1 || die "Docker is not installed."
  docker model version >/dev/null 2>&1 || die "Docker Model Runner is not installed. Run scripts/install.sh first."

  if ! docker model status >/dev/null 2>&1; then
    log "Installing Docker Model Runner runtime"
    docker model install-runner
  fi

  log "Starting Docker Model Runner"
  docker model start-runner >/dev/null 2>&1 || true

  log "Pulling model ${MODEL_NAME}"
  docker model pull "${MODEL_NAME}"
}

main() {
  cd "${REPO_ROOT}"
  ensure_env
  ensure_model_runner

  log "Building application images"
  bash "${SCRIPT_DIR}/compose.sh" build

  log "Starting services"
  bash "${SCRIPT_DIR}/compose.sh" up -d --wait postgres redis qdrant
  bash "${SCRIPT_DIR}/compose.sh" up migrate
  bash "${SCRIPT_DIR}/compose.sh" up -d --wait backend memory-worker frontend

  log "Waiting for containers to report healthy"
  bash "${SCRIPT_DIR}/compose.sh" ps

  local host_ip
  host_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  host_ip="${host_ip:-127.0.0.1}"

  cat <<EOF

Deployment started.

Open the app:
  http://localhost:${FRONTEND_HOST_PORT:-8080}
  http://${host_ip}:${FRONTEND_HOST_PORT:-8080}

Verify:
  bash scripts/verify.sh

Ingest policies after copying PDFs into policies/:
  bash scripts/ingest.sh --recreate
EOF
}

main "$@"
