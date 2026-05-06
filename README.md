# Policy RAG Chatbot

A local company-policy RAG chatbot built with FastAPI, React/Vite, Qdrant, and
Docker Model Runner. Policy PDFs are parsed into structured chunks, embedded
with Sentence Transformers, stored in Qdrant, and queried from a web chat UI
with cited source passages.

## What is included

- `backend/` - FastAPI app exposing health, metadata, search, and chat endpoints.
- `frontend/` - React + Vite chat interface.
- `EDA/structural_policy_ingest.py` - PDF ingestion pipeline for policy documents.
- `docker-compose.yml` - Local Qdrant, backend, frontend, and Docker Model Runner model binding.
- `policies/` - Local source PDFs used for ingestion.
- `qdrant_data/` - Local runtime data for Qdrant.

## Prerequisites

- Docker Desktop with Docker Compose and Docker Model Runner.
- Python 3.12 for running the ingestion script from the host.
- Node.js 22 if you want to run the frontend outside Docker.
- Company policy PDFs placed in `policies/`.

The default LLM is `ai/gemma3-qat`, and the default embedding model is
`sentence-transformers/all-MiniLM-L6-v2`.

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

Start Qdrant:

```powershell
docker compose up -d qdrant
```

Install Python dependencies for ingestion:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Ingest the policy PDFs into Qdrant:

```powershell
python -m EDA.structural_policy_ingest --recreate
```

Run the full app:

```powershell
docker compose up --build
```

Open the chat UI at http://localhost:5173. The API is available at
http://localhost:8000, with interactive docs at http://localhost:8000/docs.

## Local Development

Run Qdrant in Docker and use Docker Model Runner for the LLM:

```powershell
docker desktop enable model-runner
docker model pull ai/gemma3-qat
docker compose up -d qdrant
```

Run the backend from the repo root:

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --app-dir backend
```

The host-development defaults use Docker Model Runner at
`http://localhost:12434` with model `ai/gemma3-qat`. If your local `.env`
contains older Ollama values, update or remove those overrides.

Run the frontend locally:

```powershell
cd frontend
npm install
$env:VITE_PROXY_TARGET = "http://localhost:8000"
npm run dev
```

## API Endpoints

- `GET /health` - checks Qdrant, Docker Model Runner, model availability, and collection size.
- `GET /metadata` - returns indexed departments, versions, and policy names.
- `POST /search` - retrieves relevant policy chunks with optional filters.
- `POST /chat` - answers a question using retrieved policy context and citations.

## Configuration

The backend reads environment variables directly or from `.env`.

| Variable | Default |
| --- | --- |
| `API_CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` |
| `QDRANT_URL` | `http://localhost:6333` |
| `QDRANT_COLLECTION` | `company_policies_structural` |
| `EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` |
| `OLLAMA_BASE_URL` | `http://localhost:12434` |
| `OLLAMA_MODEL` | `ai/gemma3-qat` |
| `OLLAMA_TIMEOUT_SECONDS` | `240` |
| `OLLAMA_NUM_CTX` | `4096` |
| `OLLAMA_NUM_PREDICT` | `700` |
| `DEFAULT_TOP_K` | `5` |
| `MAX_TOP_K` | `10` |

For Docker, these values are set in `docker-compose.yml`.

## Re-ingesting Policies

Add or replace PDFs in `policies/`, make sure Qdrant is running, then run:

```powershell
python -m EDA.structural_policy_ingest --recreate
```

The ingestion script extracts metadata from the second page tables, chunks body
content from page 3 onward, creates payload indexes, and upserts vectors into the
configured Qdrant collection.

## Data and Privacy

Local policy PDFs, `.env`, Qdrant storage, virtual environments, frontend
dependencies, and build output are ignored by Git. Treat `policies/` and
`qdrant_data/` as local runtime data unless you intentionally prepare sanitized
samples for sharing. Docker Model Runner stores pulled models in Docker-managed
local storage.

## Troubleshooting

- If `/health` reports `model_missing`, run `docker model pull ai/gemma3-qat`.
- If `/health` reports Docker Model Runner errors, run `docker model status`
  and enable it with `docker desktop enable model-runner`.
- If `/health` reports Qdrant errors, start Qdrant with `docker compose up -d qdrant`.
- If the UI shows `Offline`, confirm the backend is running on port `8000`.
- If answers have no sources, re-run ingestion and check that PDFs have
  extractable text and metadata tables.
