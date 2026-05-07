# Architecture

This document describes the current local RAG application architecture. It is
intended for developers changing ingestion, retrieval, chat behavior, streaming,
or operational configuration.

## System Overview

The application is a local policy assistant with production memory services and
one host-side ingestion pipeline.

```text
Browser
  |
  | HTTP /api proxy
  v
React + Vite frontend :5173
  |
  | /auth/*, /chat/*, /health, /metadata, /search
  v
FastAPI backend :8000
  |
  | structured user/session/turn metadata
  v
PostgreSQL

FastAPI backend
  |
  | short-term memory and summary jobs
  v
Redis

FastAPI backend
  |
  | policy vector search plus user memory search
  v
Qdrant :6333 in container, :6334 on host by default

FastAPI backend
  |
  | Ollama-compatible /api/tags and /api/chat
  v
Docker Model Runner model: ai/gemma3-qat
```

Host ingestion writes into the same Qdrant storage:

```text
policies/*.pdf
  -> pdfplumber metadata and text extraction
  -> RecursiveCharacterTextSplitter chunks
  -> Sentence Transformers embeddings
  -> Qdrant collection company_policies_structural
```

## Runtime Components

### Frontend

Location: `frontend/`

The frontend is a React 19 + Vite app. It uses Vite's dev-server proxy to send
browser calls from `/api/*` to the backend. The chat UI supports login/register,
refresh-token restore, session switching, persisted message history, and
streamed assistant responses. If streaming fails before any token is received,
it falls back to non-streaming `POST /chat/message`.

Main files:

- `frontend/src/App.tsx` - auth state, sessions, chat state, stream parser, fallback request, sources display.
- `frontend/src/styles.css` - layout and chat styling.
- `frontend/vite.config.ts` - `/api` proxy to `VITE_PROXY_TARGET`.

### Backend

Location: `backend/app/`

The backend is an async FastAPI app. Startup creates a shared `RagService`,
initializes the async HTTP client, and optionally warms embeddings, metadata,
policy aliases, and the LLM.

Main files:

- `main.py` - FastAPI app, lifecycle, CORS, public API routes.
- `config.py` - environment-driven settings.
- `schemas.py` - request and response models.
- `rag.py` - retrieval, caching, prompt building, fallback answers, model calls, streaming.
- `api_auth.py` - register, login, refresh, logout, and current-user routes.
- `api_chat.py` - protected session and memory-aware chat routes.
- `chat_service.py` - authenticated chat orchestration around the existing RAG service.
- `memory_service.py` - Redis short-term memory, Qdrant long-term memory, and job enqueueing.
- `worker.py` - Redis-backed background memory summarization worker.

The backend exposes:

- `GET /health`
- `GET /metadata`
- `POST /search`
- `POST /auth/register`
- `POST /auth/login`
- `POST /auth/refresh`
- `POST /auth/logout`
- `GET /auth/me`
- `GET /chat/sessions`
- `POST /chat/session`
- `GET /chat/session/{id}/messages`
- protected `POST /chat`
- protected `POST /chat/message`
- protected `POST /chat/stream`

### Qdrant

Qdrant stores vectors and policy payloads in `qdrant_data/`. Docker Compose maps
container port `6333` to host port `6334` by default:

- backend container URL: `http://qdrant:6333`
- host ingestion URL: `http://localhost:6334`

The policy document collection name is `company_policies_structural`.

Long-term user memory uses a separate `user_memories` collection with payload
indexes for `user_id`, future `tenant_id`, `session_id`, `kind`, `topics`, and
`created_at`. Memory search always filters by `user_id` before ranking.

### PostgreSQL And Redis

PostgreSQL stores users, refresh tokens, chat sessions, conversation turns,
long-term memory metadata, and memory job state. Alembic migrations run through
the Compose `migrate` service before the API and worker start.

Redis stores short-term memory in `stm:{user_id}:{session_id}:turns` and the
`memory_jobs` queue consumed by the `memory-worker` service.

### Docker Model Runner

The backend talks to Docker Model Runner through an Ollama-compatible API. The
configured model is `ai/gemma3-qat`. Compose uses the `models` section to inject
the model endpoint and model name into backend environment variables.

## Ingestion Flow

`EDA/structural_policy_ingest.py` is the supported ingestion path.

1. Discover `*.pdf` files under `policies/`.
2. Open each PDF with `pdfplumber`.
3. Read page 2 tables and extract the latest approved change-history metadata.
4. Extract body text from page 3 onward.
5. Split body pages into chunks with `chunk_size=1000`, `chunk_overlap=200`, and start indexes.
6. Add inherited policy metadata to every chunk.
7. Embed chunks with `sentence-transformers/all-MiniLM-L6-v2`.
8. Create or reuse the Qdrant collection.
9. Create payload indexes for `Department`, `Version`, `effective_date`, `policy_name`, `source`, and `file_name`.
10. Upsert points with stable UUIDv5 point IDs.

The ingestion script intentionally skips PDFs that do not have the expected page
2 metadata table or extractable body text.

## Retrieval Flow

The backend retrieval path is implemented in `RagService.search_with_metrics`.

1. Cap `top_k` at `MAX_TOP_K`.
2. Resolve named policy aliases from cached Qdrant payloads when possible.
3. Expand public-sharing style questions with related disclosure/security terms.
4. Embed the retrieval query with a process-local LRU cache.
5. Build an optional Qdrant filter for file name, department, version, or date range.
6. Search Qdrant with payloads and no vectors.
7. Convert payloads into `Source` objects.
8. Post-filter named section requests such as scope, responsibility, approval, or disclosure.
9. Return sources plus metrics for embedding, Qdrant, filtering, and policy resolution.

Current retrieval uses `qdrant_client.QdrantClient.search`. The client reports
that this method is deprecated in favor of `query_points`, so migration is a
future compatibility task.

## Chat Flow

### Non-Streaming Chat

`POST /chat` prepares retrieval, builds a prompt, calls Model Runner with
`stream=false`, and returns:

```json
{
  "answer": "...",
  "sources": [],
  "warnings": []
}
```

If `use_llm=false`, or if model generation fails before producing an answer, the
backend returns a deterministic fallback answer based on retrieved sources.

### Streaming Chat

`POST /chat/stream` returns `application/x-ndjson`.

Expected event order:

1. `session` - selected or created persisted session.
2. `sources` - retrieved sources and warnings.
3. zero or more `warning` events.
4. one or more `token` events.
5. `metrics`.
6. `done`.

If preparation or generation fails, an `error` or fallback `warning` event is
emitted. See `AI_CONTRACT.md` for the event schema.

## Prompt Construction

Prompt construction is budgeted to control latency and context size.

- Deduplicate identical or near-duplicate source text.
- Select between `PROMPT_MIN_SOURCES` and `PROMPT_MAX_SOURCES`.
- Cap policy context to `PROMPT_CONTEXT_MAX_CHARS`.
- Include conversation history, selected policy context, and the user question.
- Ask for a concise, practical answer with source IDs.

## Runtime State

The backend keeps the following process-local state:

- HuggingFace embedding model instance.
- Query embedding LRU cache.
- Metadata cache.
- Policy alias cache.
- Shared async HTTP client for Model Runner.

Restart the backend after re-ingestion so metadata and alias caches refresh.

## Ports

| Service | Container port | Host port |
| --- | ---: | ---: |
| Frontend | `5173` | `5173` |
| Backend | `8000` | `8000` |
| Qdrant | `6333` | `6334` by default |
| Docker Model Runner | Docker-managed | usually `12434` on host |

Set `QDRANT_HOST_PORT=6333` before starting Compose if you want the host Qdrant
port to match the ingestion script default.

## Performance Notes

Recent local benchmark reports show:

- Qdrant retrieval is fast at the current corpus size.
- Cached retrieval can complete in tens of milliseconds.
- The dominant latency is local LLM first-token and generation time.
- Streaming improves perceived progress, but Dockerized streaming may buffer
  early events in some runs. See `reports/p1-optimized.md`.

## Known Maintenance Items

- `EDA/pdf_loader_and_parser.py` appears to be an older experimental parser and
  is not the supported ingestion path.
- There are no tracked automated tests at the time of this analysis.
- `qdrant_client.search` should eventually move to `query_points`.
- Root `requirements.txt` is broader than `backend/requirements.txt` and is used
  mainly for host-side ingestion and analysis workflows.
