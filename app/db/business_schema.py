"""Read-only business schema contract.

Startup verifies these tables but never creates or alters them. The single DDL
source is migrations/20261026_after_sales_business.sql.
"""

from __future__ import annotations

from sqlalchemy import inspect

MIGRATION_FILE = "migrations/20261026_after_sales_business.sql"

BUSINESS_TABLE_COLUMNS: dict[str, frozenset[str]] = {
    "business_users": frozenset(
        {
            "id",
            "username",
            "display_name",
            "password_hash",
            "role",
            "status",
            "created_at",
            "updated_at",
        }
    ),
    "auth_sessions": frozenset(
        {
            "id",
            "user_id",
            "token_hash",
            "csrf_token_hash",
            "expires_at",
            "revoked_at",
            "created_at",
        }
    ),
    "auth_rate_limits": frozenset(
        {"bucket_key", "window_started_at", "attempt_count", "expires_at"}
    ),
    "orders": frozenset(
        {
            "id",
            "order_no",
            "user_id",
            "created_by",
            "status",
            "total_amount",
            "currency",
            "created_at",
            "updated_at",
        }
    ),
    "order_items": frozenset(
        {
            "id",
            "order_id",
            "product_name",
            "product_model",
            "quantity",
            "unit_price",
            "line_amount",
        }
    ),
    "devices": frozenset(
        {
            "id",
            "serial_no",
            "user_id",
            "order_item_id",
            "model_code",
            "firmware_version",
            "purchased_on",
            "warranty_until",
            "status",
            "created_at",
        }
    ),
    "tickets": frozenset(
        {
            "id",
            "ticket_no",
            "user_id",
            "device_id",
            "subject",
            "description",
            "status",
            "version",
            "created_at",
            "updated_at",
        }
    ),
    "ticket_events": frozenset(
        {
            "id",
            "ticket_id",
            "actor_id",
            "event_type",
            "from_status",
            "to_status",
            "message",
            "created_at",
        }
    ),
    "business_operations": frozenset(
        {
            "id",
            "user_id",
            "action",
            "request_hash",
            "status",
            "result_resource_id",
            "result_snapshot",
            "expires_at",
            "created_at",
            "completed_at",
        }
    ),
}

BUSINESS_UNIQUE_KEYS: dict[str, tuple[str, ...]] = {
    "business_users": ("username",),
    "auth_sessions": ("token_hash",),
    "orders": ("order_no",),
    "devices": ("serial_no",),
    "tickets": ("ticket_no",),
}

# Foreign keys that the migration must declare. SQLite test databases created
# directly from the models cannot fully express the cross-metadata document FK.
REQUIRED_FOREIGN_KEYS: dict[str, frozenset[tuple[str, str]]] = {
    "auth_sessions": frozenset({("user_id", "business_users")}),
    "orders": frozenset(
        {("user_id", "business_users"), ("created_by", "business_users")}
    ),
    "order_items": frozenset({("order_id", "orders")}),
    "devices": frozenset(
        {("user_id", "business_users"), ("order_item_id", "order_items")}
    ),
    "tickets": frozenset({("user_id", "business_users"), ("device_id", "devices")}),
    "ticket_events": frozenset(
        {("ticket_id", "tickets"), ("actor_id", "business_users")}
    ),
    "business_operations": frozenset({("user_id", "business_users")}),
}

BUSINESS_TABLE_NAMES = tuple(BUSINESS_TABLE_COLUMNS)


def _unique_column_sets(inspector, table: str) -> set[tuple[str, ...]]:
    values: set[tuple[str, ...]] = set()
    for constraint in inspector.get_unique_constraints(table):
        values.add(tuple(constraint.get("column_names") or ()))
    for index in inspector.get_indexes(table):
        if index.get("unique"):
            values.add(tuple(index.get("column_names") or ()))
    return values


def validate_business_schema(connection, *, require_foreign_keys: bool | None = None) -> None:
    inspector = inspect(connection)
    if require_foreign_keys is None:
        require_foreign_keys = inspector.dialect.name == "mysql"

    missing_tables = [name for name in BUSINESS_TABLE_NAMES if not inspector.has_table(name)]
    if missing_tables:
        raise RuntimeError(
            "业务表尚未迁移："
            + ", ".join(missing_tables)
            + f"。启动不会自动建表，请先审核并执行 {MIGRATION_FILE}"
        )

    for table, required in BUSINESS_TABLE_COLUMNS.items():
        actual = {column["name"] for column in inspector.get_columns(table)}
        missing = required - actual
        if missing:
            raise RuntimeError(
                f"业务表 {table} 缺少列：{', '.join(sorted(missing))}；"
                f"请审核 {MIGRATION_FILE}，启动不会自动 ALTER"
            )

    for table, columns in BUSINESS_UNIQUE_KEYS.items():
        if columns not in _unique_column_sets(inspector, table):
            raise RuntimeError(f"业务表 {table} 缺少唯一约束：{', '.join(columns)}")

    if require_foreign_keys:
        for table, required in REQUIRED_FOREIGN_KEYS.items():
            actual = {
                (fk.get("constrained_columns") or [""])[0]: fk["referred_table"]
                for fk in inspector.get_foreign_keys(table)
            }
            missing = required - set(actual.items())
            if missing:
                rendered = ", ".join(f"{column}->{target}" for column, target in sorted(missing))
                raise RuntimeError(f"业务表 {table} 缺少外键：{rendered}")
