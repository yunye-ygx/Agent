"""Inspect or explicitly apply the after-sales business migration.

`check` is read-only. `apply` requires --yes, never drops tables, and reports
already-present tables/constraints as skipped steps. DDL cannot roll back in
MySQL, so a partially applied script is re-run and then re-validated.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.db.business_schema import BUSINESS_TABLE_NAMES, MIGRATION_FILE, validate_business_schema
from app.db.mysql_session import engine

DEFAULT_MIGRATION = Path(__file__).resolve().parents[2] / MIGRATION_FILE
ALREADY_APPLIED_MYSQL_CODES = {1050, 1060, 1061, 1091, 1826}  # table/column/key already exists


def target_label() -> str:
    return f"{settings.mysql_host}:{settings.mysql_port}/{settings.mysql_database}"


def load_statements(path: Path) -> list[str]:
    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--")
    ]
    return [statement.strip() for statement in "\n".join(lines).split(";") if statement.strip()]


async def check_schema() -> int:
    print(f"目标库：{target_label()}（不含连接凭据）")
    try:
        async with engine.connect() as connection:
            await connection.run_sync(validate_business_schema)
    except Exception as exc:
        print(f"检查失败：{exc}")
        return 1
    print(f"业务表完整：{', '.join(BUSINESS_TABLE_NAMES)}")
    return 0


async def apply_schema(path: Path, confirmed: bool) -> int:
    print(f"目标库：{target_label()}（不含连接凭据）")
    print(f"迁移文件：{path}")
    statements = load_statements(path)
    print(f"共 {len(statements)} 条语句；按文件顺序执行，脚本自身负责守卫。")
    if not confirmed:
        print("未执行。确认目标库后追加 --yes 显式应用。")
        return 2

    failures = 0
    # One connection for the whole script keeps @session variables (used by the
    # guarded documents.support_open migration) available across statements.
    async with engine.connect() as connection:
        for index, statement in enumerate(statements, start=1):
            label = " ".join(statement.split())[:88]
            try:
                await connection.execute(text(statement))
                await connection.commit()
            except SQLAlchemyError as exc:
                original = getattr(exc, "orig", None)
                code = (
                    original.args[0]
                    if original is not None and getattr(original, "args", None)
                    and isinstance(original.args[0], int)
                    else None
                )
                if code in ALREADY_APPLIED_MYSQL_CODES:
                    print(f"[{index}] 已存在，跳过：{label}")
                    continue
                failures += 1
                print(f"[{index}] 失败（{type(exc).__name__}）：{label}")
                break
            print(f"[{index}] 已应用：{label}")

    result = await check_schema()
    if failures or result:
        return 1
    print("迁移应用完成，结构校验通过。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="售后业务表结构检查与显式迁移")
    sub = parser.add_subparsers(dest="command")
    check = sub.add_parser("check", help="只读检查九张业务表")
    check.add_argument("--file", type=Path, default=DEFAULT_MIGRATION)
    apply_parser = sub.add_parser("apply", help="按评审过的 SQL 顺序建表")
    apply_parser.add_argument("--file", type=Path, default=DEFAULT_MIGRATION)
    apply_parser.add_argument("--yes", action="store_true", help="确认目标库并允许执行 DDL")
    args = parser.parse_args(argv)

    if args.command == "apply":
        return asyncio.run(apply_schema(args.file, args.yes))
    return asyncio.run(check_schema())


if __name__ == "__main__":
    sys.exit(main())
