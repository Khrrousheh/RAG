# P1 Optimized Latency Bottleneck Report

Date: 2026-05-07  
Repository: `c:\Users\MKhrrousheh\Desktop\RAG`  
Benchmark target: running Docker Compose backend on `http://localhost:8000`  
Benchmark command: `python benchmarks/p0_latency_benchmark.py --api-base http://localhost:8000 --llm-base http://localhost:12434 --model ai/gemma3-qat --samples 2 --timeout 240`

## Executive Summary

The P1 changes improve retrieval-side latency significantly. Cached search measured about 14 to 15 ms, and backend-reported streamed-chat retrieval measured 3.89 ms with an embedding cache hit. Metadata is also effectively cheap at this corpus size, measuring 21.34 ms p50.

The dominant remaining bottleneck is still LLM generation through Docker Model Runner. Streamed chat took 35.68 to 40.83 seconds end to end. A separate direct tiny LLM stream still took about 8 seconds to first token, so most of the remaining latency is inside the local model runtime rather than Qdrant, embedding, or FastAPI.

One important regression/risk surfaced in this Dockerized run: the client did not receive the `sources` stream event until about 12.3 to 12.8 seconds, almost exactly when the first token arrived. Internally, retrieval completed in about 4 ms, so this looks like chunk buffering or flush behavior rather than slow retrieval.

## Measurement Caveats

- Only 2 samples were taken because each LLM generation is slow on the local CPU-backed model runtime.
- Treat p95 values as sample maxima, not statistically mature p95s.
- The benchmark ran against the Docker Compose backend on `localhost:8000`.
- The compose stack stopped after the benchmark run, so these measurements reflect the completed run, not a still-running service.
- P1 benchmark output includes cached search, metadata cache behavior, embedding cache metrics, Qdrant timing, streaming milestones, and direct LLM timing.

## P1 Runtime Features Observed

| Feature | Evidence |
| --- | --- |
| `/chat/stream` endpoint | Present in OpenAPI |
| Embedding cache | `embedding_cache_hit=true` in streamed chat metrics |
| Metadata warmup/cache | Backend startup log: metadata warmup completed in 118.92 ms |
| LLM keep-alive | Config includes `OLLAMA_KEEP_ALIVE=30m` |
| Prompt budgeting | Streamed prompt measured 3,939 chars with 3,563 context chars |
| Reduced generation cap | `OLLAMA_NUM_PREDICT=384` |

## Dataset Snapshot

| Item | Value |
| --- | ---: |
| Qdrant collection | `company_policies_structural` |
| Qdrant points | 298 |
| Policies in metadata | 30 |
| Departments in metadata | 2 |
| Versions in metadata | 2 |
| Benchmark `top_k` | 6 |
| Retrieved source chars | 5,711 |
| Prompt context chars | 3,563 |
| Prompt chars | 3,939 |

## Benchmark Results

| Operation | p50 | max | Notes |
| --- | ---: | ---: | --- |
| `/metadata` | 21.34 ms | 45.42 ms | 30 policies, 2 departments, 2 versions |
| `/search` | 18.38 ms | 92.91 ms | first run includes non-cached embedding cost |
| cached `/search` | 14.12 ms | 15.14 ms | repeated query path |
| `/chat`, `use_llm=false` | 13.95 ms | 37.94 ms | retrieval plus fallback formatting |
| `/chat/stream` sources seen by client | 12,304.04 ms | 12,752.22 ms | delayed until first-token window |
| `/chat/stream` first token | 12,304.12 ms | 12,752.28 ms | LLM prompt eval/startup dominated |
| `/chat/stream` total | 35,677.44 ms | 40,827.23 ms | full streamed answer |
| backend retrieval metric | 3.89 ms | 3.89 ms | metric present in first stream sample |
| backend embedding metric | 0.00 ms | 0.00 ms | embedding cache hit |
| backend Qdrant metric | 2.52 ms | 2.52 ms | vector search is not the bottleneck |
| prompt build metric | 0.50 ms | 0.50 ms | negligible |
| backend LLM total metric | 40,629.64 ms | 40,629.64 ms | metric present in first stream sample |
| direct tiny LLM first token | 7,942.36 ms | 8,112.91 ms | tiny non-RAG prompt |
| direct tiny LLM total | 8,635.66 ms | 8,916.94 ms | tiny non-RAG prompt |

## Bottleneck Ranking

| Rank | Bottleneck | Evidence | Impact |
| ---: | --- | --- | --- |
| 1 | LLM generation throughput | streamed chat total is 35.7 to 40.8 s | Still dominates complete answer latency |
| 2 | First token latency | streamed TTFT is 12.3 to 12.8 s | User waits a long time before answer text |
| 3 | Stream chunk delivery/buffering | sources were ready internally in about 4 ms but visible after about 12 s | Weakens the main UX benefit of streaming |
| 4 | Prompt/context size | prompt is 3,939 chars | Adds prompt-evaluation cost before first token |
| 5 | Retrieval/Qdrant | retrieval is 3.89 ms and Qdrant is 2.52 ms | Healthy; no longer a meaningful bottleneck |

## P1 Impact Versus P0 Report

| Area | P0 optimized report | P1 optimized measurement | Result |
| --- | ---: | ---: | --- |
| Cached search | not measured | 14.12 to 15.14 ms | clear P1 win |
| Warm `/search` | 97.63 ms p50 | 18.38 ms p50 | faster in this run |
| `/chat` without LLM | 33.57 ms p50 | 13.95 ms p50 | faster in this run |
| Backend retrieval metric | 27.46 ms p50 | 3.89 ms | embedding cache win |
| Streamed total chat | 54.1 to 55.3 s | 35.7 to 40.8 s | faster, mostly from shorter output/model state |
| First visible sources | 31.52 to 62.43 ms | 12.3 to 12.8 s | regression in Dockerized stream delivery |
| Direct tiny LLM TTFT | about 8.1 s | about 8.0 s | unchanged model-runtime floor |

## Latency Estimates By Flow

### Metadata

Estimated local latency: 20 to 50 ms.

The metadata cache is working well for the current 298-point corpus. This path is not a priority unless the corpus grows substantially.

### Cached Search

Estimated local latency: 10 to 20 ms.

Repeated queries benefit from the embedding cache. Backend metrics show `embedding_ms=0.0`, `embedding_cache_hit=true`, and Qdrant around 2.5 ms.

### First Search For A New Query

Estimated local latency: 20 to 100 ms after startup warmup.

The measured non-cached search max was 92.91 ms. This is a major improvement from the earlier cold-load behavior because the embedding model is already warm.

### Chat Without LLM

Estimated local latency: 15 to 40 ms.

The fallback path is now very fast because retrieval is cached/warm and no generation is involved.

### Streamed Chat

Estimated latency:

| Milestone | Estimate |
| --- | ---: |
| Retrieval ready inside backend | 3 to 10 ms |
| Sources visible to benchmark client | 12 to 13 s in this Docker run |
| First answer token | 12 to 13 s |
| Full answer | 35 to 41 s |

The key problem is that the source event is not reaching the client as soon as it is yielded.

## Recommendations

| Priority | Change | Expected effect |
| ---: | --- | --- |
| 1 | Investigate streaming flush behavior in Docker: try SSE `text/event-stream`, an initial heartbeat/padding chunk, and direct `curl --no-buffer` checks | Restore source visibility to sub-100 ms |
| 2 | Keep the embedding cache and metadata warmup/cache | Preserves the 10-20 ms retrieval path |
| 3 | Reduce prompt context from 3,600 chars toward 2,400-3,000 for general questions | Should improve first-token latency |
| 4 | Add a concise-answer mode around 120-170 words | Reduces full generation time |
| 5 | Benchmark a faster model or GPU-backed runtime | Only likely way to push full answers below 10-20 s |
| 6 | Save benchmark JSON artifacts per run | Makes P0/P1 comparisons reproducible |

## Bottom Line

P1 succeeds on retrieval: cached search, metadata, embedding, and Qdrant timings are all comfortably fast. The remaining work is almost entirely generation and streaming delivery. The surprising P1 finding is that the backend has sources ready in milliseconds, but the Dockerized path delivered them to the client only when the first token arrived around 12 seconds later.
