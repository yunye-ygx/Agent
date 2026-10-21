from sqlalchemy import inspect

from app.db.mysql_models import Base
from app.db.mysql_session import engine


def validate_a1_schema(connection):
    inspector = inspect(connection)
    requirements = {
        "tasks": {
            "attempt_id",
            "attempt_count",
            "lease_until",
            "dispatch_seq",
            "source_task_id",
            "output_fingerprint",
        },
        "generations": {
            "file_version",
            "file_location_snapshot",
            "published_at",
            "artifacts_available",
        },
        "knowledge_bases": {"embedding_model", "embedding_dimension", "embedding_base_url"},
    }
    for table, required in requirements.items():
        if inspector.has_table(table):
            actual = {column["name"] for column in inspector.get_columns(table)}
            if required - actual:
                raise RuntimeError(
                    f"现有 MySQL 表 {table} 需要 A1 迁移；启动不会自动 ALTER，请先审核 docs/a1-migration.md"
                )


async def create_mysql_schema() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(validate_a1_schema)
        await connection.run_sync(Base.metadata.create_all)
