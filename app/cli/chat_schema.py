"""Explicit additive migrations; application startup only performs read-only checks."""

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import inspect, text

from app.chat.models import (
    ChatOutboxEvent,
    Conversation,
    Message,
    Run,
    RunEvent,
    ToolExecution,
    Turn,
)
from app.cli.business_schema import load_statements, target_label
from app.db.mysql_session import engine

ROOT = Path(__file__).resolve().parents[2]
INITIAL_MIGRATION = ROOT / "migrations/20261027_chat.sql"
DURABLE_MIGRATION = ROOT / "migrations/20261029_durable_chat.sql"
MODELS = (Conversation, Message, Turn, Run, ToolExecution, RunEvent, ChatOutboxEvent)


def _has_unique(inspector, table: str, columns: list[str]) -> bool:
    return any(
        constraint["column_names"] == columns
        for constraint in inspector.get_unique_constraints(table)
    )


def _has_foreign_key(inspector, table: str, columns: list[str], target: str) -> bool:
    return any(
        foreign_key["constrained_columns"] == columns
        and foreign_key["referred_table"] == target
        for foreign_key in inspector.get_foreign_keys(table)
    )


def _has_index(inspector, table: str, columns: list[str]) -> bool:
    return any(index["column_names"] == columns for index in inspector.get_indexes(table))


def validate_chat_schema(connection):
    inspector = inspect(connection)
    for model in MODELS:
        table = model.__table__
        if not inspector.has_table(table.name):
            raise RuntimeError(f"缺少 {table.name}；请执行 chat_schema apply --yes")
        actual = {column["name"] for column in inspector.get_columns(table.name)}
        missing = set(table.columns.keys()) - actual
        if missing:
            raise RuntimeError(f"{table.name} 缺少字段 {sorted(missing)}")

    unique_requirements = (
        ("chat_messages", ["conversation_id", "sequence"]),
        ("chat_messages", ["run_id", "source_key"]),
        ("chat_turns", ["conversation_id", "turn_no"]),
        ("chat_turns", ["conversation_id", "client_request_id"]),
        ("chat_runs", ["turn_id", "attempt_no"]),
        ("chat_tool_executions", ["run_id", "tool_call_id"]),
        ("chat_run_events", ["run_id", "seq"]),
        ("chat_outbox_events", ["deduplication_key"]),
    )
    for table, columns in unique_requirements:
        if not _has_unique(inspector, table, columns):
            raise RuntimeError(f"{table} 缺少唯一约束 {columns}")

    foreign_key_requirements = (
        ("chat_conversations", ["user_id"], "business_users"),
        ("chat_messages", ["conversation_id"], "chat_conversations"),
        ("chat_messages", ["run_id"], "chat_runs"),
        ("chat_turns", ["conversation_id"], "chat_conversations"),
        ("chat_turns", ["user_message_id"], "chat_messages"),
        ("chat_turns", ["latest_run_id"], "chat_runs"),
        ("chat_turns", ["active_run_id"], "chat_runs"),
        ("chat_runs", ["turn_id"], "chat_turns"),
        ("chat_tool_executions", ["run_id"], "chat_runs"),
        ("chat_run_events", ["run_id"], "chat_runs"),
    )
    for table, columns, target in foreign_key_requirements:
        if not _has_foreign_key(inspector, table, columns, target):
            raise RuntimeError(f"{table} 缺少外键 {columns} -> {target}")

    index_requirements = (
        ("chat_runs", ["status", "lease_expires_at"]),
        ("chat_runs", ["status", "queued_at"]),
        ("chat_run_events", ["run_id", "created_at"]),
        ("chat_run_events", ["expires_at"]),
        ("chat_outbox_events", ["status", "available_at"]),
    )
    for table, columns in index_requirements:
        if not _has_index(inspector, table, columns):
            raise RuntimeError(f"{table} 缺少索引 {columns}")


async def _existing_tables(connection) -> set[str]:
    return await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))


async def run(apply: bool = False, confirmed: bool = False) -> int:
    print(f"目标库：{target_label()}")
    try:
        async with engine.connect() as connection:
            tables = await _existing_tables(connection)
            if apply:
                if not confirmed:
                    print("未执行；迁移仅新增 Durable Chat 结构。确认后追加 --yes。")
                    return 2
                migrations = []
                if "chat_conversations" not in tables:
                    migrations.append(INITIAL_MIGRATION)
                if "chat_turns" not in tables:
                    migrations.append(DURABLE_MIGRATION)
                for migration in migrations:
                    for statement in load_statements(migration):
                        await connection.execute(text(statement))
                        await connection.commit()
            await connection.run_sync(validate_chat_schema)
        print("Durable Chat 表、字段、外键、唯一约束和恢复索引校验通过。")
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="聊天表结构显式迁移与只读校验")
    parser.add_argument("command", choices=["check", "apply"])
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    return asyncio.run(run(args.command == "apply", args.yes))


if __name__ == "__main__":
    raise SystemExit(main())
