"""Delete expired sessions and rate-limit buckets. Explicit and non-destructive."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta

from sqlalchemy import delete, func, select

from app.business.models import AuthRateLimit, AuthSession
from app.business.utils import utc_now
from app.db.mysql_session import AsyncSessionFactory


async def _counts(session) -> tuple[int, int]:
    now = utc_now()
    sessions = (
        await session.execute(
            select(func.count()).select_from(AuthSession).where(AuthSession.expires_at <= now)
        )
    ).scalar_one()
    buckets = (
        await session.execute(
            select(func.count()).select_from(AuthRateLimit).where(AuthRateLimit.expires_at <= now)
        )
    ).scalar_one()
    return sessions, buckets


async def cleanup(apply: bool) -> tuple[int, int]:
    async with AsyncSessionFactory() as session:
        sessions, buckets = await _counts(session)
        if not apply:
            return sessions, buckets
        now = utc_now()
        await session.execute(
            delete(AuthSession).where(
                (AuthSession.expires_at <= now)
                | (AuthSession.revoked_at.is_not(None) & (AuthSession.revoked_at <= now - timedelta(hours=8)))
            )
        )
        await session.execute(delete(AuthRateLimit).where(AuthRateLimit.expires_at <= now))
        await session.commit()
        return sessions, buckets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理过期登录会话和限速桶")
    parser.add_argument("--apply", action="store_true", help="执行删除；默认只统计")
    parser.add_argument("--yes", action="store_true", help="确认允许删除过期记录")
    args = parser.parse_args(argv)
    if args.apply and not args.yes:
        print("未执行。确认后追加 --yes。", file=sys.stderr)
        return 2
    try:
        sessions, buckets = asyncio.run(cleanup(args.apply))
    except Exception as exc:
        print(f"清理失败：{type(exc).__name__}", file=sys.stderr)
        return 1
    action = "已删除" if args.apply else "可删除（dry-run）"
    print(f"{action}过期/已撤销会话 {sessions} 条、过期限速桶 {buckets} 个。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
