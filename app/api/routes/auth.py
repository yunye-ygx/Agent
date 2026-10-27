"""Authentication routes. Login/registration accept JSON only and verify Origin."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import rate_limits
from app.auth.dependencies import (
    AuthContext,
    ensure_secure_transport,
    ensure_trusted_origin,
    get_auth_context,
    require_csrf,
)
from app.auth.passwords import dummy_hash, verify_password_async
from app.auth.schemas import LoginRequest, RegisterRequest, UserResponse, normalize_username
from app.auth.sessions import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    create_session,
    revoke_session,
)
from app.auth.users import create_user, find_by_username, public_user
from app.business.errors import Conflict
from app.business.utils import utc_now
from app.db.mysql_session import get_mysql_session

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _cookie_secure(request: Request) -> bool:
    return request.url.scheme == "https"


def _set_session_cookies(request: Request, response: Response, token: str, csrf: str) -> None:
    secure = _cookie_secure(request)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    # Deliberately readable by the same-origin page; the server still requires the
    # header copy to match both the cookie and the stored session digest.
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=False,
        samesite="lax",
        secure=secure,
        path="/",
    )


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _rate_limited() -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={"code": "RATE_LIMITED", "message": "尝试过于频繁，请稍后再试"},
    )


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_mysql_session),
) -> UserResponse:
    ensure_secure_transport(request)
    ensure_trusted_origin(request)
    _no_store(response)
    source = rate_limits.source_digest(request.client.host if request.client else None)
    if not await rate_limits.consume(
        session, rate_limits.register_source_key(source)
    ):
        raise _rate_limited()
    try:
        user = await create_user(
            session,
            username=payload.username,
            display_name=payload.display_name,
            password=payload.password,
            role="customer",
        )
    except Conflict as exc:
        raise HTTPException(409, detail=exc.to_detail()) from exc
    _, token, csrf = await create_session(session, user.id)
    await session.commit()
    _set_session_cookies(request, response, token, csrf)
    return UserResponse(**public_user(user))


@router.post("/login", response_model=UserResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_mysql_session),
) -> UserResponse:
    ensure_secure_transport(request)
    ensure_trusted_origin(request)
    _no_store(response)
    username = normalize_username(payload.username)
    source = rate_limits.source_digest(request.client.host if request.client else None)
    if not await rate_limits.consume(session, rate_limits.login_source_key(source)):
        raise _rate_limited()
    if await rate_limits.is_limited(session, rate_limits.login_account_key(username)):
        raise _rate_limited()

    user = await find_by_username(session, username)
    password_hash = user.password_hash if user is not None else dummy_hash()
    password_ok = await verify_password_async(password_hash, payload.password)
    if user is None or not password_ok or user.status != "active":
        allowed = await rate_limits.consume(session, rate_limits.login_account_key(username))
        if not allowed:
            raise _rate_limited()
        raise HTTPException(
            status_code=401,
            detail={"code": "INVALID_CREDENTIALS", "message": "用户名或密码不正确"},
        )

    await rate_limits.clear_bucket(session, rate_limits.login_account_key(username))
    _, token, csrf = await create_session(session, user.id)
    await session.commit()
    _set_session_cookies(request, response, token, csrf)
    return UserResponse(**public_user(user))


@router.get("/me", response_model=UserResponse)
async def me(
    response: Response,
    context: AuthContext = Depends(get_auth_context),
) -> UserResponse:
    _no_store(response)
    principal = context.principal
    return UserResponse(
        id=principal.user_id,
        username=principal.username,
        display_name=principal.display_name,
        role=principal.role,
        permissions=sorted(principal.permissions),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    session: AsyncSession = Depends(get_mysql_session),
    context: AuthContext = Depends(get_auth_context),
    _principal=Depends(require_csrf),
) -> Response:
    await revoke_session(session, context.record)
    await session.commit()
    _clear_session_cookies(response)
    _no_store(response)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})


@router.get("/session-info")
async def session_info(context: AuthContext = Depends(get_auth_context)):
    return {
        "user_id": context.principal.user_id,
        "expires_at": context.record.expires_at.isoformat(),
        "server_time": utc_now().isoformat(),
    }
