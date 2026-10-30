from sqlalchemy import inspect

from app.db.business_schema import BUSINESS_TABLE_NAMES, validate_business_schema
from app.db.mysql_models import Base
from app.db.mysql_session import engine


def validate_a1_schema(connection):
    inspector = inspect(connection)
    a1_hint = "A1 迁移；启动不会自动 ALTER，请先审核 docs/a1-migration.md"
    requirements = {
        "tasks": (
            {
                "attempt_id",
                "attempt_count",
                "lease_until",
                "dispatch_seq",
                "source_task_id",
                "output_fingerprint",
            },
            a1_hint,
        ),
        "generations": (
            {
                "file_version",
                "file_location_snapshot",
                "published_at",
                "artifacts_available",
            },
            a1_hint,
        ),
        "knowledge_bases": (
            {"embedding_model", "embedding_dimension", "embedding_base_url"},
            a1_hint,
        ),
        "documents": (
            {"support_open"},
            "售后资料开放迁移；请先执行 migrations/20261026b_documents_support_open.sql",
        ),
    }
    for table, (required, hint) in requirements.items():
        if inspector.has_table(table):
            actual = {column["name"] for column in inspector.get_columns(table)}
            if required - actual:
                raise RuntimeError(
                    f"现有 MySQL 表 {table} 需要{hint}（缺失字段："
                    f"{', '.join(sorted(required - actual))}）"
                )


async def create_mysql_schema() -> None:
    # Business models live on a separate metadata object. This invariant makes
    # adding an import unable to silently create business tables at startup.
    overlap = set(BUSINESS_TABLE_NAMES) & set(Base.metadata.tables)
    if overlap:
        raise RuntimeError(
            "业务模型不得加入离线建库 metadata：" + ", ".join(sorted(overlap))
        )
    async with engine.begin() as connection:
        await connection.run_sync(validate_a1_schema)
        await connection.run_sync(Base.metadata.create_all)
        # Read-only: business tables must come from the reviewed migration.
        await connection.run_sync(validate_business_schema)
