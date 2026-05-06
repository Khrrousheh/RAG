from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import httpx
from langchain_community.embeddings import HuggingFaceEmbeddings
from qdrant_client import QdrantClient, models

from .config import Settings, get_settings
from .schemas import ChatRequest, SearchRequest, Source


LOGGER = logging.getLogger(__name__)
_EMBEDDING_LOAD_LOCK = Lock()
_SERVICE_LOCK = Lock()
_SERVICE: "RagService | None" = None


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


def _format_source_for_prompt(source: Source) -> str:
    title = source.policy_name or source.policy_title or source.file_name or "Unknown policy"
    page = f"page {source.page}" if source.page else "unknown page"
    metadata = (
        f"Department: {source.department or 'Unknown'}; "
        f"Version: {source.version or 'Unknown'}; "
        f"Effective Date: {source.effective_date or 'Unknown'}"
    )
    return f"[{source.id}] {title}, {page}. {metadata}\n{source.text}"


def _fallback_answer(message: str, sources: list[Source]) -> str:
    if not sources:
        return (
            "I could not find matching policy passages for that question. "
            "Try removing filters or asking with a more specific policy term."
        )

    if _is_public_sharing_question(message):
        acceptable_ref = _source_ref(sources, "Acceptable Use")
        data_ref = _source_ref(sources, "Data Protection")
        media_ref = _source_ref(sources, "Media Handling")
        masking_ref = _source_ref(sources, "Data Masking")
        return (
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

    lines = [
        "I found relevant policy passages, but the LLM response service is unavailable. "
        "Here are the strongest retrieved references:",
        "",
    ]
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
                f"{self.settings.ollama_base_url}/api/tags",
                timeout=3,
            )
            response.raise_for_status()
            models_payload = response.json().get("models", [])
            model_names = {item.get("name") for item in models_payload}
            if self.settings.ollama_model not in model_names:
                ollama_status = "model_missing"
                warnings.append(
                    f"Ollama is reachable, but model {self.settings.ollama_model!r} is not pulled."
                )
        except Exception as exc:
            ollama_status = "error"
            warnings.append(f"Ollama check failed: {exc}")

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

    def search(self, request: SearchRequest) -> list[Source]:
        top_k = min(request.top_k, self.settings.max_top_k)
        resolved_policy = self.resolve_policy(request.policy or request.query)
        retrieval_query = _expanded_retrieval_query(request.query)
        query_vector = self.embeddings.embed_query(retrieval_query)
        query_filter = _build_filter(
            policy_file_name=resolved_policy[1] if resolved_policy else None,
            department=request.department,
            version=request.version,
            effective_date_from=request.effective_date_from,
            effective_date_to=request.effective_date_to,
        )

        results = self.qdrant.search(
            collection_name=self.settings.qdrant_collection,
            query_vector=query_vector,
            query_filter=query_filter,
            limit=min(max(top_k * 2, top_k), self.settings.max_top_k),
            with_payload=True,
            with_vectors=False,
        )

        sources: list[Source] = []
        for index, point in enumerate(results, start=1):
            sources.append(_payload_to_source(index, point.score, point.payload or {}))

        sources = _post_filter_named_part(request.query, sources)
        return sources[:top_k]

    def chat(self, request: ChatRequest) -> tuple[str, list[Source], list[str]]:
        resolved_policy = self.resolve_policy(request.policy or request.message)
        sources = self.search(
            SearchRequest(
                query=request.message,
                policy=resolved_policy[0] if resolved_policy else request.policy,
                department=request.department,
                version=request.version,
                effective_date_from=request.effective_date_from,
                effective_date_to=request.effective_date_to,
                top_k=request.top_k,
            )
        )
        warnings: list[str] = []
        if resolved_policy:
            warnings.append(f"Filtered retrieval to {resolved_policy[0]}.")

        if not request.use_llm:
            return _fallback_answer(request.message, sources), sources, warnings

        prompt = self._build_prompt(request, sources)
        try:
            response = httpx.post(
                f"{self.settings.ollama_base_url}/api/chat",
                json={
                    "model": self.settings.ollama_model,
                    "stream": False,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are a precise company policy assistant. "
                                "Answer only from the supplied policy context, but be practical and scenario-aware. "
                                "For broad workplace questions, give a clear recommendation, allowed actions, "
                                "not-allowed actions, approval steps, and a safe example when useful. "
                                "If the context is insufficient, say what is missing. "
                                "Cite policy claims with source bracket numbers like [1]. "
                                "Do not invent policy names, section numbers, or approvals that are not in the context."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "options": {
                        "temperature": 0.1,
                        "num_ctx": self.settings.ollama_num_ctx,
                        "num_predict": self.settings.ollama_num_predict,
                    },
                },
                timeout=self.settings.ollama_timeout_seconds,
            )
            response.raise_for_status()
            answer = response.json().get("message", {}).get("content", "").strip()
            if not answer:
                raise RuntimeError("Ollama returned an empty answer.")
            return answer, sources, warnings
        except Exception as exc:
            LOGGER.warning("Ollama generation failed: %s", exc)
            warnings.append(
                "Ollama could not generate an answer. Showing retrieved context instead."
            )
            return _fallback_answer(request.message, sources), sources, warnings

    def metadata(self) -> dict[str, list[str]]:
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

        return {
            "departments": sorted(departments),
            "versions": sorted(versions),
            "policies": sorted(policies),
        }

    def _build_prompt(self, request: ChatRequest, sources: list[Source]) -> str:
        history = "\n".join(
            f"{turn.role}: {turn.content}" for turn in request.history[-6:]
        )
        context = "\n\n".join(_format_source_for_prompt(source) for source in sources)
        if not context:
            context = "No matching policy context was retrieved."

        return (
            f"Conversation so far:\n{history or 'None'}\n\n"
            f"Policy context:\n{context}\n\n"
            f"User question:\n{request.message}\n\n"
            "Write a helpful, non-trivial answer. Prefer a practical checklist over a one-line answer. "
            "Keep the answer under 350 words. Include policy title, department, version, "
            "and effective date when relevant. End each policy-based recommendation with a source id."
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

    def _get_policy_aliases(self) -> list[tuple[str, str, str]]:
        if self._policy_aliases is not None:
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
