"""Create the first administrator. Passwords are never accepted as arguments.

Default mode prompts twice with no echo. `--password-stdin` exists so a secret
manager or automated local setup can pipe a password without it appearing in
shell history, process arguments or logs.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from app.auth.schemas import check_password, check_username
from app.auth.users import create_user
from app.business.errors import BusinessError
from app.db.mysql_session import AsyncSessionFactory


def _read_password(use_stdin: bool) -> str:
    if use_stdin:
        value = sys.stdin.readline().rstrip("\r\n")
        return check_password(value)
    first = getpass.getpass("管理员密码（12～128 字符）：")
    check_password(first)
    second = getpass.getpass("再次输入密码：")
    if first != second:
        raise ValueError("两次输入的密码不一致")
    return first


async def create_admin(username: str, display_name: str, password: str) -> None:
    async with AsyncSessionFactory() as session:
        user = await create_user(
            session,
            username=username,
            display_name=display_name,
            password=password,
            role="admin",
        )
    print(f"已创建管理员：{user.username}（{user.display_name}），角色={user.role}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="创建售后业务管理员（无默认密码）")
    parser.add_argument("username", help="3～32 位小写字母、数字或下划线")
    parser.add_argument("--display-name", default="售后管理员")
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="从标准输入读取密码，供密钥管理或自动化使用（不进入命令行参数）",
    )
    args = parser.parse_args(argv)
    try:
        username = check_username(args.username)
        password = _read_password(args.password_stdin)
    except ValueError as exc:
        print(f"输入不合法：{exc}", file=sys.stderr)
        return 2
    try:
        asyncio.run(create_admin(username, args.display_name.strip(), password))
    except BusinessError as exc:
        print(f"创建失败：{exc.message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
