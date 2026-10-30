"""Shared database rate-limit buckets with atomic increments."""

from __future__ import annotations

import hashlib
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.business.models import AuthRateLimit
from app.business.utils import utc_now
from app.core.config import settings

LOGIN_ACCOUNT_LIMIT = 10
LOGIN_ACCOUNT_WINDOW = timedelta(minutes=15)
LOGIN_SOURCE_LIMIT = 60
LOGIN_SOURCE_WINDOW = timedelta(minutes=15)
REGISTER_SOURCE_LIMIT = 10
REGISTER_SOURCE_WINDOW = timedelta(hours=1)


def source_digest(host: str | None) -> str:
    material = f"{settings.app_env}:{host or 'unknown'}".encode()
    return hashlib.sha256(material).hexdigest()[:32]


def login_account_key(username: str) -> str:
    return f"login:account:{username}"


def login_source_key(source: str) -> str:
    return f"login:source:{source}"


def register_source_key(source: str) -> str:
    return f"register:source:{source}"


async def is_limited(session, bucket_key: str) -> int | None:
    """Return the current count when the bucket is already at/over its limit."""

    record = await session.get(AuthRateLimit, bucket_key)
    if record is None:
        return None
    if record.expires_at and _aware(record.expires_at) <= utc_now():
        return None
    return record.attempt_count if record.attempt_count >= _limit_for(bucket_key) else None


def _limit_for(bucket_key: str) -> int:
    if bucket_key.startswith("login:account:"):
        return LOGIN_ACCOUNT_LIMIT
    if bucket_key.startswith("register:source:"):
        return REGISTER_SOURCE_LIMIT
    return LOGIN_SOURCE_LIMIT


def _window_for(bucket_key: str) -> timedelta:
    if bucket_key.startswith("login:account:"):
        return LOGIN_ACCOUNT_WINDOW
    if bucket_key.startswith("register:source:"):
        return REGISTER_SOURCE_WINDOW
    return LOGIN_SOURCE_WINDOW


def _aware(value):
    if value.tzinfo is None:
        from datetime import UTC

        return value.replace(tzinfo=UTC)
    return value


async def consume(session, bucket_key: str) -> bool:
    """Atomically count one attempt. False means the limit is exceeded.

    Existing rows are locked with SELECT ... FOR UPDATE, so separate processes
    increment the same bucket serially. A missing row may race on insert; the
    unique-key failure rolls back and retries against the now-existing row.
    """

    now = utc_now()
    for _ in range(3):
        try:
            record = (
                await session.execute(
                    select(AuthRateLimit)
                    .where(AuthRateLimit.bucket_key == bucket_key)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if record is None:
                session.add(
                    AuthRateLimit(
                        bucket_key=bucket_key,
                        window_started_at=now,
                        attempt_count=1,
                        expires_at=now + _window_for(bucket_key),
                    )
                )
                await session.commit()
                return 1 <= _limit_for(bucket_key)
            if _aware(record.expires_at) <= now:
                # A new window starts at 1; carrying the old count forward
                # would permanently block an account after one busy window.
                record.window_started_at = now
                record.attempt_count = 1
                record.expires_at = now + _window_for(bucket_key)
            else:
                record.attempt_count += 1
            await session.commit()
            return record.attempt_count <= _limit_for(bucket_key)
        except IntegrityError:
            await session.rollback()
            continue
    return False


async def clear_bucket(session, bucket_key: str) -> None:
    record = await session.get(AuthRateLimit, bucket_key)
    if record is not None:
        await session.delete(record)
