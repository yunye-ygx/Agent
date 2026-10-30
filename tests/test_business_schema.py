from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from app.business.models import BusinessBase
from app.cli.business_schema import load_statements
from app.db.business_schema import (
    BUSINESS_TABLE_COLUMNS,
    BUSINESS_TABLE_NAMES,
    BUSINESS_UNIQUE_KEYS,
    MIGRATION_FILE,
    validate_business_schema,
)
from app.db.mysql_models import Base

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS_MIGRATION = "migrations/20261026b_documents_support_open.sql"


def business_engine():
    engine = create_engine("sqlite://")
    BusinessBase.metadata.create_all(engine)
    return engine


def test_business_tables_have_columns_and_unique_keys() -> None:
    engine = business_engine()
    with engine.connect() as connection:
        validate_business_schema(connection)
        inspector = inspect(connection)
        assert set(BUSINESS_TABLE_NAMES) <= set(inspector.get_table_names())
        for table, required in BUSINESS_TABLE_COLUMNS.items():
            actual = {column["name"] for column in inspector.get_columns(table)}
            assert required <= actual
        for table, columns in BUSINESS_UNIQUE_KEYS.items():
            unique_sets = {
                tuple(item.get("column_names") or ())
                for item in inspector.get_unique_constraints(table)
            }
            unique_sets |= {
                tuple(item.get("column_names") or ())
                for item in inspector.get_indexes(table)
                if item.get("unique")
            }
            assert columns in unique_sets
    engine.dispose()


def test_missing_business_tables_are_rejected_with_migration_hint() -> None:
    engine = create_engine("sqlite://")
    with engine.connect() as connection, pytest.raises(RuntimeError, match="迁移"):
        validate_business_schema(connection)
    engine.dispose()


def test_legacy_create_all_never_creates_business_tables() -> None:
    assert set(BUSINESS_TABLE_NAMES).isdisjoint(Base.metadata.tables)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with engine.connect() as connection:
        assert set(BUSINESS_TABLE_NAMES).isdisjoint(inspect(connection).get_table_names())
    engine.dispose()


def test_migration_sql_is_additive_and_covers_all_tables() -> None:
    sql = (ROOT / MIGRATION_FILE).read_text(encoding="utf-8")
    for table in BUSINESS_TABLE_NAMES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "support_knowledge_documents" not in sql
    upper = sql.upper()
    assert "DROP TABLE" not in upper
    assert "TRUNCATE" not in upper
    statements = load_statements(ROOT / MIGRATION_FILE)
    assert len(statements) == len(BUSINESS_TABLE_NAMES)
    assert all(statement.upper().startswith("CREATE TABLE") for statement in statements)


def test_documents_support_open_migration_is_guarded_and_drops_legacy_table() -> None:
    sql = (ROOT / DOCUMENTS_MIGRATION).read_text(encoding="utf-8")
    assert "documents" in sql and "support_open" in sql
    assert "support_knowledge_documents" in sql
    assert "information_schema" in sql
    statements = load_statements(ROOT / DOCUMENTS_MIGRATION)
    assert any("ALTER TABLE documents ADD COLUMN support_open" in item for item in statements)
    assert any("DROP TABLE support_knowledge_documents" in item for item in statements)
    assert all(
        item.upper().startswith(("SET", "PREPARE", "EXECUTE", "DEALLOCATE"))
        for item in statements
    )


def test_documents_table_owns_the_support_flag() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with engine.connect() as connection:
        columns = {column["name"] for column in inspect(connection).get_columns("documents")}
    assert "support_open" in columns
    engine.dispose()
