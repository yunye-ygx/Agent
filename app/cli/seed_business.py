"""Load the synthetic after-sales dataset. Dry-run by default; apply is explicit.

Passwords are never accepted as command-line arguments. They are prompted with
no echo, or read from standard input when --password-stdin is used by a local
secret manager.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from datetime import date
from pathlib import Path

from app.business.seed_data import (
    SeedError,
    apply_dataset,
    describe,
    load_dataset,
    plan_missing_users,
)
from app.core.config import settings
from app.db.mysql_session import AsyncSessionFactory

DEFAULT_DATA_DIR = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "samples"
    / "after-sales-business-v1"
)


def _check_password(value: str, username: str) -> str:
    value = value.strip("\r\n")
    if not 12 <= len(value) <= 128:
        raise SeedError(f"账号 {username} 的密码长度必须为 12～128 个字符")
    return value


def _read_passwords(missing: list[dict], use_stdin: bool) -> dict[str, str]:
    passwords: dict[str, str] = {}
    if use_stdin:
        for user in missing:
            value = sys.stdin.readline()
            if not value:
                raise SeedError("标准输入的密码行数不足")
            passwords[user["username"]] = _check_password(value, user["username"])
        return passwords
    for user in missing:
        first = getpass.getpass(f"为 {user['username']} 设置密码（12～128 字符）：")
        second = getpass.getpass("再次输入密码：")
        if first != second:
            raise SeedError(f"账号 {user['username']} 两次密码不一致")
        passwords[user["username"]] = _check_password(first, user["username"])
    return passwords


async def _apply(dataset, create_users: bool, use_stdin: bool) -> dict:
    if not create_users:
        # Still detect ID/username conflicts before reporting anything.
        async with AsyncSessionFactory() as session:
            missing = await plan_missing_users(session, dataset.users)
        if missing:
            names = ", ".join(user["username"] for user in missing)
            raise SeedError(
                f"账号尚不存在：{names}。先使用 app.cli.create_admin 创建管理员，"
                "并让客户自行注册；或加 --create-users 由本命令创建。"
            )
        passwords: dict[str, str] = {}
    else:
        async with AsyncSessionFactory() as session:
            missing = await plan_missing_users(session, dataset.users)
        passwords = _read_passwords(missing, use_stdin)
    async with AsyncSessionFactory() as session:
        return await apply_dataset(session, dataset, passwords)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入合成售后业务数据（默认 dry-run）")
    parser.add_argument("--dry-run", action="store_true", help="只校验样例文件，不连接数据库")
    parser.add_argument("--apply", action="store_true", help="执行导入；还需 --yes")
    parser.add_argument("--yes", action="store_true", help="确认目标库并允许写入")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None, help="必须等于 manifest 基准日")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--create-users", action="store_true", help="为缺失的样例账号创建密码")
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="从标准输入按账号顺序读取密码（不进入命令行参数或日志）",
    )
    args = parser.parse_args(argv)
    if args.dry_run and args.apply:
        print("--dry-run 与 --apply 只能选择一个", file=sys.stderr)
        return 2
    apply = args.apply
    try:
        dataset = load_dataset(args.data_dir, args.as_of)
    except SeedError as exc:
        print(f"数据校验失败：{exc}", file=sys.stderr)
        return 1
    print(f"目标库：{settings.mysql_host}:{settings.mysql_port}/{settings.mysql_database}（不含连接凭据）")
    print(describe(dataset))
    if not apply:
        print("dry-run：仅校验数据，未连接数据库、未写入任何记录。")
        return 0
    if not args.yes:
        print("未执行。确认目标库后追加 --yes。", file=sys.stderr)
        return 2
    try:
        report = asyncio.run(_apply(dataset, args.create_users, args.password_stdin))
    except SeedError as exc:
        print(f"导入停止：{exc}", file=sys.stderr)
        return 1
    print(
        "导入完成："
        f"用户新增 {report['users_created']} / 跳过 {report['users_skipped']}；"
        f"订单新增 {report['orders_created']} / 跳过 {report['orders_skipped']}；"
        f"设备新增 {report['devices_created']} / 跳过 {report['devices_skipped']}；"
        f"工单新增 {report['tickets_created']} / 跳过 {report['tickets_skipped']}。"
    )
    print("已有记录不会被覆盖；重复执行不会增加数量。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
