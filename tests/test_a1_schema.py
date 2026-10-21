import pytest
from sqlalchemy import create_engine, text


def test_legacy_database_is_rejected_without_automatic_alter():
    from app.db.mysql_schema import validate_a1_schema

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE tasks (id VARCHAR(36) PRIMARY KEY)"))
        with pytest.raises(RuntimeError, match="迁移"):
            validate_a1_schema(connection)
        assert (
            connection.execute(text("SELECT COUNT(*) FROM pragma_table_info('tasks')")).scalar_one()
            == 1
        )
    engine.dispose()
