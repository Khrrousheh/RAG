from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from .config import Settings


_PASSWORD_HASHER = PasswordHasher()


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_password(password: str) -> str:
    return _PASSWORD_HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def hash_token(token: str, secret: str | None = None) -> str:
    if secret:
        return hmac.new(secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_access_token(
    *,
    settings: Settings,
    user_id: uuid.UUID,
    email: str,
    role: str,
    tenant_id: uuid.UUID | None,
) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=settings.access_token_ttl_minutes)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "tenant_id": str(tenant_id) if tenant_id else None,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": secrets.token_urlsafe(16),
        "typ": "access",
    }
    token = jwt.encode(payload, settings.jwt_access_secret, algorithm=settings.jwt_algorithm)
    return token, expires_at


def decode_access_token(token: str, settings: Settings) -> dict[str, Any]:
    payload = jwt.decode(token, settings.jwt_access_secret, algorithms=[settings.jwt_algorithm])
    if payload.get("typ") != "access":
        raise jwt.InvalidTokenError("Invalid token type")
    return payload


def create_refresh_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(48)
    jti = secrets.token_urlsafe(24)
    return token, jti
