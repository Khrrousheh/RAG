# Policy RAG Chatbot

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-19-61DAFB.svg)](https://react.dev/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED.svg)](https://docs.docker.com/compose/)
[![Qdrant](https://img.shields.io/badge/vector%20store-Qdrant-DC244C.svg)](https://qdrant.tech/)
[![License](https://img.shields.io/badge/license-MIT%20with%20retention-green.svg)](LICENSE)
[![Code of Conduct](https://img.shields.io/badge/code%20of%20conduct-active-4B5563.svg)](CODE_OF_CONDUCT.MD)

A local company-policy RAG chatbot built with FastAPI, React/Vite, Qdrant,
Sentence Transformers, and Docker Model Runner. Policy PDFs are parsed into
structured chunks, embedded, stored in Qdrant, and queried from a streaming web
chat UI with cited source passages.

Reference docs:
[Architecture](docs/ARCHITECTURE.md) |
[AI Contract](docs/AI_CONTRACT.md) |
[License](LICENSE) |
[Code of Conduct](CODE_OF_CONDUCT.MD)

## Current Capabilities

- Structured PDF ingestion from `policies/` into Qdrant.
- Metadata extraction from policy change-history tables.
- Semantic search with optional policy, department, version, and effective-date filters.
- Policy-name alias matching, including file names, titles, and acronyms.
- Streaming chat over newline-delimited JSON from `/chat/stream`.
- Non-streaming `/chat` fallback for clients that do not consume streams.
- In-process metadata cache, policy-alias cache, and query-embedding LRU cache.
- Prompt budgeting to cap context size and reduce avoidable model latency.
- JWT authentication with refresh-token rotation.
- Persistent multi-session chat history in PostgreSQL.
- Redis-backed short-term memory and background summarization queue.
- Long-term semantic user memory in a separate Qdrant `user_memories` collection.
- Local latency benchmarking and generated reports in `docs/reports/`.

## Repository Guide

| Path | Purpose |
| --- | --- |
| `backend/` | FastAPI API, RAG orchestration, Qdrant retrieval, LLM calls, caches, and streaming. |
| `frontend/` | React + Vite authenticated chat UI with sessions, streaming responses, and citations. |
| `EDA/structural_policy_ingest.py` | Main PDF ingestion pipeline for policy documents. |
| `benchmarks/p0_latency_benchmark.py` | P0/P1 latency benchmark for search, cached search, metadata, streaming, and direct LLM timing. |
| `docs/reports/` | Latency and bottleneck reports generated from local benchmark runs. |
| `docker-compose.yml` | Local Qdrant, backend, frontend, and Docker Model Runner binding. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | System architecture, runtime flow, data flow, and operational notes. |
| [`docs/AI_CONTRACT.md`](docs/AI_CONTRACT.md) | Behavioral contract for the policy assistant, streaming schema, citations, and safety rules. |
| [`LICENSE`](LICENSE) | MIT-style license with copyright retention and limited liability terms. |
| [`CODE_OF_CONDUCT.MD`](CODE_OF_CONDUCT.MD) | Code of conduct and fork usage policy. |

Local runtime data such as `policies/`, `qdrant_data/`, `.env`, virtual
environments, and frontend dependencies are ignored by Git.

## Prerequisites

- Docker Desktop with Docker Compose and Docker Model Runner.
- Python 3.12 for ingestion and local backend development.
- Node.js 22 for local frontend development outside Docker.
- Company policy PDFs placed in `policies/`.

Defaults:

- LLM: `ai/gemma3-qat`
- Embedding model: `sentence-transformers/all-MiniLM-L6-v2`
- Qdrant collection: `company_policies_structural`

## Quick Start

Enable Docker Model Runner:

```powershell
docker desktop enable model-runner
docker model status
```

Optionally pre-pull the default model:

```powershell
docker model pull ai/gemma3-qat
```

Start the data services:

```powershell
docker compose up -d postgres redis qdrant
```

By default, Compose maps Qdrant to host port `6334` to avoid collisions with
other local Qdrant stacks. The backend container still reaches Qdrant internally
at `http://qdrant:6333`.

Install Python dependencies for ingestion:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Ingest the policy PDFs into the Compose Qdrant instance:

```powershell
python -m EDA.structural_policy_ingest --qdrant-url http://localhost:6334 --recreate
```

Run the full app, migrations, and memory worker:

```powershell
docker compose up --build
```

Open the chat UI at http://localhost:5173. The API is available at
http://localhost:8000, with interactive docs at http://localhost:8000/docs.

For local Docker Compose development, the backend seeds a default login:
`mahdi` / `123456`. Override or disable this with `DEFAULT_USER_LOGIN`,
`DEFAULT_USER_PASSWORD`, and `SEED_DEFAULT_USER=false` before using a shared
environment.

## Local Development

Run Qdrant and Docker Model Runner, then run the backend from the repo root:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
$env:QDRANT_URL = "http://localhost:6334"
$env:OLLAMA_BASE_URL = "http://localhost:12434"
uvicorn app.main:app --reload --app-dir backend
```

Run the frontend locally:

```powershell
cd frontend
npm install
$env:VITE_PROXY_TARGET = "http://localhost:8000"
npm run dev
```

Build the frontend:

```powershell
cd frontend
npm run build
```

## API Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Checks Qdrant, Postgres, Redis, Docker Model Runner, model availability, and collection size. |
| `GET /metadata` | Returns cached departments, versions, and policy names. |
| `POST /search` | Retrieves relevant policy chunks with optional filters. |
| `POST /auth/register` | Creates a user and sets a refresh-token cookie. |
| `POST /auth/login` | Authenticates a user and sets a refresh-token cookie. |
| `POST /auth/refresh` | Rotates the refresh token and returns a new access token. |
| `POST /auth/logout` | Revokes the current refresh token. |
| `GET /auth/me` | Returns the authenticated user. |
| `GET /chat/sessions` | Lists the authenticated user's chat sessions. |
| `POST /chat/session` | Creates a chat session. |
| `GET /chat/session/{id}/messages` | Returns persisted conversation turns. |
| `POST /chat` | Protected compatibility endpoint returning one complete JSON answer. |
| `POST /chat/message` | Protected canonical non-streaming chat endpoint. |
| `POST /chat/stream` | Protected NDJSON stream: `session`, `sources`, `token`, `warning`, `metrics`, `done`, or `error`. |

Example streaming request:

```powershell
$body = @{
  message = "Can I share progress about this project on LinkedIn?"
  top_k = 6
} | ConvertTo-Json

Invoke-WebRequest `
  -Uri http://localhost:8000/chat/stream `
  -Method POST `
  -Headers @{ Authorization = "Bearer <access-token>" } `
  -ContentType "application/json" `
  -Body $body
```

## Configuration

The backend reads environment variables directly or from `.env`. Docker Compose
sets container-specific values in `docker-compose.yml`.

| Variable | Code default | Compose value | Notes |
| --- | --- | --- | --- |
| `API_CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | same | Comma-separated allowed browser origins. |
| `QDRANT_URL` | `http://localhost:6333` | `http://qdrant:6333` | Use `http://localhost:6334` for host scripts against Compose Qdrant. |
| `QDRANT_HOST_PORT` | Compose-only `6334` | optional | Host port mapped to container Qdrant `6333`. |
| `QDRANT_COLLECTION` | `company_policies_structural` | same | Vector collection name. |
| `QDRANT_MEMORY_COLLECTION` | `user_memories` | same | User long-term memory vector collection. |
| `DATABASE_URL` | local Postgres URL | `postgres` service URL | Async SQLAlchemy connection string. |
| `REDIS_URL` | local Redis URL | `redis` service URL | Short-term memory and worker queue. |
| `JWT_ACCESS_SECRET` | dev placeholder | env/default | Use a strong secret in any shared environment. |
| `JWT_REFRESH_SECRET` | dev placeholder | env/default | Use a separate strong secret in any shared environment. |
| `EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | same | Sentence Transformers model. |
| `OLLAMA_BASE_URL` | `http://localhost:12434` | set by Compose model binding | Docker Model Runner/Ollama-compatible API base. |
| `OLLAMA_MODEL` | `ai/gemma3-qat` | set by Compose model binding | LLM model name. |
| `OLLAMA_TIMEOUT_SECONDS` | `240` | default | LLM request timeout. |
| `OLLAMA_NUM_CTX` | `4096` | default | Model context window. |
| `OLLAMA_NUM_PREDICT` | `384` | `384` | Output-token budget. |
| `OLLAMA_KEEP_ALIVE` | `30m` | `30m` | Keeps model loaded between requests when supported. |
| `DEFAULT_TOP_K` | `5` | default | Backend default retrieval count. |
| `MAX_TOP_K` | `10` | default | Hard cap for `top_k`. |
| `WARM_EMBEDDINGS_ON_STARTUP` | `true` | `false` | Pre-loads embedding model. Disabled in Compose for faster container startup. |
| `WARM_LLM_ON_STARTUP` | `true` | `false` | Sends a tiny LLM warmup request. Disabled in Compose for faster startup. |
| `WARM_METADATA_ON_STARTUP` | `true` | `true` | Preloads metadata and policy aliases. |
| `EMBEDDING_CACHE_SIZE` | `256` | `256` | In-process query embedding LRU cache size. |
| `PROMPT_CONTEXT_MAX_CHARS` | `3600` | `3600` | Max policy context chars included in prompt. |
| `PROMPT_MIN_SOURCES` | `3` | `3` | Minimum prompt source count when available. |
| `PROMPT_MAX_SOURCES` | `5` | `5` | Maximum prompt source count. |
| `HTTP_MAX_CONNECTIONS` | `20` | `20` | Async HTTP client pool limit. |
| `HTTP_MAX_KEEPALIVE_CONNECTIONS` | `10` | `10` | Async keep-alive pool limit. |
| `VITE_PROXY_TARGET` | `http://backend:8000` | same | Vite `/api` proxy target. |
| `VITE_API_URL` | `/api` | optional | Browser API base override. |

## Re-Ingesting Policies

Add or replace PDFs in `policies/`, make sure Qdrant is running, then run:

```powershell
python -m EDA.structural_policy_ingest --qdrant-url http://localhost:6334 --recreate
```

The ingestion script:

- reads PDFs from `policies/`;
- extracts metadata from page 2 tables;
- chunks body text from page 3 onward;
- embeds chunks with Sentence Transformers;
- creates Qdrant payload indexes for common filters;
- upserts vectors and payloads into the configured collection.

Restart the backend after re-ingestion so in-process metadata and policy-alias
caches reflect the updated collection.

## Benchmarking

Run the latency benchmark against a running API:

```powershell
.\.venv\Scripts\Activate.ps1
python benchmarks\p0_latency_benchmark.py `
  --api-base http://localhost:8000 `
  --llm-base http://localhost:12434 `
  --model ai/gemma3-qat `
  --samples 2 `
  --timeout 240
```

The benchmark measures metadata, search, cached search, chat without LLM,
streamed chat, and direct LLM streaming. Recent reports are stored in
`docs/reports/`.

## Governance

This repository includes:

- [`LICENSE`](LICENSE) - MIT-style license with copyright retention and limited
  liability terms.
- [`CODE_OF_CONDUCT.MD`](CODE_OF_CONDUCT.MD) - contributor expectations, fork
  usage rules, and reporting contact.
- [`docs/AI_CONTRACT.md`](docs/AI_CONTRACT.md) - the assistant behavior,
  grounding, citation, fallback, and streaming contract.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) - the system design and data
  flow reference.

## Data and Privacy

Treat policy PDFs and Qdrant data as private local runtime data. They are ignored
by Git by default. Do not commit real policies, Qdrant storage, `.env`, model
files, generated caches, or benchmark outputs that reveal sensitive policy text
unless they have been sanitized for sharing.

## Troubleshooting

- If `/health` reports `model_missing`, run `docker model pull ai/gemma3-qat`.
- If `/health` reports Docker Model Runner errors, run `docker model status`
  and enable it with `docker desktop enable model-runner`.
- If the host ingestion script cannot reach Qdrant, confirm the host port with
  `docker compose ps`; the default is `http://localhost:6334`.
- If the backend cannot reach Qdrant in Docker, confirm the `qdrant` service is
  healthy and `QDRANT_URL` is `http://qdrant:6333`.
- If the UI shows `Offline`, confirm the backend is running on port `8000`.
- If answers have no sources, re-run ingestion and check that PDFs have
  extractable body text and page 2 metadata tables.
- If streaming appears delayed, compare `/chat/stream` locally and through
  Docker. The P1 report notes a possible buffering issue in the Dockerized path.
