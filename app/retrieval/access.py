"""Local single-user boundary. Replace this dependency when introducing real ACLs."""

import hashlib
import hmac
import ipaddress
import secrets
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response

from app.core.config import settings

COOKIE = "a2_session"


def _signature(value):
    return hmac.new(
        settings.minio_secret_key.encode(), ("a2-session:" + value).encode(), hashlib.sha256
    ).hexdigest()


async def local_owner(request: Request, response: Response):
    try:
        loopback = request.client and ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        loopback = False
    if (
        settings.app_env not in ("local", "test")
        or not loopback
        or request.url.hostname not in ("localhost", "127.0.0.1", "::1")
        or any(h in request.headers for h in ("forwarded", "x-forwarded-for", "x-forwarded-host"))
    ):
        raise HTTPException(403, "检索实验仅支持本机直连；多用户权限尚未接入")
    origin = request.headers.get("origin")
    if (
        (origin and urlsplit(origin).netloc != request.url.netloc)
        or (origin and urlsplit(origin).scheme != request.url.scheme)
        or request.headers.get("sec-fetch-site") == "cross-site"
    ):
        raise HTTPException(403, "检索实验不接受跨站访问")
    token = request.cookies.get(COOKIE, "")
    valid = False
    try:
        nonce, issued, signature = token.split(".")
        valid = (
            len(nonce) == 64
            and 0 <= time.time() - int(issued) <= 7200
            and hmac.compare_digest(signature, _signature(nonce + "." + issued))
        )
    except (ValueError, TypeError):
        pass
    if not valid:
        value = secrets.token_hex(32) + "." + str(int(time.time()))
        token = value + "." + _signature(value)
    response.set_cookie(
        COOKIE,
        token,
        max_age=7200,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/api/v1/retrieval-experiments",
    )
    response.headers["Cache-Control"] = "no-store"
    return hashlib.sha256(token.encode()).hexdigest()
