from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.api.errors import install_business_error_handler
from app.api.routes.auth import router
from app.auth.users import create_user
from app.business.models import AuthRateLimit, AuthSession, BusinessUser
from app.business.utils import utc_now
from app.db.mysql_session import get_mysql_session

ORIGIN = "http://testserver"
PASSWORD = "correct horse battery"


@asynccontextmanager
async def auth_client(db):
    app = FastAPI()
    install_business_error_handler(app)
    app.include_router(router)

    async def override_session():
        yield db

    app.dependency_overrides[get_mysql_session] = override_session
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        yield client


def origin_headers(**extra):
    return {"Origin": ORIGIN, **extra}


async def register(client, username="alice", display_name="爱丽丝", password=PASSWORD):
    return await client.post(
        "/api/v1/auth/register",
        json={"username": username, "display_name": display_name, "password": password},
        headers=origin_headers(),
    )


async def login(client, username="alice", password=PASSWORD):
    return await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
        headers=origin_headers(),
    )


def csrf_headers(client):
    return origin_headers(**{"X-CSRF-Token": client.cookies.get("business_csrf", "")})


@pytest.mark.asyncio
async def test_register_login_me_logout_flow(business_db):
    async with auth_client(business_db) as client:
        response = await register(client)
        assert response.status_code == 201
        assert response.json()["role"] == "customer"
        assert response.headers["cache-control"] == "no-store"
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert client.cookies.get("business_csrf")

        me = await client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["username"] == "alice"
        assert "orders:read" in me.json()["permissions"]

        logout = await client.post("/api/v1/auth/logout", headers=csrf_headers(client))
        assert logout.status_code == 204
        assert (await client.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_register_rejects_role_and_extra_fields(business_db):
    async with auth_client(business_db) as client:
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "username": "bob",
                "display_name": "鲍勃",
                "password": PASSWORD,
                "role": "admin",
            },
            headers=origin_headers(),
        )
        assert response.status_code == 422
        assert (
            await business_db.execute(
                select(BusinessUser).where(BusinessUser.username == "bob")
            )
        ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_duplicate_username_is_conflict(business_db):
    async with auth_client(business_db) as client:
        assert (await register(client)).status_code == 201
        again = await register(client)
        assert again.status_code == 409
        assert again.json()["detail"]["code"] == "USERNAME_TAKEN"
        upper = await register(client, username="ALICE ")
        assert upper.status_code == 409


@pytest.mark.asyncio
async def test_wrong_password_and_unknown_user_share_a_generic_error(business_db):
    async with auth_client(business_db) as client:
        await register(client)
        wrong = await login(client, password="wrong password 123")
        unknown = await login(client, username="nobody", password="wrong password 123")
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json()
        assert wrong.json()["detail"]["code"] == "INVALID_CREDENTIALS"


@pytest.mark.asyncio
async def test_disabled_user_and_expired_or_revoked_session_are_rejected(business_db):
    async with auth_client(business_db) as client:
        await register(client)
        token = client.cookies.get("business_session")
        user = (
            await business_db.execute(
                select(BusinessUser).where(BusinessUser.username == "alice")
            )
        ).scalar_one()
        user.status = "disabled"
        await business_db.commit()
        assert (await client.get("/api/v1/auth/me")).status_code == 401
        user.status = "active"
        await business_db.commit()

        session_row = (
            await business_db.execute(
                select(AuthSession).where(AuthSession.user_id == user.id)
            )
        ).scalars().first()
        session_row.expires_at = utc_now().replace(year=2020)
        await business_db.commit()
        replay = await client.get(
            "/api/v1/auth/me", headers={"Cookie": f"business_session={token}"}
        )
        assert replay.status_code == 401


@pytest.mark.asyncio
async def test_logout_revokes_old_cookie_and_requires_csrf(business_db):
    async with auth_client(business_db) as client:
        await register(client)
        token = client.cookies.get("business_session")
        no_csrf = await client.post("/api/v1/auth/logout", headers=origin_headers())
        assert no_csrf.status_code == 403
        cross_site = await client.post(
            "/api/v1/auth/logout",
            headers={
                "Origin": "https://evil.test",
                "X-CSRF-Token": client.cookies.get("business_csrf"),
            },
        )
        assert cross_site.status_code == 403
        assert (
            await client.post("/api/v1/auth/logout", headers=csrf_headers(client))
        ).status_code == 204
        replay = await client.get(
            "/api/v1/auth/me", headers={"Cookie": f"business_session={token}"}
        )
        assert replay.status_code == 401


@pytest.mark.asyncio
async def test_missing_origin_is_rejected_for_auth_writes(business_db):
    async with auth_client(business_db) as client:
        response = await client.post(
            "/api/v1/auth/register",
            json={"username": "carol", "display_name": "卡罗", "password": PASSWORD},
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "ORIGIN_REQUIRED"


@pytest.mark.asyncio
async def test_login_account_rate_limit_blocks_after_ten_failures(business_db):
    async with auth_client(business_db) as client:
        await register(client)
    async with auth_client(business_db) as client:
        for _ in range(10):
            response = await login(client, password="definitely wrong password")
            assert response.status_code == 401
        blocked = await login(client, password="definitely wrong password")
        assert blocked.status_code == 429
        assert blocked.json()["detail"]["code"] == "RATE_LIMITED"

        bucket = await business_db.get(AuthRateLimit, "login:account:alice")
        bucket.expires_at = utc_now().replace(year=2020)
        await business_db.commit()
        renewed = await login(client, password="definitely wrong password")
        assert renewed.status_code == 401


@pytest.mark.asyncio
async def test_password_policy_and_username_normalization(business_db):
    async with auth_client(business_db) as client:
        short = await register(client, username="Dave", password="short")
        assert short.status_code == 422
        response = await register(client, username="Dave", password=PASSWORD)
        assert response.status_code == 201
        assert response.json()["username"] == "dave"


@pytest.mark.asyncio
async def test_admin_comes_only_from_explicit_creation(business_db):
    async with auth_client(business_db) as client:
        await register(client)
    await create_user(
        business_db,
        username="root_admin",
        display_name="管理员",
        password=PASSWORD,
        role="admin",
    )
    roles = {
        row.username: row.role
        for row in (await business_db.execute(select(BusinessUser))).scalars()
    }
    assert roles == {"alice": "customer", "root_admin": "admin"}
