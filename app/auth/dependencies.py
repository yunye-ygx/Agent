"""FastAPI authentication/authorization dependencies.

Identity always comes from the server-side session cookie and database row.
Request bodies, query strings and tool arguments can never supply a user_id,
role or permission.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import check_username
from app.auth.sessions import CSRF_COOKIE, SESSION_COOKIE, load_session, verify_csrf
from app.business.models import AuthSession
from app.business.principal import Principal
from app.core.config import settings
from app.db.mysql_session import get_mysql_session


class AuthFailure(HTTPException):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(status_code=status_code, detail={"code": code, "message": message})


@dataclass(frozen=True, slots=True)
class AuthContext:
    principal: Principal
    record: AuthSession


def ensure_secure_transport(request: Request) -> None:
    if request.url.scheme != "https" and settings.app_env not in ("local", "test", "development"):
        raise AuthFailure(403, "HTTPS_REQUIRED", "非本机环境必须通过 HTTPS 访问认证接口")


def ensure_trusted_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        raise AuthFailure(403, "ORIGIN_REQUIRED", "缺少可信来源，浏览器写请求被拒绝")
    parsed = urlsplit(origin)
    if parsed.scheme != request.url.scheme or parsed.netloc != request.url.netloc:
        raise AuthFailure(403, "ORIGIN_REJECTED", "不接受跨站写请求")


async def get_auth_context(
    request: Request,
    session: AsyncSession = Depends(get_mysql_session),
) -> AuthContext:
    token = request.cookies.get(SESSION_COOKIE)
    loaded = await load_session(session, token or "")
    if loaded is None:
        raise AuthFailure(401, "UNAUTHENTICATED", "请先登录")
    record, user = loaded
    try:
        username = check_username(user.username)
    except ValueError as exc:
        raise AuthFailure(401, "UNAUTHENTICATED", "请先登录") from exc
    principal = Principal.for_user(
        user_id=user.id,
        username=username,
        display_name=user.display_name,
        role=user.role,
    )
    return AuthContext(principal=principal, record=record)


async def get_current_principal(
    context: AuthContext = Depends(get_auth_context),
) -> Principal:
    return context.principal


async def require_csrf(
    request: Request,
    context: AuthContext = Depends(get_auth_context),
) -> Principal:
    ensure_trusted_origin(request)
    cookie_value = request.cookies.get(CSRF_COOKIE, "")
    header_value = request.headers.get("x-csrf-token", "")
    if not verify_csrf(context.record, cookie_value, header_value):
        raise AuthFailure(403, "CSRF_INVALID", "CSRF 校验失败，请刷新页面后重试")
    return context.principal


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def require_csrf_for_write(
    request: Request,
    context: AuthContext = Depends(get_auth_context),
) -> Principal:
    """Router-level companion for admin routers that also exposes read endpoints."""

    if request.method in SAFE_METHODS:
        return context.principal
    return await require_csrf(request, context)


async def require_admin(
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    if not principal.is_admin:
        raise AuthFailure(403, "ADMIN_REQUIRED", "需要管理员权限")
    return principal
