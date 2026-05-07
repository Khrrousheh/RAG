from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from .auth_service import get_current_user
from .chat_service import ChatService
from .config import Settings, get_settings
from .db import get_db_session
from .memory_service import MemoryService
from .models import LongTermMemory, User
from .rag import RagService, get_rag_service
from .redis_client import get_redis
from .schemas import (
    ChatRequest,
    ChatResponse,
    ChatSessionResponse,
    SessionCreateRequest,
    SessionMessagesResponse,
    SessionUpdateRequest,
)
from .session_service import SessionService, get_session_service


router = APIRouter(prefix="/chat", tags=["chat"])


def get_chat_service(
    settings: Settings = Depends(get_settings),
    rag_service: RagService = Depends(get_rag_service),
    session_service: SessionService = Depends(get_session_service),
    redis: Redis = Depends(get_redis),
) -> ChatService:
    return ChatService(
        rag_service=rag_service,
        session_service=session_service,
        memory_service=MemoryService(settings=settings, redis=redis, qdrant=rag_service.qdrant),
    )


@router.get("/sessions", response_model=list[ChatSessionResponse])
async def list_sessions(
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
    sessions: SessionService = Depends(get_session_service),
) -> list[ChatSessionResponse]:
    return await sessions.list_sessions(db, user)


@router.post("/session", response_model=ChatSessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: SessionCreateRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
    sessions: SessionService = Depends(get_session_service),
) -> ChatSessionResponse:
    session = await sessions.create_session(db, user, title=payload.title)
    return sessions.to_response(session)


@router.get("/session/{session_id}", response_model=ChatSessionResponse)
async def get_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
    sessions: SessionService = Depends(get_session_service),
) -> ChatSessionResponse:
    return sessions.to_response(await sessions.get_session(db, user, session_id))


@router.patch("/session/{session_id}", response_model=ChatSessionResponse)
async def update_session(
    session_id: uuid.UUID,
    payload: SessionUpdateRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
    sessions: SessionService = Depends(get_session_service),
) -> ChatSessionResponse:
    session = await sessions.update_title(db, user, session_id, payload.title)
    return sessions.to_response(session)


@router.delete(
    "/session/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
async def delete_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
    chat: ChatService = Depends(get_chat_service),
    sessions: SessionService = Depends(get_session_service),
) -> None:
    session = await sessions.soft_delete(db, user, session_id)
    await db.execute(
        update(LongTermMemory)
        .where(LongTermMemory.user_id == user.id)
        .where(LongTermMemory.session_id == session.id)
        .where(LongTermMemory.deleted_at.is_(None))
        .values(deleted_at=datetime.now(UTC))
    )
    await db.commit()
    await chat.memory_service.delete_short_term(user_id=user.id, session_id=session.id)
    await chat.memory_service.delete_long_term_for_session(
        user_id=user.id,
        tenant_id=user.tenant_id,
        session_id=session.id,
    )


@router.get("/session/{session_id}/messages", response_model=SessionMessagesResponse)
async def get_messages(
    session_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    before_sequence: int | None = Query(default=None, ge=1),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
    sessions: SessionService = Depends(get_session_service),
) -> SessionMessagesResponse:
    return await sessions.get_messages(
        db,
        user,
        session_id,
        limit=limit,
        before_sequence=before_sequence,
    )


@router.post("/message", response_model=ChatResponse)
async def chat_message(
    request: ChatRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
    chat: ChatService = Depends(get_chat_service),
    settings: Settings = Depends(get_settings),
) -> ChatResponse:
    request.top_k = min(request.top_k, settings.max_top_k)
    return await chat.chat(db, user=user, request=request)


@router.post("", response_model=ChatResponse)
async def chat_compat(
    request: ChatRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
    chat: ChatService = Depends(get_chat_service),
    settings: Settings = Depends(get_settings),
) -> ChatResponse:
    request.top_k = min(request.top_k, settings.max_top_k)
    return await chat.chat(db, user=user, request=request)


@router.post("/stream")
async def chat_stream(
    request: ChatRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
    chat: ChatService = Depends(get_chat_service),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    request.top_k = min(request.top_k, settings.max_top_k)
    return StreamingResponse(
        chat.stream_chat(db, user=user, request=request),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
