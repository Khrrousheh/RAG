#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
MIN_COMPOSE_VERSION="2.38.0"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  echo "==> $*"
}

require_ubuntu() {
  [[ -r /etc/os-release ]] || die "Cannot detect operating system."
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ "${ID:-}" == "ubuntu" ]] || die "This installer supports Ubuntu 22.04+."

  local version_id="${VERSION_ID:-0}"
  local major="${version_id%%.*}"
  if (( major < 22 )); then
    die "Ubuntu ${version_id} is too old. Use Ubuntu 22.04 or newer."
  fi
}

version_ge() {
  local current="$1"
  local required="$2"
  [[ "$(printf '%s\n%s\n' "${required}" "${current}" | sort -V | head -n1)" == "${required}" ]]
}

install_docker_repository() {
  log "Installing Docker apt repository and packages"
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl gnupg lsb-release openssl git jq
  sudo install -m 0755 -d /etc/apt/keyrings

  if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  fi
  sudo chmod a+r /etc/apt/keyrings/docker.asc

  local codename
  codename="$(. /etc/os-release && echo "${VERSION_CODENAME}")"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${codename} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin docker-model-plugin
  sudo systemctl enable --now docker
}

verify_docker() {
  log "Verifying Docker and Compose"
  docker version >/dev/null || die "Docker is not usable by the current user. Log out/in after docker group changes, or run with sudo."

  local compose_version
  compose_version="$(docker compose version --short 2>/dev/null || true)"
  [[ -n "${compose_version}" ]] || die "Docker Compose plugin is missing."

  if ! version_ge "${compose_version}" "${MIN_COMPOSE_VERSION}"; then
    die "Docker Compose ${compose_version} is too old. Need ${MIN_COMPOSE_VERSION}+ for Compose models."
  fi

  docker model version >/dev/null || die "Docker Model Runner CLI plugin is missing."
}

configure_docker_group() {
  if groups "${USER}" | grep -qw docker; then
    return
  fi

  log "Adding ${USER} to docker group"
  sudo usermod -aG docker "${USER}"
  echo "The docker group change takes effect after logging out and back in."
}

main() {
  cd "${REPO_ROOT}"
  if [[ "$(id -u)" -eq 0 ]]; then
    die "Run this script as your normal Ubuntu user. It uses sudo for system changes."
  fi

  require_ubuntu
  install_docker_repository
  configure_docker_group

  if docker version >/dev/null 2>&1; then
    verify_docker
  else
    echo "Docker installed. Log out and back in, then run scripts/install.sh again to verify non-root access."
  fi

  bash "${SCRIPT_DIR}/bootstrap-env.sh"
  mkdir -p "${REPO_ROOT}/policies" "${REPO_ROOT}/qdrant_data"

  if [[ "${1:-}" == "--systemd" ]]; then
    bash "${SCRIPT_DIR}/install-systemd-service.sh"
  fi

  log "Install step complete"
}

main "$@"
