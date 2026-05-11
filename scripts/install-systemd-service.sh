#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SERVICE_FILE="/etc/systemd/system/policy-rag.service"

if [[ "$(id -u)" -eq 0 ]]; then
  SUDO=""
else
  SUDO="sudo"
fi

cat <<EOF | ${SUDO} tee "${SERVICE_FILE}" >/dev/null
[Unit]
Description=Policy RAG Chatbot Docker Compose stack
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=${REPO_ROOT}
RemainAfterExit=yes
ExecStartPre=-/usr/bin/docker model start-runner
ExecStart=${SCRIPT_DIR}/compose.sh up -d
ExecStop=${SCRIPT_DIR}/compose.sh down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF

${SUDO} systemctl daemon-reload
${SUDO} systemctl enable policy-rag.service

echo "Installed ${SERVICE_FILE}"
echo "Start now with: sudo systemctl start policy-rag"
