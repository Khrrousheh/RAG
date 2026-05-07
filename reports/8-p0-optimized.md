# 8-p0-optimized Latency Bottleneck Report

Date: 2026-05-07  
Repository: `c:\Users\MKhrrousheh\Desktop\RAG`  
Benchmark target: current P0 optimized working tree served temporarily on `http://127.0.0.1:8010`  
Benchmark command: `python benchmarks/p0_latency_benchmark.py --api-base http://127.0.0.1:8010 --llm-base http://localhost:12434 --model ai/gemma3-qat --samples 2 --timeout 240`

## Executive Summary

The P0 optimization improves perceived latency by streaming sources and tokens. The user now receives retrieved sources in about 32 to 62 ms instead of waiting for the whole LLM response. However, the dominant bottleneck is still Docker Model Runner generation on the local CPU backend: streamed chat took about 54 to 55 seconds end to end, and first token latency was still 16 to 25 seconds.

Compared with the earlier baseline report, total LLM chat latency improved from a measured 131.1 seconds for one non-streamed API sample to about 54.1 to 55.3 seconds in this optimized streamed path. Retrieval remains fast and is not the bottleneck.

## Measurement Caveats

- The repo's normal compose ports were already occupied by another local stack, so the current working tree was run temporarily on port `8010`.
- The benchmark used 2 samples because each LLM generation is expensive on the local CPU runtime.
- Treat p95 values as sample maxima for this run, not statistically mature p95s.
- The benchmark measured current uncommitted P0 changes, including `/chat/stream`, prompt budgeting, `num_predict=384`, LLM keep-alive, and startup warmups.

## Dataset Snapshot

| Item | Value |
| --- | ---: |
| Qdrant collection | `company_policies_structural` |
| Qdrant points | 298 |
| Benchmark `top_k` | 6 |
| Streamed prompt context | 3,563 chars |
| Streamed prompt total | 3,939 chars |
| Prompt build time | 0.66 to 1.00 ms |

## Benchmark Results

| Operation | p50 | max | Notes |
| --- | ---: | ---: | --- |
| `/search` | 97.63 ms | 196.86 ms | 6 sources, 5,711 retrieved source chars |
| `/chat`, `use_llm=false` | 33.57 ms | 76.12 ms | fallback answer path only |
| `/chat/stream` sources event | 31.52 ms | 62.43 ms | first useful UI payload |
| `/chat/stream` first token | 16,362.92 ms | 24,553.30 ms | LLM startup/prompt eval dominated |
| `/chat/stream` total | 54,148.67 ms | 55,281.67 ms | full streamed answer |
| `/chat/stream` retrieval metric | 27.46 ms | 38.37 ms | backend-reported retrieval |
| `/chat/stream` LLM total metric | 54,108.32 ms | 55,210.40 ms | backend-reported LLM duration |
| direct LLM stream first token | 8,064.19 ms | 8,167.37 ms | tiny 5-word prompt |
| direct LLM stream total | 8,914.56 ms | 9,082.06 ms | tiny 5-word prompt |

## Bottleneck Ranking

| Rank | Bottleneck | Evidence | Impact |
| ---: | --- | --- | --- |
| 1 | LLM generation throughput | `/chat/stream` LLM total is about 54-55 s | Still dominates total request time |
| 2 | First token latency | `/chat/stream` TTFT is about 16-25 s | User sees sources quickly, but waits for actual answer text |
| 3 | Prompt/context size | streamed prompt is 3,939 chars | Adds prompt eval cost before token output |
| 4 | Retrieval | retrieval is about 27-38 ms | Healthy; not a current bottleneck |
| 5 | Prompt assembly | about 1 ms | Negligible |

## Latency Estimates By Flow

### Optimized Page Interaction

Estimated useful feedback latency: 30 to 70 ms.

The streaming endpoint emits sources before generation completes. This means the UI can show retrieved policy cards almost immediately even when the answer text is still pending.

### Optimized Warm Search

Estimated latency: 75 to 225 ms.

Measured `/search` results were 97.63 ms p50 and 196.86 ms max. This includes embedding, Qdrant search, payload serialization, and API overhead.

### Optimized Chat Without LLM

Estimated latency: 30 to 90 ms.

Measured fallback chat was 33.57 ms p50 and 76.12 ms max. This path is retrieval plus deterministic fallback formatting.

### Optimized Streamed Chat

Estimated latency:

| Milestone | Estimate |
| --- | ---: |
| Sources visible | 30 to 70 ms |
| First answer token | 16 to 25 s |
| Full answer | 54 to 56 s |

This is the key P0 improvement: perceived progress is immediate, but actual generation still depends on the local model runtime.

### Direct Tiny LLM Call

Estimated latency: about 8 to 9 seconds.

Even a tiny direct streaming prompt took about 8.1 seconds to first token and about 9.1 seconds total. That means a large part of first-token latency is inside the local model runtime, not retrieval or FastAPI.

## P0 Optimization Impact

| Area | Baseline report | P0 optimized report | Result |
| --- | ---: | ---: | --- |
| First visible retrieval feedback | non-streamed, effectively after full answer | 31.52 to 62.43 ms | major perceived latency win |
| Full LLM chat sample | 131.1 s | 54.1 to 55.3 s | about 58% faster in this run |
| Retrieval path | about 17 ms component p50, 72 ms API p50 | 27-38 ms backend metric, 98 ms API p50 | still healthy |
| Prompt build | not separately measured | about 1 ms | negligible |

## Remaining Root Causes

1. CPU-backed `ai/gemma3-qat` generation is slow.
   The optimized app can stream, but it cannot make the model produce tokens quickly.

2. First token is expensive even for tiny prompts.
   Direct LLM streaming took about 8 seconds to first token before RAG context was added.

3. RAG prompt evaluation adds additional delay.
   The full RAG prompt was about 3,939 chars and pushed TTFT to 16-25 seconds.

4. Total answer length still matters.
   Streamed answers measured 1,613 and 2,094 chars. At this local generation speed, each extra paragraph is expensive.

## Recommended Next Optimizations

| Priority | Change | Expected effect |
| ---: | --- | --- |
| 1 | Keep streaming as the default UI path | Preserves the 30-70 ms source feedback win |
| 2 | Reduce target answer length to about 120-170 words for normal questions | Should reduce total generation time |
| 3 | Reduce `PROMPT_CONTEXT_MAX_CHARS` from 3600 to 2400-3000 and default prompt sources from 5 to 3-4 | Should improve TTFT by reducing prompt eval |
| 4 | Add a "brief answer" mode for common policy questions | Gives users faster answers when citations are enough |
| 5 | Benchmark a smaller/faster model or GPU-backed runtime | Only likely path to bring full answers below 10-20 s |
| 6 | Record benchmark JSON artifacts for each run | Makes regressions measurable over time |

## Bottom Line

P0 optimization successfully changes the experience from "wait silently for a long request" to "show sources almost immediately, then stream the answer." The remaining bottleneck is not retrieval or API code; it is local LLM first-token latency and token generation throughput.
