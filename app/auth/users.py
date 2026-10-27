from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.auth.passwords import hash_password_async
from app.auth.schemas import check_password, check_username
from app.business.errors import Conflict
from app.business.models import BusinessUser
from app.business.utils import utc_now


async def create_user(
    session, *, username: str, display_name: str, password: str, role: str = "customer"
) -> BusinessUser:
    username = check_username(username)
    check_password(password)
    display_name = display_name.strip()
    if not display_name:
        raise Conflict("显示名不能为空", code="INVALID_DISPLAY_NAME")
    existing = (
        await session.execute(select(BusinessUser.id).where(BusinessUser.username == username))
    ).scalar_one_or_none()
    if existing is not None:
        raise Conflict("用户名已被使用", code="USERNAME_TAKEN")
    password_hash = await hash_password_async(password)
    user = BusinessUser(
        username=username,
        display_name=display_name[:64],
        password_hash=password_hash,
        role=role,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("用户名已被使用", code="USERNAME_TAKEN") from exc
    await session.refresh(user)
    return user


async def find_by_username(session, username: str) -> BusinessUser | None:
    return (
        await session.execute(select(BusinessUser).where(BusinessUser.username == username))
    ).scalar_one_or_none()


async def count_admins(session) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(BusinessUser).where(BusinessUser.role == "admin")
        )
    ).scalar_one()


async def list_customers(session, *, query: str, page: int, page_size: int):
    statement = select(BusinessUser).where(BusinessUser.role == "customer")
    count_statement = select(func.count()).select_from(BusinessUser).where(
        BusinessUser.role == "customer"
    )
    term = query.strip()
    if term:
        pattern = f"%{term}%"
        condition = or_(
            BusinessUser.username.ilike(pattern),
            BusinessUser.display_name.ilike(pattern),
        )
        statement = statement.where(condition)
        count_statement = count_statement.where(condition)
    total = (await session.execute(count_statement)).scalar_one()
    rows = (
        await session.execute(
            statement.order_by(BusinessUser.created_at.desc(), BusinessUser.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars()
    return list(rows), total


def public_user(user: BusinessUser) -> dict:
    from app.business.principal import Principal

    principal = Principal.for_user(
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
    )
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "permissions": sorted(principal.permissions),
        "created_at": (user.created_at or utc_now()).isoformat(),
    }
