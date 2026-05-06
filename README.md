# Policy RAG Chatbot

A local company-policy RAG chatbot built with FastAPI, React/Vite, Qdrant, and
Ollama. Policy PDFs are parsed into structured chunks, embedded with Sentence
Transformers, stored in Qdrant, and queried from a web chat UI with cited source
passages.

## What is included

- `backend/` - FastAPI app exposing health, metadata, search, and chat endpoints.
- `frontend/` - React + Vite chat interface.
- `EDA/structural_policy_ingest.py` - PDF ingestion pipeline for policy documents.
- `docker-compose.yml` - Local Ollama, Qdrant, backend, and frontend services.
- `policies/` - Local source PDFs used for ingestion.
- `qdrant_data/` and `ollama/` - Local runtime data for Qdrant and Ollama.

## Prerequisites

- Docker Desktop with Docker Compose.
- Python 3.12 for running the ingestion script from the host.
- Node.js 22 if you want to run the frontend outside Docker.
- Company policy PDFs placed in `policies/`.

The default LLM is `llama3.2:3b`, and the default embedding model is
`sentence-transformers/all-MiniLM-L6-v2`.

## Quick Start

Start Qdrant and Ollama:

```powershell
docker compose up -d qdrant ollama
```

Pull the default Ollama model:

```powershell
docker compose --profile model run --rm ollama-pull
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

Run Qdrant and Ollama in Docker:

```powershell
docker compose up -d qdrant ollama
```

Run the backend from the repo root:

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --app-dir backend
```

Run the frontend locally:

```powershell
cd frontend
npm install
$env:VITE_PROXY_TARGET = "http://localhost:8000"
npm run dev
```

## API Endpoints

- `GET /health` - checks Qdrant, Ollama, model availability, and collection size.
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
| `OLLAMA_BASE_URL` | `http://localhost:11434` |
| `OLLAMA_MODEL` | `llama3.2:3b` |
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

Local policy PDFs, `.env`, Qdrant storage, Ollama models, virtual environments,
frontend dependencies, and build output are ignored by Git. Treat `policies/`,
`qdrant_data/`, and `ollama/` as local runtime data unless you intentionally
prepare sanitized samples for sharing.

## Troubleshooting

- If `/health` reports `model_missing`, rerun the Ollama model pull command.
- If `/health` reports Qdrant errors, start Qdrant with `docker compose up -d qdrant`.
- If the UI shows `Offline`, confirm the backend is running on port `8000`.
- If answers have no sources, re-run ingestion and check that PDFs have
  extractable text and metadata tables.
