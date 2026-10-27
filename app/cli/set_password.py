"""Reset a business account password and revoke its existing sessions."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select, update

from app.auth.passwords import hash_password_async
from app.auth.schemas import check_password, check_username
from app.business.models import AuthSession, BusinessUser
from app.business.utils import utc_now
from app.db.mysql_session import AsyncSessionFactory


async def reset_password(username: str, password: str) -> str:
    async with AsyncSessionFactory() as session:
        user = (
            await session.execute(
                select(BusinessUser).where(BusinessUser.username == username)
            )
        ).scalar_one_or_none()
        if user is None:
            raise ValueError("账号不存在")
        user.password_hash = await hash_password_async(password)
        user.updated_at = utc_now()
        await session.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=utc_now())
        )
        await session.commit()
        return user.username


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重置业务账号密码（交互输入，不进参数）")
    parser.add_argument("username")
    parser.add_argument(
        "--password-stdin", action="store_true", help="从标准输入读取密码（不进入命令行参数）"
    )
    args = parser.parse_args(argv)
    try:
        username = check_username(args.username)
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\r\n")
            check_password(password)
        else:
            password = getpass.getpass("新密码（12～128 字符）：")
            check_password(password)
            if password != getpass.getpass("再次输入新密码："):
                raise ValueError("两次输入的密码不一致")
        asyncio.run(reset_password(username, password))
    except ValueError as exc:
        print(f"重置失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"重置失败：{type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"已重置 {username} 的密码，并撤销其现有登录会话。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
