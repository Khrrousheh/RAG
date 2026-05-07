from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid

import httpx
from sqlalchemy import select

from .config import get_settings
from .db import dispose_db, get_sessionmaker
from .memory_service import MemoryService
from .models import ConversationTurn, MemoryJob, User
from .rag import RagService
from .redis_client import close_redis, get_redis


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
LOGGER = logging.getLogger("memory_worker")


async def main() -> int:
    settings = get_settings()
    redis = get_redis()
    rag_service = RagService(settings)
    memory_service = MemoryService(settings=settings, redis=redis, qdrant=rag_service.qdrant)
    await rag_service.startup()

    LOGGER.info("Memory worker started queue=%s", settings.memory_job_queue)
    try:
        while True:
            item = await redis.blpop(
                [settings.memory_job_queue],
                timeout=settings.memory_worker_poll_timeout_seconds,
            )
            if item is None:
                continue
            _, job_id = item
            await process_job(uuid.UUID(str(job_id)), memory_service, rag_service)
    finally:
        await rag_service.shutdown()
        await close_redis()
        await dispose_db()


async def process_job(
    job_id: uuid.UUID,
    memory_service: MemoryService,
    rag_service: RagService,
) -> None:
    settings = get_settings()
    async_session = get_sessionmaker()
    async with async_session() as db:
        result = await db.execute(select(MemoryJob).where(MemoryJob.id == job_id))
        job = result.scalar_one_or_none()
        if job is None or job.status == "succeeded":
            return
        if job.attempts >= settings.memory_worker_max_attempts:
            job.status = "failed"
            job.last_error = "Maximum retry attempts exceeded."
            await db.commit()
            return

        user = await db.get(User, job.user_id)
        if user is None:
            job.status = "failed"
            job.last_error = "User no longer exists."
            await db.commit()
            return

        job.status = "running"
        job.attempts += 1
        await db.commit()

        try:
            turns = await load_unsummarized_turns(db, job)
            if len(turns) < 2:
                job.status = "succeeded"
                await db.commit()
                return

            transcript = format_transcript(turns)
            summary = await summarize_transcript(rag_service, transcript)
            topics = extract_topics(summary)
            source_turn_start = min(turn.sequence for turn in turns)
            source_turn_end = max(turn.sequence for turn in turns)
            memory = await memory_service.store_summary_memory(
                db,
                user=user,
                session_id=job.session_id,
                text=summary,
                topics=topics,
                source_turn_start=source_turn_start,
                source_turn_end=source_turn_end,
                rag_service=rag_service,
            )
            async with async_session() as status_db:
                status_job = await status_db.get(MemoryJob, job.id)
                if status_job is not None:
                    status_job.status = "succeeded" if memory is not None else "failed"
                    status_job.last_error = None if memory is not None else "Duplicate memory summary."
                    await status_db.commit()
            LOGGER.info("Processed memory job %s session=%s", job.id, job.session_id)
        except Exception as exc:
            LOGGER.exception("Memory job %s failed: %s", job.id, exc)
            await mark_failed_or_retry(job.id, str(exc))


async def load_unsummarized_turns(db, job: MemoryJob) -> list[ConversationTurn]:
    result = await db.execute(
        select(ConversationTurn)
        .where(ConversationTurn.user_id == job.user_id)
        .where(ConversationTurn.session_id == job.session_id)
        .where(ConversationTurn.summary_id.is_(None))
        .order_by(ConversationTurn.sequence.asc())
        .limit(24)
    )
    return list(result.scalars().all())


async def mark_failed_or_retry(job_id: uuid.UUID, message: str) -> None:
    settings = get_settings()
    async_session = get_sessionmaker()
    async with async_session() as db:
        job = await db.get(MemoryJob, job_id)
        if job is None:
            return
        job.last_error = message[:2000]
        if job.attempts >= settings.memory_worker_max_attempts:
            job.status = "failed"
        else:
            job.status = "queued"
            await get_redis().rpush(settings.memory_job_queue, str(job.id))
        await db.commit()


async def summarize_transcript(rag_service: RagService, transcript: str) -> str:
    prompt = (
        "Summarize this chat window as durable assistant memory. Preserve stable facts, "
        "preferences, decisions, and unresolved follow-ups. Exclude secrets, credentials, "
        "tokens, raw private data, and temporary small talk. Return 3-6 concise bullets.\n\n"
        f"{transcript}"
    )
    payload = {
        "model": rag_service.settings.ollama_model,
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": "You compress conversation history into safe long-term memory.",
            },
            {"role": "user", "content": prompt},
        ],
        "options": {
            "temperature": 0.1,
            "num_ctx": min(rag_service.settings.ollama_num_ctx, 4096),
            "num_predict": 220,
        },
    }
    if rag_service.settings.ollama_keep_alive:
        payload["keep_alive"] = rag_service.settings.ollama_keep_alive

    try:
        response = await rag_service._ensure_async_client().post(
            f"{rag_service.model_runner_base_url}/api/chat",
            json=payload,
            timeout=min(90.0, rag_service.settings.ollama_timeout_seconds),
        )
        response.raise_for_status()
        content = response.json().get("message", {}).get("content", "").strip()
        if content:
            return sanitize_memory_text(content)
    except (httpx.HTTPError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        LOGGER.warning("LLM summarization failed, using extractive fallback: %s", exc)
    return sanitize_memory_text(extractive_summary(transcript))


def format_transcript(turns: list[ConversationTurn]) -> str:
    return "\n".join(f"{turn.sequence}. {turn.role}: {turn.content}" for turn in turns)


def extractive_summary(transcript: str) -> str:
    lines = [line.strip() for line in transcript.splitlines() if line.strip()]
    selected = lines[-8:]
    return "\n".join(f"- {line[:320]}" for line in selected)


def sanitize_memory_text(text: str) -> str:
    patterns = [
        r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+",
        r"(?i)bearer\s+[a-z0-9._\-]+",
        r"(?i)sk-[a-z0-9]{20,}",
    ]
    sanitized = text
    for pattern in patterns:
        sanitized = re.sub(pattern, "[redacted secret]", sanitized)
    return sanitized.strip()[:2400]


def extract_topics(summary: str) -> list[str]:
    candidates = re.findall(r"\b[A-Za-z][A-Za-z0-9_-]{3,24}\b", summary.lower())
    stop = {
        "this",
        "that",
        "with",
        "from",
        "into",
        "user",
        "assistant",
        "memory",
        "summary",
        "policy",
    }
    topics: list[str] = []
    for candidate in candidates:
        if candidate in stop or candidate in topics:
            continue
        topics.append(candidate)
        if len(topics) >= 8:
            break
    return topics


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

