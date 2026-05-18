from __future__ import annotations

import argparse
import json
import math
import time
import urllib.request
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark P0/P1 RAG latency paths.")
    parser.add_argument("--api-base", default="http://localhost:8000")
    parser.add_argument("--llm-base", default="http://localhost:12434")
    parser.add_argument("--model", default="hf.co/microsoft/Phi-3-mini-4k-instruct-gguf")
    parser.add_argument(
        "--question",
        default="Can I share progress about this project on LinkedIn?",
    )
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=240.0)
    return parser.parse_args()


def post_json(url: str, payload: dict[str, Any], timeout: float) -> tuple[dict[str, Any], float]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read())
    return data, (time.perf_counter() - start) * 1000


def get_json(url: str, timeout: float) -> tuple[dict[str, Any], float]:
    start = time.perf_counter()
    with urllib.request.urlopen(url, timeout=timeout) as response:
        data = json.loads(response.read())
    return data, (time.perf_counter() - start) * 1000


def post_ndjson_stream(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.perf_counter()
    sources_ms: float | None = None
    ttft_ms: float | None = None
    answer_chars = 0
    source_count = 0
    metrics: dict[str, Any] = {}

    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line:
                continue
            event = json.loads(line)
            event_name = event.get("event")
            now_ms = (time.perf_counter() - start) * 1000

            if event_name == "sources":
                sources_ms = now_ms
                source_count = len(event.get("sources") or [])
            elif event_name == "token":
                if ttft_ms is None:
                    ttft_ms = now_ms
                answer_chars += len(event.get("content") or "")
            elif event_name == "metrics":
                metrics = event.get("metrics") or {}
            elif event_name == "error":
                raise RuntimeError(event.get("message") or "stream error")

    total_ms = (time.perf_counter() - start) * 1000
    return {
        "sources_ms": round(sources_ms, 2) if sources_ms is not None else None,
        "ttft_ms": round(ttft_ms, 2) if ttft_ms is not None else None,
        "total_ms": round(total_ms, 2),
        "source_count": source_count,
        "answer_chars": answer_chars,
        "context_chars": metrics.get("context_chars"),
        "prompt_chars": metrics.get("prompt_chars"),
        "prompt_build_ms": metrics.get("prompt_build_ms"),
        "retrieval_ms": metrics.get("retrieval_ms"),
        "embedding_ms": metrics.get("embedding_ms"),
        "embedding_cache_hit": metrics.get("embedding_cache_hit"),
        "qdrant_ms": metrics.get("qdrant_ms"),
        "llm_total_ms": metrics.get("llm_total_ms"),
    }


def direct_llm_stream(
    llm_base: str,
    model: str,
    timeout: float,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "stream": True,
        "keep_alive": "30m",
        "messages": [{"role": "user", "content": "Answer in five words: define RAG."}],
        "options": {"temperature": 0.1, "num_ctx": 512, "num_predict": 24},
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{llm_base.rstrip('/')}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.perf_counter()
    ttft_ms: float | None = None
    answer_chars = 0

    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line:
                continue
            event = json.loads(line)
            content = (event.get("message") or {}).get("content") or ""
            if content and ttft_ms is None:
                ttft_ms = (time.perf_counter() - start) * 1000
            answer_chars += len(content)
            if event.get("done"):
                break

    return {
        "ttft_ms": round(ttft_ms, 2) if ttft_ms is not None else None,
        "total_ms": round((time.perf_counter() - start) * 1000, 2),
        "answer_chars": answer_chars,
    }


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * p) - 1))
    return round(ordered[index], 2)


def summarize(samples: list[dict[str, Any]], keys: list[str]) -> dict[str, Any]:
    summary: dict[str, Any] = {"samples": len(samples)}
    for key in keys:
        values = [float(sample[key]) for sample in samples if sample.get(key) is not None]
        summary[key] = {
            "p50": percentile(values, 0.50),
            "p95": percentile(values, 0.95),
            "max": round(max(values), 2) if values else None,
        }
    return summary


def main() -> int:
    args = parse_args()
    api_base = args.api_base.rstrip("/")
    payload = {"message": args.question, "query": args.question, "top_k": 6}
    results: dict[str, Any] = {}

    search_samples: list[dict[str, Any]] = []
    cached_search_samples: list[dict[str, Any]] = []
    metadata_samples: list[dict[str, Any]] = []
    fallback_samples: list[dict[str, Any]] = []
    stream_samples: list[dict[str, Any]] = []
    direct_samples: list[dict[str, Any]] = []

    for _ in range(args.samples):
        metadata_data, metadata_ms = get_json(f"{api_base}/metadata", args.timeout)
        metadata_samples.append(
            {
                "total_ms": round(metadata_ms, 2),
                "departments": len(metadata_data.get("departments") or []),
                "versions": len(metadata_data.get("versions") or []),
                "policies": len(metadata_data.get("policies") or []),
            }
        )

        search_data, search_ms = post_json(
            f"{api_base}/search",
            {"query": payload["query"], "top_k": payload["top_k"]},
            args.timeout,
        )
        search_sources = search_data.get("sources") or []
        search_samples.append(
            {
                "total_ms": round(search_ms, 2),
                "source_count": len(search_sources),
                "source_chars": sum(len(source.get("text") or "") for source in search_sources),
            }
        )

        cached_search_data, cached_search_ms = post_json(
            f"{api_base}/search",
            {"query": payload["query"], "top_k": payload["top_k"]},
            args.timeout,
        )
        cached_search_sources = cached_search_data.get("sources") or []
        cached_search_samples.append(
            {
                "total_ms": round(cached_search_ms, 2),
                "source_count": len(cached_search_sources),
                "source_chars": sum(len(source.get("text") or "") for source in cached_search_sources),
            }
        )

        chat_data, chat_ms = post_json(
            f"{api_base}/chat",
            {
                "message": payload["message"],
                "top_k": payload["top_k"],
                "use_llm": False,
            },
            args.timeout,
        )
        fallback_samples.append(
            {
                "total_ms": round(chat_ms, 2),
                "source_count": len(chat_data.get("sources") or []),
                "answer_chars": len(chat_data.get("answer") or ""),
            }
        )

        stream_samples.append(
            post_ndjson_stream(
                f"{api_base}/chat/stream",
                {"message": payload["message"], "top_k": payload["top_k"]},
                args.timeout,
            )
        )

        direct_samples.append(direct_llm_stream(args.llm_base, args.model, args.timeout))

    results["search"] = {
        "summary": summarize(search_samples, ["total_ms", "source_count", "source_chars"]),
        "samples": search_samples,
    }
    results["cached_search"] = {
        "summary": summarize(cached_search_samples, ["total_ms", "source_count", "source_chars"]),
        "samples": cached_search_samples,
    }
    results["metadata"] = {
        "summary": summarize(metadata_samples, ["total_ms", "departments", "versions", "policies"]),
        "samples": metadata_samples,
    }
    results["chat_without_llm"] = {
        "summary": summarize(fallback_samples, ["total_ms", "source_count", "answer_chars"]),
        "samples": fallback_samples,
    }
    results["chat_stream"] = {
        "summary": summarize(
            stream_samples,
            [
                "sources_ms",
                "ttft_ms",
                "total_ms",
                "retrieval_ms",
                "embedding_ms",
                "qdrant_ms",
                "prompt_build_ms",
                "llm_total_ms",
                "context_chars",
                "prompt_chars",
                "answer_chars",
            ],
        ),
        "samples": stream_samples,
    }
    results["direct_llm_stream"] = {
        "summary": summarize(direct_samples, ["ttft_ms", "total_ms", "answer_chars"]),
        "samples": direct_samples,
    }

    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
