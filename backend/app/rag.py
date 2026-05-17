from __future__ import annotations

import json
import logging
import re
import time as monotonic_time
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import anyio
import httpx
from langchain_community.embeddings import HuggingFaceEmbeddings
from qdrant_client import QdrantClient, models

from .config import Settings, get_settings
from .schemas import ChatRequest, SearchRequest, Source


LOGGER = logging.getLogger(__name__)
MODEL_RUNNER_NAME = "Docker Model Runner"
POLICY_ASSISTANT_SYSTEM_PROMPT = (
    "You are a precise company policy assistant. "
    "Answer only from the supplied policy context, but be practical and scenario-aware. "
    "For broad workplace questions, give a clear recommendation, allowed actions, "
    "not-allowed actions, approval steps, and a safe example when useful. "
    "If the context is insufficient, say what is missing. "
    "Cite policy claims with source bracket numbers like [1]. "
    "Do not invent policy names, section numbers, or approvals that are not in the context."
)
_EMBEDDING_LOAD_LOCK = Lock()
_SERVICE_LOCK = Lock()
_SERVICE: "RagService | None" = None


@dataclass(frozen=True)
class PromptBuild:
    prompt: str
    context_chars: int
    prompt_source_count: int
    prompt_chars: int


@dataclass(frozen=True)
class PreparedChat:
    sources: list[Source]
    warnings: list[str]
    prompt: str
    retrieval_ms: float
    prompt_build_ms: float
    context_chars: int
    prompt_source_count: int
    prompt_chars: int
    retrieval_metrics: dict[str, Any]


@dataclass(frozen=True)
class SearchResult:
    sources: list[Source]
    resolved_policy: tuple[str, str] | None
    metrics: dict[str, Any]


def _model_runner_api_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    normalized = re.sub(r"/engines(?:/[^/]+)?/v1$", "", normalized)
    normalized = re.sub(r"/v1$", "", normalized)
    return re.sub(r"/api$", "", normalized)


def _model_name_candidates(model_name: str) -> set[str]:
    candidates = {model_name}
    last_segment = model_name.rsplit("/", 1)[-1]
    candidates.add(last_segment)
    if ":" in last_segment:
        candidates.add(model_name.rsplit(":", 1)[0])
        candidates.add(last_segment.rsplit(":", 1)[0])
    else:
        candidates.add(f"{model_name}:latest")
        candidates.add(f"{last_segment}:latest")

    for candidate in list(candidates):
        if "/" in candidate and not candidate.startswith("docker.io/"):
            candidates.add(f"docker.io/{candidate}")

    return candidates


def _display_policy_name(file_name: str | None, policy_title: str | None = None) -> str | None:
    if file_name:
        return Path(file_name).stem
    return policy_title


def _normalize_policy_text(value: str) -> str:
    value = value.lower().replace("poilcy", "policy")
    value = re.sub(r"\.pdf\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _without_policy_suffix(value: str) -> str:
    return re.sub(r"\bpolicy\b", " ", value, flags=re.IGNORECASE).strip()


def _acronym(value: str) -> str | None:
    words = [
        word
        for word in re.findall(r"[A-Za-z0-9]+", value)
        if word.lower() not in {"policy", "and", "of", "the"}
    ]
    if len(words) < 2:
        return None
    acronym = "".join(word[0] for word in words).upper()
    if len(acronym) <= 2 and acronym not in {"HR", "IT"}:
        return None
    return acronym


def _contains_alias(query: str, alias: str) -> bool:
    if not alias or alias == "policy":
        return False
    pattern = rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
    return re.search(pattern, query) is not None


def _is_public_sharing_question(query: str) -> bool:
    normalized = _normalize_policy_text(query)
    social_terms = {
        "linkedin",
        "social media",
        "post",
        "portfolio",
        "showcase",
        "progress",
        "work proof",
        "proof of work",
        "public",
        "share",
        "publish",
    }
    return any(term in normalized for term in social_terms)


def _expanded_retrieval_query(query: str) -> str:
    if not _is_public_sharing_question(query):
        return query

    expansion = (
        " public post external disclosure confidential information client private "
        "company private personal data source code screenshots credentials access tokens "
        "architecture vulnerabilities intellectual property copyright data protection "
        "media handling information classification confidentiality non disclosure "
        "approval exception compliance acceptable use"
    )
    return f"{query} {expansion}"


def _source_ref(sources: list[Source], *name_terms: str) -> str:
    for source in sources:
        haystack = _normalize_policy_text(
            " ".join(
                item
                for item in (
                    source.policy_name,
                    source.policy_title,
                    source.file_name,
                    source.text[:240],
                )
                if item
            )
        )
        if any(_normalize_policy_text(term) in haystack for term in name_terms):
            return f"[{source.id}]"
    return "[source]"


def _as_utc_datetime(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _build_filter(
    policy_file_name: str | None = None,
    department: str | None = None,
    version: str | None = None,
    effective_date_from: date | None = None,
    effective_date_to: date | None = None,
) -> models.Filter | None:
    conditions: list[models.FieldCondition] = []

    if policy_file_name:
        conditions.append(
            models.FieldCondition(
                key="file_name",
                match=models.MatchValue(value=policy_file_name),
            )
        )

    if department:
        conditions.append(
            models.FieldCondition(
                key="Department",
                match=models.MatchValue(value=department),
            )
        )

    if version:
        conditions.append(
            models.FieldCondition(
                key="Version",
                match=models.MatchValue(value=version),
            )
        )

    if effective_date_from or effective_date_to:
        conditions.append(
            models.FieldCondition(
                key="effective_date",
                range=models.DatetimeRange(
                    gte=_as_utc_datetime(effective_date_from) if effective_date_from else None,
                    lte=_as_utc_datetime(effective_date_to) if effective_date_to else None,
                ),
            )
        )

    if not conditions:
        return None

    return models.Filter(must=conditions)


def _payload_to_source(index: int, score: float, payload: dict[str, Any]) -> Source:
    text = str(payload.get("text", "")).strip()
    file_name = payload.get("file_name")
    policy_title = payload.get("Policy Title")
    return Source(
        id=index,
        score=round(float(score), 4),
        policy_name=_display_policy_name(file_name, policy_title),
        file_name=file_name,
        page=payload.get("page"),
        department=payload.get("Department"),
        version=payload.get("Version"),
        effective_date=payload.get("Effective Date") or payload.get("effective_date"),
        policy_title=policy_title,
        text=text,
    )


def _source_to_json(source: Source) -> dict[str, Any]:
    return source.model_dump(mode="json")


def _stream_event(event: str, **payload: Any) -> str:
    return json.dumps({"event": event, **payload}, separators=(",", ":")) + "\n"


def _format_source_for_prompt(source: Source, text: str | None = None) -> str:
    title = source.policy_name or source.policy_title or source.file_name or "Unknown policy"
    page = f"page {source.page}" if source.page else "unknown page"
    metadata = (
        f"Department: {source.department or 'Unknown'}; "
        f"Version: {source.version or 'Unknown'}; "
        f"Effective Date: {source.effective_date or 'Unknown'}"
    )
    return f"[{source.id}] {title}, {page}. {metadata}\n{text if text is not None else source.text}"


def _append_unique_warning(warnings: list[str], warning: str | None) -> None:
    if warning and warning not in warnings:
        warnings.append(warning)


def _policy_note(sources: list[Source]) -> str:
    if not sources:
        return "Policy note: No matching policy passages were retrieved."

    labels: list[str] = []
    seen: set[tuple[str, int]] = set()
    for source in sources:
        title = source.policy_name or source.policy_title or source.file_name or "Unknown policy"
        key = (title, source.id)
        if key in seen:
            continue
        seen.add(key)
        labels.append(f"{title} [{source.id}]")
        if len(labels) >= 4:
            break

    remaining = len(sources) - len(labels)
    suffix = f" and {remaining} more source(s)" if remaining > 0 else ""
    return f"Policy note: Retrieved policy context from {', '.join(labels)}{suffix}."


def _llm_unavailable_warning(exc: BaseException | None = None) -> str:
    reason = "generation failed"
    if isinstance(exc, httpx.TimeoutException):
        reason = "request timed out"
    elif isinstance(exc, httpx.HTTPStatusError):
        reason = f"service returned HTTP {exc.response.status_code}"
    elif isinstance(exc, httpx.ConnectError):
        reason = "connection failed"
    elif isinstance(exc, httpx.RequestError):
        reason = "request failed"
    elif exc is not None and "empty" in str(exc).lower():
        reason = "empty response"

    return f"LLM unavailable: {reason}. Showing retrieved policy context instead."


def _fallback_answer(
    message: str,
    sources: list[Source],
    *,
    llm_warning: str | None = None,
) -> str:
    prefix = f"{llm_warning}\n\n" if llm_warning else ""

    if not sources:
        return prefix + (
            "I could not find matching policy passages for that question. "
            "Try removing filters or asking with a more specific policy term."
        )

    if _is_public_sharing_question(message):
        acceptable_ref = _source_ref(sources, "Acceptable Use")
        data_ref = _source_ref(sources, "Data Protection")
        media_ref = _source_ref(sources, "Media Handling")
        masking_ref = _source_ref(sources, "Data Masking")
        answer = (
            "Short answer: yes, you can share a LinkedIn progress post, but keep it high-level, "
            "sanitized, and non-confidential.\n\n"
            "Safe to include:\n"
            "- Your role, learning progress, general technologies, generic engineering practices, and outcomes.\n"
            "- A sanitized description of the problem class, for example: \"improved retrieval quality in a policy RAG chatbot.\"\n"
            "- Your own diagrams or screenshots only after removing client names, internal URLs, tickets, repository names, keys, tokens, logs, emails, employee/customer data, and architecture details.\n\n"
            "Do not include:\n"
            f"- Client-private, company-private, confidential, sensitive, or need-to-know information {media_ref}.\n"
            f"- Personal data, user data, employee data, customer data, or anything disclosed externally without a valid basis {data_ref}.\n"
            f"- Source code, credentials, access tokens, vulnerability details, internal infrastructure, SOW/MSA/SLA details, screenshots of internal systems, or unpublished project names {media_ref} {masking_ref}.\n"
            f"- Copyrighted material or third-party content unless you have permission {acceptable_ref}.\n\n"
            "Before posting:\n"
            f"- Ask your manager or the policy owner/security team if the post reveals company, client, or project information {acceptable_ref}.\n"
            "- If you need proof of work, use redacted screenshots, synthetic/sample data, or a recreated demo that does not expose real company assets.\n"
            "- If approval is required or you are unsure, get it in writing before publishing.\n\n"
            "Safer LinkedIn wording:\n"
            "\"I have been building a policy-aware RAG chatbot that improves document ingestion, metadata filtering, and retrieval quality. "
            "The work helped me practice FastAPI, React, Qdrant, and structured PDF extraction while keeping confidential data out of public examples.\"\n\n"
            "Bottom line: share the engineering journey, not the company secrets."
        )
        return f"{prefix}{answer}"

    lines: list[str] = []
    if llm_warning:
        lines.extend([llm_warning, ""])
    lines.extend(["Here are the strongest retrieved policy references:", ""])
    for source in sources[:3]:
        title = source.policy_name or source.policy_title or source.file_name or "Unknown policy"
        page = f"p. {source.page}" if source.page else "page unknown"
        preview = source.text[:450].strip()
        suffix = "" if len(source.text) <= 450 else "..."
        lines.append(f"[{source.id}] {title} ({page}, score {source.score})")
        lines.append(f"{preview}{suffix}")
        lines.append("")

    lines.append(f"Question: {message}")
    return "\n".join(lines).strip()


def _post_filter_named_part(query: str, sources: list[Source]) -> list[Source]:
    if _is_public_sharing_question(query):
        return sources

    section_terms = (
        "compliance",
        "scope",
        "purpose",
        "responsibility",
        "responsibilities",
        "approval",
        "disclosure",
        "exceptions",
        "non compliance",
        "employment",
        "terms and conditions",
    )
    normalized_query = _normalize_policy_text(query)
    requested_terms = [term for term in section_terms if term in normalized_query]
    if not requested_terms:
        return sources

    filtered = [
        source
        for source in sources
        if any(term in _normalize_policy_text(source.text) for term in requested_terms)
    ]
    selected = filtered or sources
    for index, source in enumerate(selected, start=1):
        source.id = index
    return selected


class RagService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.qdrant = QdrantClient(
            url=settings.qdrant_url,
            check_compatibility=False,
        )
        self._embeddings: HuggingFaceEmbeddings | None = None
        self._policy_aliases: list[tuple[str, str, str]] | None = None
        self._metadata_cache: dict[str, list[str]] | None = None
        self._embedding_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._metadata_cache_lock = Lock()
        self._policy_alias_lock = Lock()
        self._embedding_cache_lock = Lock()
        self._async_client: httpx.AsyncClient | None = None

    @property
    def model_runner_base_url(self) -> str:
        return _model_runner_api_base_url(self.settings.ollama_base_url)

    async def startup(self) -> None:
        self._ensure_async_client()

    async def shutdown(self) -> None:
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None

    def _ensure_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None:
            limits = httpx.Limits(
                max_connections=self.settings.http_max_connections,
                max_keepalive_connections=self.settings.http_max_keepalive_connections,
            )
            timeout = httpx.Timeout(self.settings.ollama_timeout_seconds, connect=10.0)
            self._async_client = httpx.AsyncClient(timeout=timeout, limits=limits)
        return self._async_client

    @property
    def embeddings(self) -> HuggingFaceEmbeddings:
        if self._embeddings is None:
            with _EMBEDDING_LOAD_LOCK:
                if self._embeddings is None:
                    LOGGER.info("Loading embedding model %s", self.settings.embedding_model)
                    self._embeddings = HuggingFaceEmbeddings(
                        model_name=self.settings.embedding_model,
                        encode_kwargs={"normalize_embeddings": True},
                    )
        return self._embeddings

    def _embed_query_cached(self, query: str) -> tuple[list[float], bool, float]:
        cache_key = _normalize_policy_text(query)
        cache_size = max(0, self.settings.embedding_cache_size)

        if cache_size > 0:
            with self._embedding_cache_lock:
                cached = self._embedding_cache.get(cache_key)
                if cached is not None:
                    self._embedding_cache.move_to_end(cache_key)
                    return cached, True, 0.0

        start = monotonic_time.perf_counter()
        vector = self.embeddings.embed_query(query)
        embedding_ms = (monotonic_time.perf_counter() - start) * 1000

        if cache_size > 0:
            with self._embedding_cache_lock:
                self._embedding_cache[cache_key] = vector
                self._embedding_cache.move_to_end(cache_key)
                while len(self._embedding_cache) > cache_size:
                    self._embedding_cache.popitem(last=False)

        return vector, False, embedding_ms

    def warm_embeddings(self) -> None:
        start = monotonic_time.perf_counter()
        self._embed_query_cached("warmup")
        LOGGER.info(
            "Embedding warmup completed in %.2f ms",
            (monotonic_time.perf_counter() - start) * 1000,
        )

    def warm_metadata(self) -> None:
        start = monotonic_time.perf_counter()
        metadata = self.metadata(refresh=True)
        aliases = self._get_policy_aliases(refresh=True)
        LOGGER.info(
            "Metadata warmup completed in %.2f ms departments=%s versions=%s policies=%s aliases=%s",
            (monotonic_time.perf_counter() - start) * 1000,
            len(metadata["departments"]),
            len(metadata["versions"]),
            len(metadata["policies"]),
            len(aliases),
        )

    def warm_llm(self) -> None:
        start = monotonic_time.perf_counter()
        try:
            response = httpx.post(
                f"{self.model_runner_base_url}/api/chat",
                json=self._llm_payload(
                    "Reply with OK.",
                    stream=False,
                    num_ctx=128,
                    num_predict=1,
                ),
                timeout=min(30.0, self.settings.ollama_timeout_seconds),
            )
            response.raise_for_status()
            LOGGER.info(
                "%s warmup completed in %.2f ms",
                MODEL_RUNNER_NAME,
                (monotonic_time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            LOGGER.warning("%s warmup failed: %s", MODEL_RUNNER_NAME, exc)

    async def warm_llm_async(self) -> None:
        start = monotonic_time.perf_counter()
        try:
            response = await self._ensure_async_client().post(
                f"{self.model_runner_base_url}/api/chat",
                json=self._llm_payload(
                    "Reply with OK.",
                    stream=False,
                    num_ctx=128,
                    num_predict=1,
                ),
                timeout=min(30.0, self.settings.ollama_timeout_seconds),
            )
            response.raise_for_status()
            LOGGER.info(
                "%s async warmup completed in %.2f ms",
                MODEL_RUNNER_NAME,
                (monotonic_time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            LOGGER.warning("%s async warmup failed: %s", MODEL_RUNNER_NAME, exc)

    def _llm_payload(
        self,
        prompt: str,
        *,
        stream: bool,
        num_ctx: int | None = None,
        num_predict: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.settings.ollama_model,
            "stream": stream,
            "messages": [
                {"role": "system", "content": POLICY_ASSISTANT_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "options": {
                "temperature": 0.1,
                "num_ctx": num_ctx or self.settings.ollama_num_ctx,
                "num_predict": num_predict or self.settings.ollama_num_predict,
            },
        }
        if self.settings.ollama_keep_alive:
            payload["keep_alive"] = self.settings.ollama_keep_alive
        return payload

    def health(self) -> dict[str, Any]:
        warnings: list[str] = []
        qdrant_status = "ok"
        ollama_status = "ok"
        points_count: int | None = None

        try:
            collection = self.qdrant.get_collection(self.settings.qdrant_collection)
            points_count = collection.points_count
        except Exception as exc:
            qdrant_status = "error"
            warnings.append(f"Qdrant collection check failed: {exc}")

        try:
            response = httpx.get(
                f"{self.model_runner_base_url}/api/tags",
                timeout=3,
            )
            response.raise_for_status()
            models_payload = response.json().get("models", [])
            model_names = {
                value
                for item in models_payload
                for value in (item.get("name"), item.get("model"))
                if value
            }
            expected_model_names = _model_name_candidates(self.settings.ollama_model)
            if not expected_model_names.intersection(model_names):
                ollama_status = "model_missing"
                warnings.append(
                    f"{MODEL_RUNNER_NAME} is reachable, but model "
                    f"{self.settings.ollama_model!r} is not pulled."
                )
        except Exception as exc:
            ollama_status = "error"
            warnings.append(f"{MODEL_RUNNER_NAME} check failed: {exc}")

        status = "ok" if qdrant_status == "ok" else "degraded"
        if ollama_status != "ok":
            status = "degraded"

        return {
            "status": status,
            "qdrant": qdrant_status,
            "ollama": ollama_status,
            "collection": self.settings.qdrant_collection,
            "points_count": points_count,
            "warnings": warnings,
        }

    async def health_async(self) -> dict[str, Any]:
        warnings: list[str] = []
        qdrant_status = "ok"
        ollama_status = "ok"
        points_count: int | None = None

        try:
            collection = await anyio.to_thread.run_sync(
                self.qdrant.get_collection,
                self.settings.qdrant_collection,
            )
            points_count = collection.points_count
        except Exception as exc:
            qdrant_status = "error"
            warnings.append(f"Qdrant collection check failed: {exc}")

        try:
            response = await self._ensure_async_client().get(
                f"{self.model_runner_base_url}/api/tags",
                timeout=3,
            )
            response.raise_for_status()
            models_payload = response.json().get("models", [])
            model_names = {
                value
                for item in models_payload
                for value in (item.get("name"), item.get("model"))
                if value
            }
            expected_model_names = _model_name_candidates(self.settings.ollama_model)
            if not expected_model_names.intersection(model_names):
                ollama_status = "model_missing"
                warnings.append(
                    f"{MODEL_RUNNER_NAME} is reachable, but model "
                    f"{self.settings.ollama_model!r} is not pulled."
                )
        except Exception as exc:
            ollama_status = "error"
            warnings.append(f"{MODEL_RUNNER_NAME} check failed: {exc}")

        status = "ok" if qdrant_status == "ok" else "degraded"
        if ollama_status != "ok":
            status = "degraded"

        return {
            "status": status,
            "qdrant": qdrant_status,
            "ollama": ollama_status,
            "collection": self.settings.qdrant_collection,
            "points_count": points_count,
            "warnings": warnings,
        }

    def search_with_metrics(self, request: SearchRequest) -> SearchResult:
        total_start = monotonic_time.perf_counter()
        top_k = min(request.top_k, self.settings.max_top_k)
        resolve_start = monotonic_time.perf_counter()
        resolved_policy = self.resolve_policy(request.policy or request.query)
        resolve_policy_ms = (monotonic_time.perf_counter() - resolve_start) * 1000
        retrieval_query = _expanded_retrieval_query(request.query)
        query_vector, embedding_cache_hit, embedding_ms = self._embed_query_cached(retrieval_query)
        query_filter = _build_filter(
            policy_file_name=resolved_policy[1] if resolved_policy else None,
            department=request.department,
            version=request.version,
            effective_date_from=request.effective_date_from,
            effective_date_to=request.effective_date_to,
        )

        search_limit = min(max(top_k * 2, top_k), self.settings.max_top_k)
        qdrant_start = monotonic_time.perf_counter()
        results = self.qdrant.search(
            collection_name=self.settings.qdrant_collection,
            query_vector=query_vector,
            query_filter=query_filter,
            limit=search_limit,
            with_payload=True,
            with_vectors=False,
        )
        qdrant_ms = (monotonic_time.perf_counter() - qdrant_start) * 1000

        sources: list[Source] = []
        for index, point in enumerate(results, start=1):
            sources.append(_payload_to_source(index, point.score, point.payload or {}))

        post_filter_start = monotonic_time.perf_counter()
        sources = _post_filter_named_part(request.query, sources)
        post_filter_ms = (monotonic_time.perf_counter() - post_filter_start) * 1000
        selected_sources = sources[:top_k]
        total_ms = (monotonic_time.perf_counter() - total_start) * 1000
        metrics = {
            "total_ms": round(total_ms, 2),
            "resolve_policy_ms": round(resolve_policy_ms, 2),
            "embedding_ms": round(embedding_ms, 2),
            "embedding_cache_hit": embedding_cache_hit,
            "qdrant_ms": round(qdrant_ms, 2),
            "post_filter_ms": round(post_filter_ms, 2),
            "requested_top_k": request.top_k,
            "effective_top_k": top_k,
            "search_limit": search_limit,
            "returned_sources": len(selected_sources),
            "candidate_sources": len(sources),
            "resolved_policy": resolved_policy[0] if resolved_policy else None,
        }
        LOGGER.info(
            "search_latency total_ms=%.2f resolve_policy_ms=%.2f embedding_ms=%.2f "
            "embedding_cache_hit=%s qdrant_ms=%.2f post_filter_ms=%.2f "
            "returned_sources=%s search_limit=%s resolved_policy=%s",
            total_ms,
            resolve_policy_ms,
            embedding_ms,
            embedding_cache_hit,
            qdrant_ms,
            post_filter_ms,
            len(selected_sources),
            search_limit,
            metrics["resolved_policy"],
        )
        return SearchResult(
            sources=selected_sources,
            resolved_policy=resolved_policy,
            metrics=metrics,
        )

    def search(self, request: SearchRequest) -> list[Source]:
        return self.search_with_metrics(request).sources

    async def search_async(self, request: SearchRequest) -> list[Source]:
        return await anyio.to_thread.run_sync(self.search, request)

    def prepare_chat(self, request: ChatRequest) -> PreparedChat:
        retrieval_start = monotonic_time.perf_counter()
        search_result = self.search_with_metrics(
            SearchRequest(
                query=request.message,
                policy=request.policy,
                department=request.department,
                version=request.version,
                effective_date_from=request.effective_date_from,
                effective_date_to=request.effective_date_to,
                top_k=request.top_k,
            )
        )
        retrieval_ms = (monotonic_time.perf_counter() - retrieval_start) * 1000
        sources = search_result.sources
        warnings: list[str] = [_policy_note(sources)]
        if search_result.resolved_policy:
            _append_unique_warning(warnings, f"Filtered retrieval to {search_result.resolved_policy[0]}.")

        if not request.use_llm:
            return PreparedChat(
                sources=sources,
                warnings=warnings,
                prompt="",
                retrieval_ms=retrieval_ms,
                prompt_build_ms=0.0,
                context_chars=0,
                prompt_source_count=0,
                prompt_chars=0,
                retrieval_metrics=search_result.metrics,
            )

        prompt_start = monotonic_time.perf_counter()
        prompt_build = self._build_prompt(request, sources)
        prompt_build_ms = (monotonic_time.perf_counter() - prompt_start) * 1000
        return PreparedChat(
            sources=sources,
            warnings=warnings,
            prompt=prompt_build.prompt,
            retrieval_ms=retrieval_ms,
            prompt_build_ms=prompt_build_ms,
            context_chars=prompt_build.context_chars,
            prompt_source_count=prompt_build.prompt_source_count,
            prompt_chars=prompt_build.prompt_chars,
            retrieval_metrics=search_result.metrics,
        )

    def chat(self, request: ChatRequest) -> tuple[str, list[Source], list[str]]:
        request_start = monotonic_time.perf_counter()
        prepared = self.prepare_chat(request)

        if not request.use_llm:
            answer = _fallback_answer(request.message, prepared.sources)
            self._log_chat_metrics(
                prepared=prepared,
                answer_chars=len(answer),
                total_ms=(monotonic_time.perf_counter() - request_start) * 1000,
                stream=False,
            )
            return answer, prepared.sources, prepared.warnings

        try:
            llm_start = monotonic_time.perf_counter()
            response = httpx.post(
                f"{self.model_runner_base_url}/api/chat",
                json=self._llm_payload(prepared.prompt, stream=False),
                timeout=self.settings.ollama_timeout_seconds,
            )
            response.raise_for_status()
            answer = response.json().get("message", {}).get("content", "").strip()
            if not answer:
                raise RuntimeError(f"{MODEL_RUNNER_NAME} returned an empty answer.")
            self._log_chat_metrics(
                prepared=prepared,
                answer_chars=len(answer),
                total_ms=(monotonic_time.perf_counter() - request_start) * 1000,
                llm_total_ms=(monotonic_time.perf_counter() - llm_start) * 1000,
                stream=False,
            )
            return answer, prepared.sources, prepared.warnings
        except Exception as exc:
            LOGGER.warning("%s generation failed: %s", MODEL_RUNNER_NAME, exc)
            warning = _llm_unavailable_warning(exc)
            _append_unique_warning(prepared.warnings, warning)
            answer = _fallback_answer(request.message, prepared.sources, llm_warning=warning)
            self._log_chat_metrics(
                prepared=prepared,
                answer_chars=len(answer),
                total_ms=(monotonic_time.perf_counter() - request_start) * 1000,
                stream=False,
                fallback=True,
                llm_unavailable=True,
            )
            return answer, prepared.sources, prepared.warnings

    async def chat_async(self, request: ChatRequest) -> tuple[str, list[Source], list[str]]:
        request_start = monotonic_time.perf_counter()
        prepared = await anyio.to_thread.run_sync(self.prepare_chat, request)

        if not request.use_llm:
            answer = _fallback_answer(request.message, prepared.sources)
            self._log_chat_metrics(
                prepared=prepared,
                answer_chars=len(answer),
                total_ms=(monotonic_time.perf_counter() - request_start) * 1000,
                stream=False,
            )
            return answer, prepared.sources, prepared.warnings

        try:
            llm_start = monotonic_time.perf_counter()
            response = await self._ensure_async_client().post(
                f"{self.model_runner_base_url}/api/chat",
                json=self._llm_payload(prepared.prompt, stream=False),
                timeout=self.settings.ollama_timeout_seconds,
            )
            response.raise_for_status()
            answer = response.json().get("message", {}).get("content", "").strip()
            if not answer:
                raise RuntimeError(f"{MODEL_RUNNER_NAME} returned an empty answer.")
            self._log_chat_metrics(
                prepared=prepared,
                answer_chars=len(answer),
                total_ms=(monotonic_time.perf_counter() - request_start) * 1000,
                llm_total_ms=(monotonic_time.perf_counter() - llm_start) * 1000,
                stream=False,
            )
            return answer, prepared.sources, prepared.warnings
        except Exception as exc:
            LOGGER.warning("%s async generation failed: %s", MODEL_RUNNER_NAME, exc)
            warning = _llm_unavailable_warning(exc)
            _append_unique_warning(prepared.warnings, warning)
            answer = _fallback_answer(request.message, prepared.sources, llm_warning=warning)
            self._log_chat_metrics(
                prepared=prepared,
                answer_chars=len(answer),
                total_ms=(monotonic_time.perf_counter() - request_start) * 1000,
                stream=False,
                fallback=True,
                llm_unavailable=True,
            )
            return answer, prepared.sources, prepared.warnings

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[str]:
        request_start = monotonic_time.perf_counter()
        try:
            prepared = await anyio.to_thread.run_sync(self.prepare_chat, request)
        except Exception as exc:
            LOGGER.exception("Chat preparation failed: %s", exc)
            yield _stream_event("error", message="Chat preparation failed.")
            return

        yield _stream_event(
            "sources",
            sources=[_source_to_json(source) for source in prepared.sources],
            warnings=prepared.warnings,
        )

        if not request.use_llm:
            answer = _fallback_answer(request.message, prepared.sources)
            yield _stream_event("token", content=answer)
            metrics = self._chat_metrics(
                prepared=prepared,
                answer_chars=len(answer),
                total_ms=(monotonic_time.perf_counter() - request_start) * 1000,
                stream=True,
            )
            self._log_chat_metrics_from_dict(metrics)
            yield _stream_event("metrics", metrics=metrics)
            yield _stream_event("done")
            return

        answer_parts: list[str] = []
        llm_start = monotonic_time.perf_counter()
        first_token_ms: float | None = None
        fallback = False
        llm_unavailable = False

        try:
            async with self._ensure_async_client().stream(
                "POST",
                f"{self.model_runner_base_url}/api/chat",
                json=self._llm_payload(prepared.prompt, stream=True),
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        LOGGER.debug("Skipping invalid stream chunk from %s", MODEL_RUNNER_NAME)
                        continue

                    content = (payload.get("message") or {}).get("content") or ""
                    if content:
                        if first_token_ms is None:
                            first_token_ms = (monotonic_time.perf_counter() - llm_start) * 1000
                        answer_parts.append(content)
                        yield _stream_event("token", content=content)

                    if payload.get("done"):
                        break
        except Exception as exc:
            llm_unavailable = True
            fallback = True
            LOGGER.warning("%s streaming generation failed: %s", MODEL_RUNNER_NAME, exc)
            warning = _llm_unavailable_warning(exc)
            yield _stream_event("warning", message=warning)
            _append_unique_warning(prepared.warnings, warning)
            answer = _fallback_answer(request.message, prepared.sources, llm_warning=warning)
            if answer_parts:
                answer = f"\n\n{answer}"
            answer_parts.append(answer)
            yield _stream_event("token", content=answer)

        if not answer_parts:
            fallback = True
            llm_unavailable = True
            warning = _llm_unavailable_warning(RuntimeError(f"{MODEL_RUNNER_NAME} returned an empty streamed answer."))
            _append_unique_warning(prepared.warnings, warning)
            yield _stream_event("warning", message=warning)
            answer = _fallback_answer(request.message, prepared.sources, llm_warning=warning)
            answer_parts.append(answer)
            yield _stream_event("token", content=answer)

        metrics = self._chat_metrics(
            prepared=prepared,
            answer_chars=len("".join(answer_parts)),
            total_ms=(monotonic_time.perf_counter() - request_start) * 1000,
            llm_ttft_ms=first_token_ms,
            llm_total_ms=(monotonic_time.perf_counter() - llm_start) * 1000,
            stream=True,
            fallback=fallback,
            llm_unavailable=llm_unavailable,
        )
        self._log_chat_metrics_from_dict(metrics)
        yield _stream_event("metrics", metrics=metrics)
        yield _stream_event("done")

    def _chat_metrics(
        self,
        *,
        prepared: PreparedChat,
        answer_chars: int,
        total_ms: float,
        llm_ttft_ms: float | None = None,
        llm_total_ms: float | None = None,
        stream: bool,
        fallback: bool = False,
        llm_unavailable: bool = False,
    ) -> dict[str, Any]:
        metrics: dict[str, Any] = {
            "retrieval_ms": round(prepared.retrieval_ms, 2),
            "prompt_build_ms": round(prepared.prompt_build_ms, 2),
            "embedding_ms": prepared.retrieval_metrics.get("embedding_ms"),
            "embedding_cache_hit": prepared.retrieval_metrics.get("embedding_cache_hit"),
            "qdrant_ms": prepared.retrieval_metrics.get("qdrant_ms"),
            "resolve_policy_ms": prepared.retrieval_metrics.get("resolve_policy_ms"),
            "post_filter_ms": prepared.retrieval_metrics.get("post_filter_ms"),
            "llm_ttft_ms": round(llm_ttft_ms, 2) if llm_ttft_ms is not None else None,
            "llm_total_ms": round(llm_total_ms, 2) if llm_total_ms is not None else None,
            "total_ms": round(total_ms, 2),
            "source_count": len(prepared.sources),
            "prompt_source_count": prepared.prompt_source_count,
            "context_chars": prepared.context_chars,
            "prompt_chars": prepared.prompt_chars,
            "answer_chars": answer_chars,
            "stream": stream,
            "fallback": fallback,
        }
        if llm_unavailable:
            metrics["llm_unavailable"] = True
        return metrics

    def _log_chat_metrics(
        self,
        *,
        prepared: PreparedChat,
        answer_chars: int,
        total_ms: float,
        llm_ttft_ms: float | None = None,
        llm_total_ms: float | None = None,
        stream: bool,
        fallback: bool = False,
        llm_unavailable: bool = False,
    ) -> None:
        self._log_chat_metrics_from_dict(
            self._chat_metrics(
                prepared=prepared,
                answer_chars=answer_chars,
                total_ms=total_ms,
                llm_ttft_ms=llm_ttft_ms,
                llm_total_ms=llm_total_ms,
                stream=stream,
                fallback=fallback,
                llm_unavailable=llm_unavailable,
            )
        )

    def _log_chat_metrics_from_dict(self, metrics: dict[str, Any]) -> None:
        LOGGER.info(
            "chat_latency stream=%s fallback=%s retrieval_ms=%.2f "
            "embedding_ms=%s embedding_cache_hit=%s qdrant_ms=%s "
            "prompt_build_ms=%.2f llm_ttft_ms=%s llm_total_ms=%s "
            "total_ms=%.2f source_count=%s prompt_source_count=%s "
            "context_chars=%s prompt_chars=%s answer_chars=%s",
            metrics["stream"],
            metrics["fallback"],
            metrics["retrieval_ms"],
            metrics["embedding_ms"],
            metrics["embedding_cache_hit"],
            metrics["qdrant_ms"],
            metrics["prompt_build_ms"],
            metrics["llm_ttft_ms"],
            metrics["llm_total_ms"],
            metrics["total_ms"],
            metrics["source_count"],
            metrics["prompt_source_count"],
            metrics["context_chars"],
            metrics["prompt_chars"],
            metrics["answer_chars"],
        )

    def metadata(self, refresh: bool = False) -> dict[str, list[str]]:
        if not refresh and self._metadata_cache is not None:
            return {
                key: list(value)
                for key, value in self._metadata_cache.items()
            }

        departments: set[str] = set()
        versions: set[str] = set()
        policies: set[str] = set()
        offset: int | str | None = None

        while True:
            records, offset = self.qdrant.scroll(
                collection_name=self.settings.qdrant_collection,
                offset=offset,
                limit=128,
                with_payload=True,
                with_vectors=False,
            )
            for record in records:
                payload = record.payload or {}
                if payload.get("Department"):
                    departments.add(str(payload["Department"]))
                if payload.get("Version"):
                    versions.add(str(payload["Version"]))
                policy_name = payload.get("Policy Title") or payload.get("file_name")
                file_name = payload.get("file_name")
                policy_name = _display_policy_name(str(file_name) if file_name else None, str(policy_name) if policy_name else None)
                if policy_name:
                    policies.add(str(policy_name))

            if offset is None:
                break

        metadata = {
            "departments": sorted(departments),
            "versions": sorted(versions),
            "policies": sorted(policies),
        }
        with self._metadata_cache_lock:
            self._metadata_cache = metadata
        return {
            key: list(value)
            for key, value in metadata.items()
        }

    async def metadata_async(self) -> dict[str, list[str]]:
        return await anyio.to_thread.run_sync(self.metadata)

    def _dedupe_sources_for_prompt(self, sources: list[Source]) -> list[Source]:
        selected: list[Source] = []
        seen_text: set[str] = set()
        seen_by_page: dict[tuple[str | None, int | None], list[str]] = {}

        for source in sources:
            normalized_text = _normalize_policy_text(source.text)
            if normalized_text and normalized_text in seen_text:
                continue
            page_key = (source.file_name, source.page)
            page_texts = seen_by_page.setdefault(page_key, [])
            if normalized_text and any(
                len(previous) > 240
                and (
                    normalized_text in previous
                    or previous in normalized_text
                )
                for previous in page_texts
            ):
                continue
            if normalized_text:
                seen_text.add(normalized_text)
                page_texts.append(normalized_text)
            selected.append(source)

        return selected

    def _build_budgeted_context(self, sources: list[Source]) -> tuple[str, int]:
        if not sources:
            return "No matching policy context was retrieved.", 0

        max_sources = max(1, self.settings.prompt_max_sources)
        min_sources = max(1, min(self.settings.prompt_min_sources, max_sources))
        unique_sources = self._dedupe_sources_for_prompt(sources)
        source_count = min(max_sources, len(unique_sources))
        if len(unique_sources) >= min_sources:
            source_count = max(min_sources, source_count)
        selected_sources = unique_sources[:source_count]

        max_context_chars = max(500, self.settings.prompt_context_max_chars)
        separators_chars = max(0, len(selected_sources) - 1) * 2
        header_chars = 0
        for source in selected_sources:
            header_chars += len(_format_source_for_prompt(source, text=""))

        available_text_chars = max_context_chars - header_chars - separators_chars
        text_budget = max(120, available_text_chars // max(len(selected_sources), 1))

        context_parts: list[str] = []
        for source in selected_sources:
            text = source.text.strip()
            if len(text) > text_budget:
                text = text[:text_budget].rsplit(" ", 1)[0].rstrip()
                if text:
                    text = f"{text}..."
                else:
                    text = source.text[:text_budget].rstrip()
            context_parts.append(_format_source_for_prompt(source, text=text))

        context = "\n\n".join(context_parts)
        if len(context) > max_context_chars:
            context = f"{context[: max_context_chars - 3].rstrip()}..."

        return context, len(selected_sources)

    def _build_prompt(self, request: ChatRequest, sources: list[Source]) -> PromptBuild:
        history = "\n".join(
            f"{turn.role}: {turn.content}" for turn in request.history[-6:]
        )
        context, prompt_source_count = self._build_budgeted_context(sources)

        prompt = (
            f"Conversation so far:\n{history or 'None'}\n\n"
            f"Policy context:\n{context}\n\n"
            f"User question:\n{request.message}\n\n"
            "Write a direct, concise answer in about 120-170 words unless the question is simpler. "
            "Use short bullets only when they improve scanning. Include policy title, department, version, "
            "and effective date when relevant. End each policy-based recommendation with a source id."
        )
        return PromptBuild(
            prompt=prompt,
            context_chars=len(context),
            prompt_source_count=prompt_source_count,
            prompt_chars=len(prompt),
        )

    def resolve_policy(self, text: str | None) -> tuple[str, str] | None:
        """Return (display policy name, exact file_name) when a policy is named."""
        if not text:
            return None

        query = _normalize_policy_text(text)
        best_match: tuple[int, str, str] | None = None

        for alias, display_name, file_name in self._get_policy_aliases():
            if not _contains_alias(query, alias):
                continue

            score = len(alias.split()) * 100 + len(alias)
            if best_match is None or score > best_match[0]:
                best_match = (score, display_name, file_name)

        if best_match is None:
            return None

        return best_match[1], best_match[2]

    def _get_policy_aliases(self, refresh: bool = False) -> list[tuple[str, str, str]]:
        if not refresh and self._policy_aliases is not None:
            return self._policy_aliases

        with self._policy_alias_lock:
            if not refresh and self._policy_aliases is not None:
                return self._policy_aliases

            alias_rows: set[tuple[str, str, str]] = set()
            offset: int | str | None = None

            while True:
                records, offset = self.qdrant.scroll(
                    collection_name=self.settings.qdrant_collection,
                    offset=offset,
                    limit=128,
                    with_payload=True,
                    with_vectors=False,
                )
                for record in records:
                    payload = record.payload or {}
                    file_name = payload.get("file_name")
                    if not file_name:
                        continue

                    file_name = str(file_name)
                    policy_title = str(payload.get("Policy Title") or "")
                    display_name = _display_policy_name(file_name, policy_title) or file_name
                    candidate_phrases = {
                        file_name,
                        Path(file_name).stem,
                        display_name,
                        policy_title,
                        _without_policy_suffix(Path(file_name).stem),
                        _without_policy_suffix(display_name),
                        _without_policy_suffix(policy_title),
                    }

                    for phrase in list(candidate_phrases):
                        acronym = _acronym(phrase)
                        if acronym:
                            candidate_phrases.add(acronym)
                            candidate_phrases.add(f"{acronym} Policy")

                    for phrase in candidate_phrases:
                        alias = _normalize_policy_text(phrase)
                        if len(alias) < 2 or alias == "policy":
                            continue
                        alias_rows.add((alias, display_name, file_name))

                if offset is None:
                    break

            self._policy_aliases = sorted(alias_rows, key=lambda row: len(row[0]), reverse=True)
            return self._policy_aliases


def get_rag_service() -> RagService:
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                _SERVICE = RagService(get_settings())
    return _SERVICE
