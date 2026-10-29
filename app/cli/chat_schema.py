"""Explicit additive migration; application startup never creates chat tables."""

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import inspect, text

from app.chat.models import Conversation, Message
from app.cli.business_schema import load_statements, target_label
from app.db.mysql_session import engine

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/20261027_chat.sql"


def validate_chat_schema(connection):
    inspector = inspect(connection)
    for model in (Conversation, Message):
        table = model.__table__
        if not inspector.has_table(table.name):
            raise RuntimeError(f"缺少 {table.name}；请执行 chat_schema apply --yes")
        actual = {column["name"] for column in inspector.get_columns(table.name)}
        missing = set(table.columns.keys()) - actual
        if missing:
            raise RuntimeError(f"{table.name} 缺少字段 {sorted(missing)}")
    unique = inspector.get_unique_constraints(Message.__tablename__)
    if not any(c["column_names"] == ["conversation_id", "sequence"] for c in unique):
        raise RuntimeError("chat_messages 缺少消息顺序唯一约束")
    for name, columns, target in (
        ("chat_conversations", ["user_id"], "business_users"),
        ("chat_messages", ["conversation_id"], "chat_conversations"),
    ):
        if not any(
            f["constrained_columns"] == columns and f["referred_table"] == target
            for f in inspector.get_foreign_keys(name)
        ):
            raise RuntimeError(f"{name} 缺少归属外键")


async def run(apply=False, confirmed=False):
    print(f"目标库：{target_label()}")
    try:
        async with engine.connect() as connection:
            if apply:
                if not confirmed:
                    print("未执行；仅新增 chat_conversations/chat_messages。确认后追加 --yes。")
                    return 2
                for statement in load_statements(MIGRATION):
                    await connection.execute(text(statement))
                    await connection.commit()
            await connection.run_sync(validate_chat_schema)
        print("聊天两张表及消息顺序唯一约束、归属外键校验通过。")
        return 0
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="聊天表结构显式迁移与只读校验")
    parser.add_argument("command", choices=["check", "apply"])
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    return asyncio.run(run(args.command == "apply", args.yes))


if __name__ == "__main__":
    raise SystemExit(main())
