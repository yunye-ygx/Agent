"""Shared adapters for real business tools.

Session creation is indirected through this module so tests can bind the same
tool handlers to an isolated database without any production-only test bypass.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy.exc import SQLAlchemyError

from app.business.models import BusinessUser
from app.business.principal import Principal
from app.db.mysql_session import AsyncSessionFactory
from app.tool_runtime import ExecutionContext, RetryableToolError, ToolRejected

session_factory = AsyncSessionFactory


@asynccontextmanager
async def read_session():
    try:
        async with session_factory() as session:
            yield session
    except SQLAlchemyError as exc:
        # A read can safely retry a transient database/connection failure.
        raise RetryableToolError() from exc


async def load_principal(session, context: ExecutionContext) -> Principal:
    """Resolve role and permissions from the database, not from model arguments."""

    user = await session.get(BusinessUser, context.user_id)
    if user is None or user.status != "active":
        raise ToolRejected("UNAUTHENTICATED", "登录状态已失效。")
    return Principal.for_user(
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
    )
