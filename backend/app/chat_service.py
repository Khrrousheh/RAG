from __future__ import annotations

import json
import logging
import time as monotonic_time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from .memory_service import MemoryHit, MemoryService
from .models import ChatSession, ConversationTurn, User
from .rag import (
    MODEL_RUNNER_NAME,
    RagService,
    _append_unique_warning,
    _fallback_answer,
    _llm_unavailable_warning,
    _policy_note,
    _source_to_json,
    _stream_event,
)
from .schemas import ChatRequest, ChatResponse, ChatTurn, MemoryUsageResponse, SearchRequest, Source
from .session_service import SessionService


LOGGER = logging.getLogger(__name__)


@dataclass
class PreparedMemoryChat:
    session: ChatSession
    user_turn: ConversationTurn
    sources: list[Source]
    warnings: list[str]
    prompt: str
    retrieval_ms: float
    prompt_build_ms: float
    context_chars: int
    prompt_source_count: int
    prompt_chars: int
    retrieval_metrics: dict[str, Any]
    short_history: list[ChatTurn]
    long_memories: list[MemoryHit]


class ChatService:
    def __init__(
        self,
        *,
        rag_service: RagService,
        session_service: SessionService,
        memory_service: MemoryService,
    ) -> None:
        self.rag_service = rag_service
        self.session_service = session_service
        self.memory_service = memory_service

    async def prepare_chat(
        self,
        db: AsyncSession,
        *,
        user: User,
        request: ChatRequest,
    ) -> PreparedMemoryChat:
        total_start = monotonic_time.perf_counter()
        session = await self.session_service.get_or_create_session(
            db,
            user,
            request.session_id,
            request.message,
        )
        short_history = await self.memory_service.load_short_term(
            db,
            user=user,
            session_id=session.id,
        )
        if not short_history and request.history:
            short_history = request.history[-self.rag_service.settings.short_term_memory_turns :]

        user_turn = await self.session_service.append_turn(
            db,
            user,
            session,
            role="user",
            content=request.message,
            token_count=estimate_tokens(request.message),
        )
        await db.commit()
        await db.refresh(session)

        search_request = SearchRequest(
            query=request.message,
            policy=request.policy,
            department=request.department,
            version=request.version,
            effective_date_from=request.effective_date_from,
            effective_date_to=request.effective_date_to,
            top_k=request.top_k,
        )
        search_result = await anyio.to_thread.run_sync(
            self.rag_service.search_with_metrics,
            search_request,
        )
        sources = search_result.sources
        warnings: list[str] = [_policy_note(sources)]
        if search_result.resolved_policy:
            _append_unique_warning(warnings, f"Filtered retrieval to {search_result.resolved_policy[0]}.")

        memory_start = monotonic_time.perf_counter()
        long_memories: list[MemoryHit] = []
        if request.use_llm:
            long_memories = await self.memory_service.retrieve_long_term(
                db,
                user=user,
                session_id=session.id,
                query=request.message,
                rag_service=self.rag_service,
            )
        memory_retrieval_ms = (monotonic_time.perf_counter() - memory_start) * 1000
        await db.commit()

        retrieval_ms = (monotonic_time.perf_counter() - total_start) * 1000
        retrieval_metrics = dict(search_result.metrics)
        retrieval_metrics["memory_retrieval_ms"] = round(memory_retrieval_ms, 2)
        retrieval_metrics["long_term_memory_count"] = len(long_memories)
        retrieval_metrics["short_term_turn_count"] = len(short_history)

        if not request.use_llm:
            return PreparedMemoryChat(
                session=session,
                user_turn=user_turn,
                sources=sources,
                warnings=warnings,
                prompt="",
                retrieval_ms=retrieval_ms,
                prompt_build_ms=0.0,
                context_chars=0,
                prompt_source_count=0,
                prompt_chars=0,
                retrieval_metrics=retrieval_metrics,
                short_history=short_history,
                long_memories=long_memories,
            )

        prompt_start = monotonic_time.perf_counter()
        prompt, context_chars, prompt_source_count = self._build_memory_prompt(
            request=request,
            sources=sources,
            short_history=short_history,
            long_memories=long_memories,
        )
        prompt_build_ms = (monotonic_time.perf_counter() - prompt_start) * 1000
        return PreparedMemoryChat(
            session=session,
            user_turn=user_turn,
            sources=sources,
            warnings=warnings,
            prompt=prompt,
            retrieval_ms=retrieval_ms,
            prompt_build_ms=prompt_build_ms,
            context_chars=context_chars,
            prompt_source_count=prompt_source_count,
            prompt_chars=len(prompt),
            retrieval_metrics=retrieval_metrics,
            short_history=short_history,
            long_memories=long_memories,
        )

    async def chat(
        self,
        db: AsyncSession,
        *,
        user: User,
        request: ChatRequest,
    ) -> ChatResponse:
        request_start = monotonic_time.perf_counter()
        prepared = await self.prepare_chat(db, user=user, request=request)
        fallback = False
        summary_enqueued = False

        if not request.use_llm:
            answer = _fallback_answer(request.message, prepared.sources)
            metrics = self._chat_metrics(
                prepared=prepared,
                answer_chars=len(answer),
                total_ms=(monotonic_time.perf_counter() - request_start) * 1000,
                stream=False,
            )
            summary_enqueued = await self._persist_assistant_and_memory(
                db,
                user=user,
                prepared=prepared,
                answer=answer,
                metrics=metrics,
            )
            return self._response(prepared, answer, summary_enqueued)

        try:
            llm_start = monotonic_time.perf_counter()
            response = await self.rag_service._ensure_async_client().post(
                f"{self.rag_service.model_runner_base_url}/api/chat",
                json=self.rag_service._llm_payload(prepared.prompt, stream=False),
                timeout=self.rag_service.settings.ollama_timeout_seconds,
            )
            response.raise_for_status()
            answer = response.json().get("message", {}).get("content", "").strip()
            if not answer:
                raise RuntimeError(f"{MODEL_RUNNER_NAME} returned an empty answer.")
            metrics = self._chat_metrics(
                prepared=prepared,
                answer_chars=len(answer),
                total_ms=(monotonic_time.perf_counter() - request_start) * 1000,
                llm_total_ms=(monotonic_time.perf_counter() - llm_start) * 1000,
                stream=False,
            )
        except Exception as exc:
            fallback = True
            LOGGER.warning("%s generation failed: %s", MODEL_RUNNER_NAME, exc)
            warning = _llm_unavailable_warning(exc)
            _append_unique_warning(prepared.warnings, warning)
            answer = _fallback_answer(request.message, prepared.sources, llm_warning=warning)
            metrics = self._chat_metrics(
                prepared=prepared,
                answer_chars=len(answer),
                total_ms=(monotonic_time.perf_counter() - request_start) * 1000,
                stream=False,
                fallback=fallback,
                llm_unavailable=True,
            )

        summary_enqueued = await self._persist_assistant_and_memory(
            db,
            user=user,
            prepared=prepared,
            answer=answer,
            metrics=metrics,
        )
        return self._response(prepared, answer, summary_enqueued)

    async def stream_chat(
        self,
        db: AsyncSession,
        *,
        user: User,
        request: ChatRequest,
    ) -> AsyncIterator[str]:
        request_start = monotonic_time.perf_counter()
        try:
            prepared = await self.prepare_chat(db, user=user, request=request)
        except Exception as exc:
            LOGGER.exception("Chat preparation failed: %s", exc)
            yield _stream_event("error", message="Chat preparation failed.")
            return

        yield _stream_event("session", session=self.session_service.to_response(prepared.session).model_dump(mode="json"))
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
            summary_enqueued = await self._persist_assistant_and_memory(
                db,
                user=user,
                prepared=prepared,
                answer=answer,
                metrics=metrics,
            )
            metrics["summary_enqueued"] = summary_enqueued
            self._log_chat_metrics(metrics)
            yield _stream_event("metrics", metrics=metrics)
            yield _stream_event("done")
            return

        answer_parts: list[str] = []
        llm_start = monotonic_time.perf_counter()
        first_token_ms: float | None = None
        fallback = False
        llm_unavailable = False

        try:
            async with self.rag_service._ensure_async_client().stream(
                "POST",
                f"{self.rag_service.model_runner_base_url}/api/chat",
                json=self.rag_service._llm_payload(prepared.prompt, stream=True),
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
            _append_unique_warning(prepared.warnings, warning)
            yield _stream_event("warning", message=warning)
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

        answer = "".join(answer_parts)
        metrics = self._chat_metrics(
            prepared=prepared,
            answer_chars=len(answer),
            total_ms=(monotonic_time.perf_counter() - request_start) * 1000,
            llm_ttft_ms=first_token_ms,
            llm_total_ms=(monotonic_time.perf_counter() - llm_start) * 1000,
            stream=True,
            fallback=fallback,
            llm_unavailable=llm_unavailable,
        )
        summary_enqueued = await self._persist_assistant_and_memory(
            db,
            user=user,
            prepared=prepared,
            answer=answer,
            metrics=metrics,
        )
        metrics["summary_enqueued"] = summary_enqueued
        self._log_chat_metrics(metrics)
        yield _stream_event("metrics", metrics=metrics)
        yield _stream_event("done")

    def _build_memory_prompt(
        self,
        *,
        request: ChatRequest,
        sources: list[Source],
        short_history: list[ChatTurn],
        long_memories: list[MemoryHit],
    ) -> tuple[str, int, int]:
        history = "\n".join(
            f"{turn.role}: {turn.content}" for turn in short_history[-6:]
        )
        policy_context, prompt_source_count = self.rag_service._build_budgeted_context(sources)
        memory_context = format_memory_context(long_memories)
        prompt = (
            f"Conversation so far:\n{history or 'None'}\n\n"
            f"User memory from prior sessions:\n{memory_context}\n\n"
            f"Policy context:\n{policy_context}\n\n"
            f"User question:\n{request.message}\n\n"
            "Write a helpful, concise answer. Use user memory only for continuity and personalization; "
            "do not treat memory as policy authority. Policy claims must be grounded in the policy context "
            "and cite source ids like [1]. Write a direct, concise answer in about 120-170 words unless "
            "the question is simpler. Use short bullets only when they improve scanning. Include policy "
            "title, department, version, and effective date when relevant."
        )
        return prompt, len(policy_context) + len(memory_context), prompt_source_count

    async def _persist_assistant_and_memory(
        self,
        db: AsyncSession,
        *,
        user: User,
        prepared: PreparedMemoryChat,
        answer: str,
        metrics: dict[str, Any],
    ) -> bool:
        await self.session_service.append_turn(
            db,
            user,
            prepared.session,
            role="assistant",
            content=answer,
            sources=[_source_to_json(source) for source in prepared.sources],
            warnings=prepared.warnings,
            metrics=metrics,
            token_count=estimate_tokens(answer),
        )
        await db.commit()
        await db.refresh(prepared.session)
        await self.memory_service.append_short_term(
            user_id=user.id,
            session_id=prepared.session.id,
            turns=[
                ChatTurn(role="user", content=prepared.user_turn.content),
                ChatTurn(role="assistant", content=answer),
            ],
        )
        return await self.memory_service.enqueue_summary_if_needed(
            db,
            user=user,
            session_id=prepared.session.id,
        )

    def _response(
        self,
        prepared: PreparedMemoryChat,
        answer: str,
        summary_enqueued: bool,
    ) -> ChatResponse:
        return ChatResponse(
            answer=answer,
            sources=prepared.sources,
            warnings=prepared.warnings,
            session=self.session_service.to_response(prepared.session),
            memory=MemoryUsageResponse(
                short_term_turns=len(prepared.short_history),
                long_term_memories=len(prepared.long_memories),
                summary_enqueued=summary_enqueued,
            ),
        )

    def _chat_metrics(
        self,
        *,
        prepared: PreparedMemoryChat,
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
            "memory_retrieval_ms": prepared.retrieval_metrics.get("memory_retrieval_ms"),
            "short_term_turn_count": prepared.retrieval_metrics.get("short_term_turn_count"),
            "long_term_memory_count": prepared.retrieval_metrics.get("long_term_memory_count"),
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

    @staticmethod
    def _log_chat_metrics(metrics: dict[str, Any]) -> None:
        LOGGER.info(
            "memory_chat_latency stream=%s fallback=%s retrieval_ms=%s "
            "memory_retrieval_ms=%s qdrant_ms=%s prompt_build_ms=%s "
            "llm_ttft_ms=%s llm_total_ms=%s total_ms=%s source_count=%s "
            "short_term_turns=%s long_term_memories=%s answer_chars=%s",
            metrics["stream"],
            metrics["fallback"],
            metrics["retrieval_ms"],
            metrics["memory_retrieval_ms"],
            metrics["qdrant_ms"],
            metrics["prompt_build_ms"],
            metrics["llm_ttft_ms"],
            metrics["llm_total_ms"],
            metrics["total_ms"],
            metrics["source_count"],
            metrics["short_term_turn_count"],
            metrics["long_term_memory_count"],
            metrics["answer_chars"],
        )


def format_memory_context(memories: list[MemoryHit]) -> str:
    if not memories:
        return "None"
    lines: list[str] = []
    for index, memory in enumerate(memories, start=1):
        topics = f" topics={', '.join(memory.topics)}" if memory.topics else ""
        lines.append(f"(M{index}, score {memory.score}{topics}) {memory.text}")
    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    return max(1, len(text.split()))

