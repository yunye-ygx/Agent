"""Opaque server-side sessions. Only SHA-256 digests are stored."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from sqlalchemy import select, update

from app.business.models import AuthSession, BusinessUser
from app.business.utils import as_utc, new_id, utc_now

SESSION_COOKIE = "business_session"
CSRF_COOKIE = "business_csrf"
SESSION_TTL = timedelta(hours=8)
SESSION_MAX_AGE_SECONDS = int(SESSION_TTL.total_seconds())


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def create_session(session, user_id: str):
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    record = AuthSession(
        id=new_id(),
        user_id=user_id,
        token_hash=digest(token),
        csrf_token_hash=digest(csrf_token),
        expires_at=utc_now() + SESSION_TTL,
    )
    session.add(record)
    return record, token, csrf_token


async def load_session(session, token: str):
    """Return (AuthSession, BusinessUser) for a live session, otherwise None."""

    if not token:
        return None
    result = await session.execute(
        select(AuthSession, BusinessUser)
        .join(BusinessUser, BusinessUser.id == AuthSession.user_id)
        .where(
            AuthSession.token_hash == digest(token),
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > utc_now(),
        )
    )
    row = result.first()
    if row is None:
        return None
    record, user = row
    if user.status != "active":
        return None
    expires_at = as_utc(record.expires_at)
    if expires_at is None or expires_at <= utc_now():
        return None
    return record, user


async def revoke_session(session, record: AuthSession) -> None:
    record.revoked_at = utc_now()


async def revoke_expired(session, now=None) -> int:
    moment = now or utc_now()
    result = await session.execute(
        update(AuthSession)
        .where(AuthSession.revoked_at.is_(None), AuthSession.expires_at <= moment)
        .values(revoked_at=moment)
    )
    return result.rowcount or 0


def verify_csrf(record: AuthSession, cookie_value: str, header_value: str) -> bool:
    if not cookie_value or not header_value:
        return False
    if not hmac.compare_digest(cookie_value, header_value):
        return False
    return hmac.compare_digest(record.csrf_token_hash, digest(cookie_value))
