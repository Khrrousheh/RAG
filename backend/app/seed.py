from __future__ import annotations

import logging

from sqlalchemy import select

from .config import Settings
from .db import get_sessionmaker
from .models import User
from .security import hash_password, normalize_email, verify_password


LOGGER = logging.getLogger(__name__)


async def seed_default_user(settings: Settings) -> None:
    if not settings.seed_default_user:
        return

    login = normalize_email(settings.default_user_login)
    async_session = get_sessionmaker()
    async with async_session() as db:
        result = await db.execute(select(User).where(User.email == login))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(
                email=login,
                password_hash=hash_password(settings.default_user_password),
                display_name=settings.default_user_display_name,
                role="user",
                is_active=True,
            )
            db.add(user)
            await db.commit()
            LOGGER.info("Seeded default user %s", login)
            return

        changed = False
        if not verify_password(settings.default_user_password, user.password_hash):
            user.password_hash = hash_password(settings.default_user_password)
            changed = True
        if not user.is_active:
            user.is_active = True
            changed = True
        if settings.default_user_display_name and user.display_name != settings.default_user_display_name:
            user.display_name = settings.default_user_display_name
            changed = True
        if changed:
            await db.commit()
            LOGGER.info("Updated default user %s", login)

