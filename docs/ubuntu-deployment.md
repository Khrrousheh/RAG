# Ubuntu Desktop Deployment Guide

This guide deploys the Policy RAG Chatbot on Ubuntu Desktop 22.04 or newer using Docker Engine, Docker Compose, Docker Model Runner, PostgreSQL, Redis, Qdrant, FastAPI, and a production-built React frontend served by Nginx.

The default deployment is for a single Ubuntu Desktop machine on localhost or a trusted LAN. Public HTTPS is documented as an optional hardening step.

## Table Of Contents

- [Prerequisites](#prerequisites)
- [Project Overview](#project-overview)
- [Deployment Architecture](#deployment-architecture)
- [Required Software And Services](#required-software-and-services)
- [Environment Setup](#environment-setup)
- [Build And Production Configuration](#build-and-production-configuration)
- [Database Setup And Migrations](#database-setup-and-migrations)
- [Policy Ingestion](#policy-ingestion)
- [Reverse Proxy Configuration](#reverse-proxy-configuration)
- [Process Management](#process-management)
- [Optional SSL Setup](#optional-ssl-setup)
- [Verification And Testing](#verification-and-testing)
- [Maintenance And Updates](#maintenance-and-updates)
- [Troubleshooting](#troubleshooting)
- [Security Recommendations](#security-recommendations)

## Prerequisites

Use Ubuntu Desktop 22.04+ on a 64-bit machine. For realistic local LLM latency, prefer a GPU machine supported by Docker Model Runner. CPU-only can run functional tests but responses may be slow.

Minimum practical machine:

| Resource | Recommendation |
| --- | --- |
| CPU | 8 vCPU or better |
| RAM | 32 GiB |
| Disk | 200 GiB free for images, model cache, Qdrant, Postgres, and policy data |
| Network | Outbound access to Docker, PyPI, Hugging Face/Sentence Transformers, and Docker model registries |

Install the repository on the Ubuntu machine:

```bash
sudo apt-get update
sudo apt-get install -y git
git clone <REPOSITORY_URL> RAG
cd RAG
```

Run the automated prerequisite installer:

```bash
bash scripts/install.sh
```

The installer:

- installs Docker Engine from Docker's Ubuntu apt repository;
- installs Docker Compose, Buildx, and `docker-model-plugin`;
- creates `.env` with URI-safe strong secrets if it does not already exist;
- creates `policies/` and `qdrant_data/`;
- verifies Docker Compose `2.38.0+`, which is required for Compose model bindings.

If the installer adds your user to the `docker` group, log out and back in, then rerun:

```bash
bash scripts/install.sh
```

Optional boot service:

```bash
bash scripts/install.sh --systemd
```

Do not install Python dependencies from the root `requirements.txt` on Ubuntu. That file is broad local tooling output and includes Windows-only packages such as `pywin32`. Ingestion is containerized by `scripts/Dockerfile.ingest`.

## Project Overview

The application is a local company-policy RAG chatbot.

- `frontend/`: React 19 and Vite chat UI with authentication, sessions, streaming answers, and citations.
- `backend/`: FastAPI API, authentication, chat orchestration, retrieval, LLM calls, Postgres models, Redis memory, and Alembic migrations.
- `EDA/structural_policy_ingest.py`: supported policy PDF ingestion pipeline.
- `docker-compose.yml`: base development stack for Postgres, Redis, Qdrant, backend, worker, frontend, and Docker Model Runner binding.
- `scripts/docker-compose.ubuntu.yml`: Ubuntu production overlay that narrows host exposure and serves the built frontend through Nginx.

## Deployment Architecture

```text
Browser on Ubuntu host or trusted LAN
  |
  | http://<host-ip>:8080
  v
Frontend Nginx container
  | serves React build
  | proxies /api/* without buffering
  v
FastAPI backend container :8000
  | auth, sessions, chat history
  v
PostgreSQL container

FastAPI backend
  | short-term memory and background jobs
  v
Redis container

FastAPI backend
  | policy vectors and user memory vectors
  v
Qdrant container

FastAPI backend and memory worker
  | Ollama-compatible /api/chat
  v
Docker Model Runner
```

Host exposure in the Ubuntu overlay:

| Service | Host bind | Port |
| --- | --- | ---: |
| Frontend Nginx | `0.0.0.0` | `8080` |
| Backend FastAPI | `127.0.0.1` | `8000` |
| PostgreSQL | `127.0.0.1` | `5432` |
| Redis | `127.0.0.1` | `6379` |
| Qdrant | `127.0.0.1` | `6334` |

## Required Software And Services

The bootstrap scripts install or use:

- Docker Engine
- Docker Compose plugin `2.38.0+`
- Docker Buildx
- Docker Model Runner plugin
- Git, curl, jq, OpenSSL
- Containers for Postgres 16, Redis 7, Qdrant 1.17.1, backend, worker, frontend, and ingestion

Docker Model Runner is used because the Compose file declares:

```yaml
models:
  llm:
    model: ai/gemma3-qat
    context_size: 4096
```

The backend receives the model endpoint and model name through Compose model binding.

## Environment Setup

Create `.env`:

```bash
bash scripts/bootstrap-env.sh
```

This command is idempotent. If `.env` already exists, it leaves it unchanged. If you already have an older local `.env`, make sure it includes strong `POSTGRES_PASSWORD`, `JWT_ACCESS_SECRET`, and `JWT_REFRESH_SECRET` values before running `scripts/deploy.sh`. `POSTGRES_PASSWORD` must be URI-safe because it is interpolated into `DATABASE_URL`; the bootstrap script generates a safe hex value.

Important generated defaults:

```env
COMPOSE_PROJECT_NAME=policy-rag

FRONTEND_HOST_BIND=0.0.0.0
FRONTEND_HOST_PORT=8080
BACKEND_HOST_BIND=127.0.0.1
BACKEND_HOST_PORT=8000
POSTGRES_HOST_BIND=127.0.0.1
REDIS_HOST_BIND=127.0.0.1
QDRANT_HOST_BIND=127.0.0.1
QDRANT_HOST_PORT=6334

QDRANT_COLLECTION=company_policies_structural
QDRANT_MEMORY_COLLECTION=user_memories
OLLAMA_MODEL=ai/gemma3-qat

SEED_DEFAULT_USER=false
REFRESH_COOKIE_SECURE=false
```

For a trusted LAN deployment, users should register through the UI. To seed a default account, set `SEED_DEFAULT_USER=true` and replace `DEFAULT_USER_LOGIN` and `DEFAULT_USER_PASSWORD` with a strong password before deployment.

## Build And Production Configuration

Deploy:

```bash
bash scripts/deploy.sh
```

The deployment script:

1. creates `.env` if missing;
2. starts or installs Docker Model Runner;
3. pulls `ai/gemma3-qat`;
4. builds backend, worker, frontend, and ingestion-ready images as needed;
5. starts Postgres, Redis, Qdrant, migrations, backend, memory worker, and frontend.

Use the Compose wrapper for all manual operations:

```bash
./scripts/compose.sh ps
./scripts/compose.sh config
./scripts/compose.sh restart backend frontend
./scripts/compose.sh down
```

The production frontend image uses `frontend/Dockerfile.prod`, builds the Vite app with `VITE_API_URL=/api`, and serves static files through `frontend/nginx.conf`.

## Database Setup And Migrations

PostgreSQL stores:

- users;
- refresh tokens;
- chat sessions;
- conversation turns;
- long-term memory metadata;
- memory job status.

Alembic migrations are run by the existing `migrate` Compose service:

```bash
./scripts/compose.sh up migrate
```

The main deployment flow also starts `migrate` before the backend and memory worker. The current migration creates the memory/auth/chat schema in Postgres.

## Policy Ingestion

Copy approved policy PDFs into:

```text
policies/
```

Run ingestion:

```bash
bash scripts/ingest.sh --recreate
```

Use `--recreate` for the first ingestion or for a full replacement. For additive safe upsert without deleting the collection:

```bash
bash scripts/ingest.sh
```

The ingestion container:

- reads PDFs from `policies/`;
- extracts page 2 change-history metadata;
- extracts body text from page 3 onward;
- chunks text;
- embeds with `sentence-transformers/all-MiniLM-L6-v2`;
- writes vectors and payload indexes into Qdrant.

After ingestion, the script restarts the backend and memory worker so in-process metadata caches refresh.

## Reverse Proxy Configuration

The default reverse proxy is the frontend Nginx container. It:

- serves the built React app on port `8080`;
- proxies `/api/*` to `backend:8000`;
- disables proxy buffering for streaming chat;
- keeps backend, Postgres, Redis, and Qdrant bound to localhost on the host.

Default app URL:

```text
http://localhost:8080
http://<ubuntu-host-ip>:8080
```

Do not expose ports `8000`, `5432`, `6379`, `6334`, or Docker Model Runner to untrusted networks.

## Process Management

Docker handles individual service restarts through `restart: unless-stopped`.

For reboot recovery, install the systemd unit:

```bash
bash scripts/install-systemd-service.sh
sudo systemctl start policy-rag
sudo systemctl status policy-rag
```

The unit runs:

```bash
./scripts/compose.sh up -d
```

and stops with:

```bash
./scripts/compose.sh down
```

## Optional SSL Setup

For Local/LAN deployment, SSL is not enabled by default.

If exposing the app beyond a trusted LAN, put a host-level Nginx or company reverse proxy in front of `127.0.0.1:8080`, then set:

```env
REFRESH_COOKIE_SECURE=true
REFRESH_COOKIE_SAMESITE=lax
```

Example host Nginx server block:

```nginx
server {
    listen 80;
    server_name rag.example.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
    }
}
```

With a public DNS name, install Certbot and issue a certificate:

```bash
sudo apt-get install -y nginx certbot python3-certbot-nginx
sudo certbot --nginx -d rag.example.com
```

Restart after changing `.env`:

```bash
./scripts/compose.sh up -d
```

## Verification And Testing

Run static checks:

```bash
bash -n scripts/*.sh
./scripts/compose.sh config
```

Run deployment verification:

```bash
bash scripts/verify.sh
```

Before policy ingestion, the policy collection and metadata checks may report warnings. The core service checks should still pass.

Manual checkpoints:

```bash
curl -fsS http://127.0.0.1:8080/healthz
curl -fsS http://127.0.0.1:8080/api/health | jq .
curl -fsS http://127.0.0.1:6334/collections | jq .
docker model status
./scripts/compose.sh ps
```

After ingesting PDFs, verify the policy collection:

```bash
curl -fsS http://127.0.0.1:6334/collections/company_policies_structural | jq .
```

UI validation:

1. Open `http://localhost:8080`.
2. Register a user or sign in with the seeded account if enabled.
3. Ask a policy question.
4. Confirm the answer includes source cards after ingestion.

## Maintenance And Updates

Update the application:

```bash
git pull
bash scripts/deploy.sh
bash scripts/verify.sh
```

Re-ingest policies:

```bash
bash scripts/ingest.sh --recreate
bash scripts/verify.sh
```

View logs:

```bash
bash scripts/logs.sh
bash scripts/logs.sh backend
bash scripts/logs.sh frontend
docker model logs
```

Stop services:

```bash
./scripts/compose.sh down
```

Back up important runtime data:

- Docker volumes: `postgres_data`, `redis_data`, `hf_cache`;
- bind-mounted Qdrant data: `qdrant_data/`;
- policy source PDFs in `policies/`;
- `.env` secrets.

## Troubleshooting

### Docker requires sudo

Log out and back in after `scripts/install.sh` adds your user to the `docker` group, then rerun:

```bash
bash scripts/install.sh
```

### Compose reports unknown `models`

Install or upgrade Docker Compose to `2.38.0+`:

```bash
bash scripts/install.sh
docker compose version
```

### `docker model` is not found

Install the Docker Model Runner plugin:

```bash
sudo apt-get update
sudo apt-get install -y docker-model-plugin
docker model version
```

### Model is missing

Pull it manually:

```bash
docker model pull ai/gemma3-qat
docker model status
```

### Frontend works but chat is slow

Local LLM generation dominates latency. Confirm Model Runner is using the expected hardware:

```bash
docker model status
docker model logs
```

CPU-only machines can be much slower than GPU machines.

### Health reports missing or empty Qdrant collection

Copy PDFs into `policies/`, then run:

```bash
bash scripts/ingest.sh --recreate
```

### Ingestion skips PDFs

The supported ingestion script expects:

- PDFs under `policies/`;
- page 2 tables with change-history metadata;
- extractable body text from page 3 onward.

Check ingestion logs for skipped file reasons.

### Login cookies fail through HTTPS proxy

Set secure cookies when serving over HTTPS:

```env
REFRESH_COOKIE_SECURE=true
```

Then redeploy:

```bash
./scripts/compose.sh up -d
```

## Security Recommendations

- Keep `.env` at mode `600` and never commit it.
- Use strong, unique `JWT_ACCESS_SECRET`, `JWT_REFRESH_SECRET`, and `POSTGRES_PASSWORD`.
- Keep `SEED_DEFAULT_USER=false` unless you have changed the default credentials.
- Restrict LAN access with Ubuntu firewall or network controls.
- Keep Postgres, Redis, Qdrant, backend, and Model Runner off public interfaces.
- Treat `policies/`, `qdrant_data/`, prompts, logs, and benchmark outputs as confidential.
- Apply Ubuntu and Docker security updates regularly.
- Use HTTPS and `REFRESH_COOKIE_SECURE=true` before exposing the app outside a trusted LAN.

## References

- Docker Engine on Ubuntu: https://docs.docker.com/engine/install/ubuntu/
- Docker Model Runner: https://docs.docker.com/ai/model-runner/get-started/
- Compose models: https://docs.docker.com/ai/compose/models-and-compose/
- Compose merge and `!override`: https://docs.docker.com/reference/compose-file/merge/
