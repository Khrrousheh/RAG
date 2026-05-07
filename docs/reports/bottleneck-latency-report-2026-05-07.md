# RAG Bottleneck Latency Report

Date: 2026-05-07  
Repository: `c:\Users\MKhrrousheh\Desktop\RAG`  
Scope: read/benchmark analysis plus this report file. No application code was modified.

## Executive Summary

The main user-visible bottleneck is LLM generation through Docker Model Runner. Warm retrieval is fast: embedding plus Qdrant search measured at about 17 ms p50, and `/chat` with `use_llm=false` measured at about 75 ms p50. In contrast, a real `/chat` call with LLM enabled measured 131.1 seconds for a 1,729-character answer with 6 retrieved sources.

The second bottleneck is first-query embedding model warmup. The embedding model cold load measured 8.8 seconds locally, and the first API `/search` sample had a 14.6 second max latency. After warmup, search latency returned to tens of milliseconds.

## Measurement Caveats

- The current repo's compose stack was not fully running. Another local compose project was already bound to ports `5173`, `8000`, and `6333`.
- The existing local backend on `localhost:8000` reported the expected collection, `company_policies_structural`, with 298 points, so it was used for API timing samples.
- A brief attempt to start this repo's Qdrant service failed because `localhost:6333` was already occupied; the stopped container was removed afterward.
- The Qdrant and API measurements are local workstation measurements, not production SLOs.
- Samples are small. Treat p50 and max values as a local bottleneck baseline, not a statistically complete p95/p99 benchmark.

## Current Runtime Shape

The main chat path is:

`frontend -> FastAPI /chat -> lazy HuggingFace embedding -> Qdrant search -> prompt assembly -> Docker Model Runner /api/chat -> response`

Relevant implementation points:

- Frontend sends `top_k: 6` to `/chat`: `frontend/src/App.tsx`.
- Backend caps `top_k` at `MAX_TOP_K=10`: `backend/app/main.py`, `backend/app/config.py`.
- Embedding model is loaded lazily on first retrieval: `backend/app/rag.py`.
- Qdrant search is synchronous and returns payloads without vectors.
- LLM generation is synchronous and non-streaming.
- Model context size is configured as 4096 in `docker-compose.yml`.

## Dataset Snapshot

| Item | Local value |
| --- | ---: |
| Policy PDFs | 30 |
| Policy PDF bytes | 29,262,989 bytes, about 27.9 MiB |
| Parsed PDFs | 30 |
| Skipped PDFs | 0 |
| Produced chunks | 298 |
| Qdrant collection points | 298 |
| Local `qdrant_data` bytes | 483,152,794 bytes, about 460.8 MiB |
| Qdrant scroll batches at limit 128 | 3 |

## Benchmark Snapshot

| Operation | Measured latency | Notes |
| --- | ---: | --- |
| PDF parse only | 34.318 s | `collect_chunks(policies)` over 30 PDFs, no embedding/upsert |
| Embedding model cold load | 8.767 s | `sentence-transformers/all-MiniLM-L6-v2` |
| Warm query embedding | 9.64 ms p50 | min 9.13 ms, max 206.19 ms |
| Batch embed 64 chunks | 2,043.42 ms | about 31.93 ms/chunk |
| Qdrant `get_collection` | 10.60 ms p50 | min 3.26 ms, max 25.58 ms |
| Qdrant metadata scroll | 49.28 ms | 298 records over 3 batches |
| Qdrant unfiltered search | 6.45 ms p50 | min 5.11 ms, max 27.13 ms |
| Qdrant file-filtered search | 5.57 ms p50 | min 4.16 ms, max 31.14 ms |
| Warm embed plus Qdrant search | 16.79 ms p50 | min 13.50 ms, max 57.91 ms |
| Docker Model Runner `/api/tags` | 12.71 ms p50 | max outlier 2,164.27 ms |
| Direct short LLM call | 8.313 s | 6 response chars |
| Direct synthetic RAG-shaped LLM call | 31.387 s | 444 response chars, synthetic context |
| API `/health` | 48.71 ms p50 | min 35.30 ms, max 92.46 ms |
| API `/metadata` | 20.01 ms p50 | min 19.18 ms, max 87.54 ms |
| API `/search` | 72.33 ms p50 | max 14,636.67 ms from cold embedding/model warmup |
| API `/chat`, `use_llm=false` | 74.97 ms p50 | min 73.53 ms, max 84.52 ms |
| API `/chat`, `use_llm=true` | 131,106.33 ms | one sample, 1,729 answer chars, 6 sources |

## Bottleneck Ranking

| Rank | Bottleneck | Evidence | Current impact |
| ---: | --- | --- | --- |
| 1 | LLM generation | API `/chat` with LLM took 131.1 s; direct RAG-shaped LLM call took 31.4 s | Dominates total user latency, often over 99% of request time |
| 2 | Embedding cold start | Host cold load was 8.8 s; first API `/search` max was 14.6 s | First search/chat after backend restart can feel stalled |
| 3 | PDF parsing during ingestion | 30 PDFs parsed in 34.3 s before embedding/upsert | Re-ingestion is batch/offline, but slow enough to affect iteration |
| 4 | Metadata and alias full scrolls | Full scroll is 20 to 50 ms at 298 points | Fine now, grows linearly with corpus size |
| 5 | Qdrant vector search | Search p50 is 5 to 7 ms | Not currently a bottleneck |

## Latency Estimates By Flow

### Page Load Health Check

Estimated local latency: 35 to 100 ms normally, with occasional 1 to 3 second outliers if Docker Model Runner tag checks stall.

Reasoning: `/health` checks Qdrant collection status and Model Runner `/api/tags`. API p50 measured 48.71 ms; direct `/api/tags` had one 2.16 s outlier.

### First Search After Backend Restart

Estimated local latency: 8 to 15 seconds.

Reasoning: `RagService.embeddings` lazily loads the Sentence Transformers model. Host cold load measured 8.767 s, and API `/search` max measured 14.637 s.

### Warm Search

Estimated local latency: 30 to 150 ms.

Reasoning: warm embedding plus Qdrant search measured 16.79 ms p50. API `/search` p50 measured 72.33 ms after including FastAPI, serialization, source mapping, and local container overhead.

### Warm Chat Without LLM

Estimated local latency: 70 to 100 ms.

Reasoning: `/chat` with `use_llm=false` measured 74.97 ms p50. This path still performs retrieval, but skips generation.

### Warm Chat With LLM

Estimated local latency: 30 to 140 seconds for typical current answers.

Practical model from measured local output speed:

`estimated seconds = 8 s fixed overhead + output_chars / 13 to 15 chars_per_second + context overhead`

Approximate examples:

| Answer size | Estimated latency |
| ---: | ---: |
| 400 chars | 35 to 45 s |
| 1,000 chars | 75 to 90 s |
| 1,700 chars | 125 to 140 s |

The measured API LLM sample produced 1,729 characters in 131.1 seconds, which matches this estimate. The backend timeout is 240 seconds, so long answers can approach timeout territory on this local CPU-backed setup.

### Re-Ingestion

Estimated local latency: 55 to 75 seconds for the current 30-PDF corpus, excluding any model downloads.

Reasoning:

- PDF parse measured 34.318 s.
- Embedding model cold load measured 8.767 s.
- Batch embedding cost estimated from 31.93 ms/chunk across 298 chunks: about 9.5 s.
- Qdrant collection/index/upsert overhead is likely a few seconds at this corpus size.

## Root Causes

1. LLM response generation is synchronous and non-streaming.
   The frontend receives nothing until `/chat` fully completes, so a 30 to 130 second generation looks like a frozen request.

2. Answer length is the largest controllable multiplier.
   The backend allows up to `OLLAMA_NUM_PREDICT=700`, and the prompt asks for practical, non-trivial answers. The measured full API sample returned 1,729 characters.

3. Embeddings load lazily.
   This keeps startup lighter, but shifts the model load penalty onto the first real user search/chat.

4. Retrieval is already fast at the current corpus size.
   Qdrant search and metadata scans are not worth optimizing before generation and warmup.

5. Corpus-scale operations are linear where expected.
   Metadata and policy-alias discovery scroll every point. This is fine at 298 points, but should be revisited before the corpus grows into the tens of thousands of chunks.

## Recommended Mitigations

| Priority | Change | Expected latency impact |
| ---: | --- | --- |
| 1 | Stream LLM responses from Model Runner to FastAPI to React | Does not reduce total generation time, but improves perceived latency from 30 to 130 s down to first-token time |
| 2 | Reduce `OLLAMA_NUM_PREDICT` from 700 to about 300 to 400 and tighten the prompt length target | Likely cuts long-answer latency by 30 to 50% |
| 3 | Warm the embedding model during backend startup or in a background startup task | Removes the 8 to 15 s first-query penalty |
| 4 | Add an answer-length guard in the prompt or post-generation policy | Keeps common answers closer to 400 to 900 chars, saving tens of seconds |
| 5 | Use a faster local model/runtime, preferably GPU-backed if available | Could reduce generation latency by several multiples |
| 6 | Reduce default frontend `top_k` from 6 to 4 or 5 for normal chat | Small to moderate improvement by reducing prompt context size |
| 7 | Cache metadata and policy aliases after ingestion/startup | Avoids future linear-scroll growth; low current impact |
| 8 | Move from deprecated Qdrant `search` client call to `query_points` | Compatibility improvement; minimal latency impact |

## Suggested Measurement Follow-Up

To make this repeatable, add a benchmark script that records:

- cold `/search`
- warm `/search`
- `/chat` with `use_llm=false`
- `/chat` with `use_llm=true` for fixed answer-size prompts
- direct Model Runner generation latency
- p50, p95, max, answer chars, source count, and warning count

Recommended sample size: at least 20 warm requests per endpoint, with a separate cold-start run after backend restart.

## Bottom Line

Retrieval is healthy. The current bottleneck is generation, followed by lazy embedding warmup. The fastest user-visible win is streaming the LLM response. The fastest total-latency win is reducing generated answer length and/or moving generation to a faster runtime.
