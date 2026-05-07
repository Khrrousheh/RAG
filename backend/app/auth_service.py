from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .db import get_db_session
from .models import RefreshToken, User
from .redis_client import get_redis
from .schemas import AuthResponse
from .security import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    hash_password,
    hash_token,
    normalize_email,
    verify_password,
)


bearer_scheme = HTTPBearer(auto_error=False)


class AuthService:
    def __init__(self, settings: Settings, redis: Redis) -> None:
        self.settings = settings
        self.redis = redis

    async def register(
        self,
        db: AsyncSession,
        *,
        email: str,
        password: str,
        display_name: str | None,
    ) -> tuple[AuthResponse, str]:
        user = User(
            email=normalize_email(email),
            password_hash=hash_password(password),
            display_name=display_name.strip() if display_name else None,
        )
        db.add(user)
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this email already exists.",
            ) from exc

        response, refresh_token, _ = await self._issue_token_pair(db, user)
        await db.commit()
        return response, refresh_token

    async def login(
        self,
        db: AsyncSession,
        request: Request,
        *,
        email: str,
        password: str,
    ) -> tuple[AuthResponse, str]:
        await self._check_login_rate_limit(request, email)
        normalized_email = normalize_email(email)
        result = await db.execute(select(User).where(User.email == normalized_email))
        user = result.scalar_one_or_none()
        if user is None or not user.is_active or not verify_password(password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password.",
            )

        user.last_login_at = datetime.now(UTC)
        response, refresh_token, _ = await self._issue_token_pair(db, user)
        await db.commit()
        await self._clear_login_rate_limit(request, email)
        return response, refresh_token

    async def refresh(self, db: AsyncSession, refresh_token: str | None) -> tuple[AuthResponse, str]:
        if not refresh_token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing refresh token.")

        token_hash = hash_token(refresh_token, self.settings.jwt_refresh_secret)
        result = await db.execute(
            select(RefreshToken, User)
            .join(User, RefreshToken.user_id == User.id)
            .where(RefreshToken.token_hash == token_hash)
        )
        row = result.one_or_none()
        now = datetime.now(UTC)
        if row is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token.")

        token_row, user = row
        if token_row.revoked_at is not None or token_row.expires_at <= now or not user.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token.")

        token_row.revoked_at = now
        response, new_refresh_token, new_refresh_token_id = await self._issue_token_pair(
            db,
            user,
            family_id=token_row.family_id,
        )
        token_row.replaced_by_token_id = new_refresh_token_id
        await db.commit()
        return response, new_refresh_token

    async def logout(self, db: AsyncSession, refresh_token: str | None) -> None:
        if refresh_token:
            await db.execute(
                update(RefreshToken)
                .where(RefreshToken.token_hash == hash_token(refresh_token, self.settings.jwt_refresh_secret))
                .where(RefreshToken.revoked_at.is_(None))
                .values(revoked_at=datetime.now(UTC))
            )
            await db.commit()

    async def _issue_token_pair(
        self,
        db: AsyncSession,
        user: User,
        *,
        family_id: uuid.UUID | None = None,
    ) -> tuple[AuthResponse, str, uuid.UUID]:
        access_token, expires_at = create_access_token(
            settings=self.settings,
            user_id=user.id,
            email=user.email,
            role=user.role,
            tenant_id=user.tenant_id,
        )
        refresh_token, refresh_jti = create_refresh_token()
        refresh_row = RefreshToken(
            user_id=user.id,
            jti=refresh_jti,
            token_hash=hash_token(refresh_token, self.settings.jwt_refresh_secret),
            family_id=family_id or uuid.uuid4(),
            expires_at=datetime.now(UTC) + timedelta(days=self.settings.refresh_token_ttl_days),
        )
        db.add(refresh_row)
        await db.flush()
        return (
            AuthResponse(
                access_token=access_token,
                expires_at=expires_at,
                user=user,
            ),
            refresh_token,
            refresh_row.id,
        )

    async def _check_login_rate_limit(self, request: Request, email: str) -> None:
        key = self._login_rate_key(request, email)
        try:
            attempts = await self.redis.incr(key)
            if attempts == 1:
                await self.redis.expire(key, self.settings.auth_login_rate_window_seconds)
            if attempts > self.settings.auth_login_rate_limit:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many login attempts. Try again later.",
                )
        except HTTPException:
            raise
        except Exception:
            return

    async def _clear_login_rate_limit(self, request: Request, email: str) -> None:
        try:
            await self.redis.delete(self._login_rate_key(request, email))
        except Exception:
            return

    @staticmethod
    def _login_rate_key(request: Request, email: str) -> str:
        client_host = request.client.host if request.client else "unknown"
        return f"auth:login:{normalize_email(email)}:{client_host}"


def get_auth_service(
    settings: Settings = Depends(get_settings),
    redis: Redis = Depends(get_redis),
) -> AuthService:
    return AuthService(settings=settings, redis=redis)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing access token.")
    try:
        payload = decode_access_token(credentials.credentials, settings)
        user_id = uuid.UUID(str(payload["sub"]))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid access token.") from exc

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Inactive or missing user.")
    return user
