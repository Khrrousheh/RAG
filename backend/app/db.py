from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings


_ENGINE: AsyncEngine | None = None
_SESSIONMAKER: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _ENGINE
    if _ENGINE is None:
        settings = get_settings()
        _ENGINE = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
    return _ENGINE


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _SESSIONMAKER
    if _SESSIONMAKER is None:
        _SESSIONMAKER = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _SESSIONMAKER


async def get_db_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


async def check_db() -> str:
    try:
        async with get_sessionmaker()() as session:
            await session.execute(text("select 1"))
        return "ok"
    except Exception:
        return "error"


async def dispose_db() -> None:
    global _ENGINE, _SESSIONMAKER
    if _ENGINE is not None:
        await _ENGINE.dispose()
    _ENGINE = None
    _SESSIONMAKER = None

