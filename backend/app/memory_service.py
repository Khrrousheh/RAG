from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import anyio
from qdrant_client import QdrantClient, models as qdrant_models
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .models import ConversationTurn, LongTermMemory, MemoryJob, User
from .rag import RagService
from .redis_client import get_redis
from .schemas import ChatTurn


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemoryHit:
    id: str
    text: str
    score: float
    kind: str
    topics: list[str]
    session_id: uuid.UUID | None
    created_at: str | None


class MemoryService:
    def __init__(self, settings: Settings, redis: Redis, qdrant: QdrantClient) -> None:
        self.settings = settings
        self.redis = redis
        self.qdrant = qdrant

    def short_term_key(self, user_id: uuid.UUID, session_id: uuid.UUID) -> str:
        return f"stm:{user_id}:{session_id}:turns"

    async def load_short_term(
        self,
        db: AsyncSession,
        *,
        user: User,
        session_id: uuid.UUID,
    ) -> list[ChatTurn]:
        key = self.short_term_key(user.id, session_id)
        try:
            cached_rows = await self.redis.lrange(key, 0, -1)
        except Exception:
            cached_rows = []

        turns: list[ChatTurn] = []
        for row in cached_rows:
            try:
                payload = json.loads(row)
                turns.append(ChatTurn(role=payload["role"], content=payload["content"]))
            except Exception:
                continue

        if turns:
            return turns

        result = await db.execute(
            select(ConversationTurn)
            .where(ConversationTurn.user_id == user.id)
            .where(ConversationTurn.session_id == session_id)
            .order_by(ConversationTurn.sequence.desc())
            .limit(self.settings.short_term_memory_turns)
        )
        hydrated = list(reversed(result.scalars().all()))
        turns = [ChatTurn(role=turn.role, content=turn.content) for turn in hydrated]
        if turns:
            await self.append_short_term(user_id=user.id, session_id=session_id, turns=turns)
        return turns

    async def append_short_term(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        turns: list[ChatTurn],
    ) -> None:
        if not turns:
            return
        key = self.short_term_key(user_id, session_id)
        rows = [
            json.dumps({"role": turn.role, "content": turn.content}, separators=(",", ":"))
            for turn in turns
        ]
        try:
            await self.redis.rpush(key, *rows)
            await self.redis.ltrim(key, -self.settings.short_term_memory_turns, -1)
            await self.redis.expire(key, self.settings.short_term_memory_ttl_seconds)
        except Exception as exc:
            LOGGER.debug("Short-term memory update failed: %s", exc)

    async def delete_short_term(self, *, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
        try:
            await self.redis.delete(self.short_term_key(user_id, session_id))
        except Exception:
            return

    async def retrieve_long_term(
        self,
        db: AsyncSession,
        *,
        user: User,
        session_id: uuid.UUID,
        query: str,
        rag_service: RagService,
    ) -> list[MemoryHit]:
        if self.settings.long_term_memory_top_k <= 0:
            return []

        query_vector, _, _ = await anyio.to_thread.run_sync(rag_service._embed_query_cached, query)
        await self.ensure_memory_collection(len(query_vector))
        hits = await anyio.to_thread.run_sync(
            self._search_long_term,
            user.id,
            user.tenant_id,
            session_id,
            query_vector,
        )
        if hits:
            await db.execute(
                update(LongTermMemory)
                .where(LongTermMemory.qdrant_point_id.in_([hit.id for hit in hits]))
                .values(
                    last_accessed_at=datetime.now(UTC),
                    access_count=LongTermMemory.access_count + 1,
                )
            )
        return hits

    def _search_long_term(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
        session_id: uuid.UUID,
        query_vector: list[float],
    ) -> list[MemoryHit]:
        conditions: list[qdrant_models.Condition] = [
            qdrant_models.FieldCondition(
                key="user_id",
                match=qdrant_models.MatchValue(value=str(user_id)),
            )
        ]
        if tenant_id is not None:
            conditions.append(
                qdrant_models.FieldCondition(
                    key="tenant_id",
                    match=qdrant_models.MatchValue(value=str(tenant_id)),
                )
            )

        try:
            results = self.qdrant.search(
                collection_name=self.settings.qdrant_memory_collection,
                query_vector=query_vector,
                query_filter=qdrant_models.Filter(must=conditions),
                limit=max(self.settings.long_term_memory_top_k * 3, self.settings.long_term_memory_top_k),
                with_payload=True,
                with_vectors=False,
            )
        except Exception:
            return []

        ranked: list[tuple[float, MemoryHit]] = []
        seen: set[str] = set()
        now = datetime.now(UTC)
        for point in results:
            payload = point.payload or {}
            text = str(payload.get("text", "")).strip()
            normalized = normalize_memory_text(text)
            if not text or normalized in seen:
                continue
            seen.add(normalized)

            payload_session_id = parse_uuid(payload.get("session_id"))
            created_at_raw = payload.get("created_at")
            recency_boost = recency_score(created_at_raw, now)
            same_session_boost = 0.08 if payload_session_id == session_id else 0.0
            importance = float(payload.get("importance") or 0.5)
            semantic = float(point.score)
            combined = semantic + recency_boost + same_session_boost + (importance * 0.05)
            ranked.append(
                (
                    combined,
                    MemoryHit(
                        id=str(point.id),
                        text=text,
                        score=round(combined, 4),
                        kind=str(payload.get("kind") or "summary"),
                        topics=[str(topic) for topic in payload.get("topics") or []],
                        session_id=payload_session_id,
                        created_at=str(created_at_raw) if created_at_raw else None,
                    ),
                )
            )

        ranked.sort(key=lambda item: item[0], reverse=True)
        return [hit for _, hit in ranked[: self.settings.long_term_memory_top_k]]

    async def ensure_memory_collection(self, vector_size: int) -> None:
        await anyio.to_thread.run_sync(self._ensure_memory_collection_sync, vector_size)

    def _ensure_memory_collection_sync(self, vector_size: int) -> None:
        collection = self.settings.qdrant_memory_collection
        if not self.qdrant.collection_exists(collection):
            self.qdrant.create_collection(
                collection_name=collection,
                vectors_config=qdrant_models.VectorParams(
                    size=vector_size,
                    distance=qdrant_models.Distance.COSINE,
                ),
            )
        index_specs = {
            "user_id": qdrant_models.PayloadSchemaType.KEYWORD,
            "tenant_id": qdrant_models.PayloadSchemaType.KEYWORD,
            "session_id": qdrant_models.PayloadSchemaType.KEYWORD,
            "kind": qdrant_models.PayloadSchemaType.KEYWORD,
            "topics": qdrant_models.PayloadSchemaType.KEYWORD,
            "created_at": qdrant_models.PayloadSchemaType.DATETIME,
        }
        for field_name, field_schema in index_specs.items():
            try:
                self.qdrant.create_payload_index(
                    collection_name=collection,
                    field_name=field_name,
                    field_schema=field_schema,
                    wait=True,
                )
            except Exception as exc:
                if "already exists" not in str(exc).lower() and "same params" not in str(exc).lower():
                    raise

    async def enqueue_summary_if_needed(
        self,
        db: AsyncSession,
        *,
        user: User,
        session_id: uuid.UUID,
    ) -> bool:
        stats = await db.execute(
            select(
                func.count(ConversationTurn.id),
                func.coalesce(func.sum(func.length(ConversationTurn.content)), 0),
            )
            .where(ConversationTurn.user_id == user.id)
            .where(ConversationTurn.session_id == session_id)
            .where(ConversationTurn.summary_id.is_(None))
        )
        turn_count, char_count = stats.one()
        if (
            int(turn_count) < self.settings.memory_summary_turn_threshold
            and int(char_count) < self.settings.memory_summary_char_threshold
        ):
            return False

        existing = await db.execute(
            select(MemoryJob.id)
            .where(MemoryJob.user_id == user.id)
            .where(MemoryJob.session_id == session_id)
            .where(MemoryJob.status.in_(["queued", "running"]))
            .limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            return False

        job = MemoryJob(user_id=user.id, session_id=session_id)
        db.add(job)
        await db.flush()
        await db.commit()
        try:
            await self.redis.rpush(self.settings.memory_job_queue, str(job.id))
        except Exception as exc:
            job.status = "failed"
            job.last_error = f"Redis enqueue failed: {exc}"
            await db.commit()
            return False
        return True

    async def delete_long_term_for_session(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
        session_id: uuid.UUID,
    ) -> None:
        conditions: list[qdrant_models.Condition] = [
            qdrant_models.FieldCondition(
                key="user_id",
                match=qdrant_models.MatchValue(value=str(user_id)),
            ),
            qdrant_models.FieldCondition(
                key="session_id",
                match=qdrant_models.MatchValue(value=str(session_id)),
            ),
        ]
        if tenant_id is not None:
            conditions.append(
                qdrant_models.FieldCondition(
                    key="tenant_id",
                    match=qdrant_models.MatchValue(value=str(tenant_id)),
                )
            )

        try:
            await anyio.to_thread.run_sync(
                lambda: self.qdrant.delete(
                    collection_name=self.settings.qdrant_memory_collection,
                    points_selector=qdrant_models.FilterSelector(
                        filter=qdrant_models.Filter(must=conditions)
                    ),
                )
            )
        except Exception:
            return

    async def store_summary_memory(
        self,
        db: AsyncSession,
        *,
        user: User,
        session_id: uuid.UUID,
        text: str,
        topics: list[str],
        source_turn_start: int,
        source_turn_end: int,
        rag_service: RagService,
    ) -> LongTermMemory | None:
        normalized = normalize_memory_text(text)
        content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{user.id}:{session_id}:{content_hash}"))
        vector, _, _ = await anyio.to_thread.run_sync(rag_service._embed_query_cached, text)
        await self.ensure_memory_collection(len(vector))
        payload = {
            "user_id": str(user.id),
            "tenant_id": str(user.tenant_id) if user.tenant_id else None,
            "session_id": str(session_id),
            "kind": "summary",
            "text": text,
            "topics": topics,
            "importance": 0.65,
            "content_hash": content_hash,
            "created_at": datetime.now(UTC).isoformat(),
            "source_turn_start": source_turn_start,
            "source_turn_end": source_turn_end,
        }
        await anyio.to_thread.run_sync(
            lambda: self.qdrant.upsert(
                collection_name=self.settings.qdrant_memory_collection,
                points=[
                    qdrant_models.PointStruct(
                        id=point_id,
                        vector=vector,
                        payload=payload,
                    )
                ],
                wait=True,
            )
        )
        row = LongTermMemory(
            user_id=user.id,
            tenant_id=user.tenant_id,
            session_id=session_id,
            qdrant_point_id=point_id,
            kind="summary",
            text=text,
            topics=topics,
            importance=0.65,
            content_hash=content_hash,
            source_turn_start=source_turn_start,
            source_turn_end=source_turn_end,
        )
        db.add(row)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            return None
        await db.execute(
            update(ConversationTurn)
            .where(ConversationTurn.user_id == user.id)
            .where(ConversationTurn.session_id == session_id)
            .where(ConversationTurn.sequence >= source_turn_start)
            .where(ConversationTurn.sequence <= source_turn_end)
            .values(summary_id=row.id)
        )
        await db.commit()
        return row


def get_memory_service(
    settings: Settings | None = None,
    redis: Redis | None = None,
    qdrant: QdrantClient | None = None,
) -> MemoryService:
    settings = settings or get_settings()
    return MemoryService(
        settings=settings,
        redis=redis or get_redis(),
        qdrant=qdrant or QdrantClient(url=settings.qdrant_url, check_compatibility=False),
    )


def normalize_memory_text(text: str) -> str:
    value = text.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def parse_uuid(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def recency_score(value: Any, now: datetime) -> float:
    if not value:
        return 0.0
    try:
        created_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
    except ValueError:
        return 0.0
    age_days = max(0.0, (now - created_at).total_seconds() / 86400)
    return max(0.0, 0.12 - min(age_days, 60.0) * 0.002)
