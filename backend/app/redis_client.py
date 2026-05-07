from __future__ import annotations

from redis.asyncio import Redis

from .config import get_settings


_REDIS: Redis | None = None


def get_redis() -> Redis:
    global _REDIS
    if _REDIS is None:
        _REDIS = Redis.from_url(
            get_settings().redis_url,
            decode_responses=True,
            health_check_interval=30,
        )
    return _REDIS


async def check_redis() -> str:
    try:
        await get_redis().ping()
        return "ok"
    except Exception:
        return "error"


async def close_redis() -> None:
    global _REDIS
    if _REDIS is not None:
        await _REDIS.aclose()
    _REDIS = None

