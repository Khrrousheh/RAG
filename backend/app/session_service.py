from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ChatSession, ConversationTurn, User
from .schemas import ChatSessionResponse, ConversationTurnResponse, SessionMessagesResponse


class SessionService:
    async def list_sessions(self, db: AsyncSession, user: User) -> list[ChatSessionResponse]:
        result = await db.execute(
            select(ChatSession)
            .where(ChatSession.user_id == user.id)
            .where(ChatSession.status == "active")
            .order_by(ChatSession.last_message_at.desc().nullslast(), ChatSession.created_at.desc())
        )
        return [self.to_response(session) for session in result.scalars().all()]

    async def create_session(
        self,
        db: AsyncSession,
        user: User,
        *,
        title: str | None = None,
    ) -> ChatSession:
        session = ChatSession(
            user_id=user.id,
            tenant_id=user.tenant_id,
            title=self.clean_title(title) if title else "New chat",
        )
        db.add(session)
        await db.flush()
        await db.commit()
        await db.refresh(session)
        return session

    async def get_session(self, db: AsyncSession, user: User, session_id: uuid.UUID) -> ChatSession:
        result = await db.execute(
            select(ChatSession)
            .where(ChatSession.id == session_id)
            .where(ChatSession.user_id == user.id)
            .where(ChatSession.status == "active")
        )
        session = result.scalar_one_or_none()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found.")
        return session

    async def get_or_create_session(
        self,
        db: AsyncSession,
        user: User,
        session_id: uuid.UUID | None,
        message: str,
    ) -> ChatSession:
        if session_id is not None:
            return await self.get_session(db, user, session_id)

        result = await db.execute(
            select(ChatSession)
            .where(ChatSession.user_id == user.id)
            .where(ChatSession.status == "active")
            .order_by(ChatSession.last_message_at.desc().nullslast(), ChatSession.created_at.desc())
            .limit(1)
        )
        session = result.scalar_one_or_none()
        if session is not None:
            return session

        title = self.title_from_message(message)
        session = ChatSession(user_id=user.id, tenant_id=user.tenant_id, title=title)
        db.add(session)
        await db.flush()
        return session

    async def update_title(
        self,
        db: AsyncSession,
        user: User,
        session_id: uuid.UUID,
        title: str,
    ) -> ChatSession:
        session = await self.get_session(db, user, session_id)
        session.title = self.clean_title(title)
        session.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(session)
        return session

    async def soft_delete(self, db: AsyncSession, user: User, session_id: uuid.UUID) -> ChatSession:
        session = await self.get_session(db, user, session_id)
        now = datetime.now(UTC)
        session.status = "deleted"
        session.deleted_at = now
        session.updated_at = now
        await db.commit()
        await db.refresh(session)
        return session

    async def append_turn(
        self,
        db: AsyncSession,
        user: User,
        session: ChatSession,
        *,
        role: str,
        content: str,
        sources: list[dict[str, object]] | None = None,
        warnings: list[str] | None = None,
        metrics: dict[str, object] | None = None,
        token_count: int | None = None,
    ) -> ConversationTurn:
        result = await db.execute(
            select(func.coalesce(func.max(ConversationTurn.sequence), 0)).where(
                ConversationTurn.session_id == session.id
            )
        )
        next_sequence = int(result.scalar_one()) + 1
        turn = ConversationTurn(
            user_id=user.id,
            session_id=session.id,
            sequence=next_sequence,
            role=role,
            content=content,
            sources=sources,
            warnings=warnings,
            metrics=metrics,
            token_count=token_count,
        )
        session.last_message_at = datetime.now(UTC)
        if session.title == "New chat" and role == "user":
            session.title = self.title_from_message(content)
        db.add(turn)
        await db.flush()
        return turn

    async def get_messages(
        self,
        db: AsyncSession,
        user: User,
        session_id: uuid.UUID,
        *,
        limit: int,
        before_sequence: int | None,
    ) -> SessionMessagesResponse:
        session = await self.get_session(db, user, session_id)
        query = (
            select(ConversationTurn)
            .where(ConversationTurn.session_id == session.id)
            .order_by(ConversationTurn.sequence.desc())
            .limit(limit)
        )
        if before_sequence is not None:
            query = query.where(ConversationTurn.sequence < before_sequence)

        result = await db.execute(query)
        messages = list(reversed(result.scalars().all()))
        return SessionMessagesResponse(
            session=self.to_response(session),
            messages=[self.turn_to_response(turn) for turn in messages],
        )

    async def recent_turns(
        self,
        db: AsyncSession,
        user: User,
        session_id: uuid.UUID,
        *,
        limit: int,
    ) -> list[ConversationTurn]:
        await self.get_session(db, user, session_id)
        result = await db.execute(
            select(ConversationTurn)
            .where(ConversationTurn.session_id == session_id)
            .order_by(ConversationTurn.sequence.desc())
            .limit(limit)
        )
        return list(reversed(result.scalars().all()))

    @staticmethod
    def to_response(session: ChatSession) -> ChatSessionResponse:
        return ChatSessionResponse(
            id=session.id,
            title=session.title,
            status=session.status,
            created_at=session.created_at,
            updated_at=session.updated_at,
            last_message_at=session.last_message_at,
        )

    @staticmethod
    def turn_to_response(turn: ConversationTurn) -> ConversationTurnResponse:
        return ConversationTurnResponse(
            id=turn.id,
            session_id=turn.session_id,
            sequence=turn.sequence,
            role=turn.role,
            content=turn.content,
            sources=turn.sources,
            warnings=turn.warnings,
            metrics=turn.metrics,
            created_at=turn.created_at,
        )

    @staticmethod
    def title_from_message(message: str) -> str:
        title = " ".join(message.strip().split())
        if len(title) > 64:
            title = title[:64].rsplit(" ", 1)[0].rstrip()
        return title or "New chat"

    @staticmethod
    def clean_title(title: str | None) -> str:
        cleaned = " ".join((title or "").strip().split())
        return cleaned[:180] or "New chat"


def get_session_service() -> SessionService:
    return SessionService()

