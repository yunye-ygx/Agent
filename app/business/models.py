"""Business tables, intentionally separate from the offline-ingestion metadata.

`BusinessBase` is never passed to `create_all` at application startup. The only
DDL authority is migrations/20261026_after_sales_business.sql, and startup runs a
read-only compatibility check through app/db/business_schema.py.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.business.utils import new_id, utc_now

ROLES = ("customer", "admin")
USER_STATUSES = ("active", "disabled")
ORDER_STATUSES = ("recorded", "completed", "cancelled")
DEVICE_STATUSES = ("active", "retired")
TICKET_STATUSES = ("open", "in_progress", "resolved", "closed")
OPERATION_STATUSES = ("reserved", "succeeded")
OPERATION_ACTIONS = ("create_ticket",)
SUPPORTED_MODEL_CODES = ("NX100", "NX200")


class BusinessBase(DeclarativeBase):
    pass


class BusinessUser(BusinessBase):
    __tablename__ = "business_users"
    __table_args__ = (
        UniqueConstraint("username", name="uk_business_users_username"),
        CheckConstraint("role IN ('customer','admin')", name="ck_business_users_role"),
        CheckConstraint("status IN ('active','disabled')", name="ck_business_users_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="customer")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )


class AuthSession(BusinessBase):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uk_auth_sessions_token"),
        Index("ix_auth_sessions_user_expires", "user_id", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("business_users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class AuthRateLimit(BusinessBase):
    __tablename__ = "auth_rate_limits"

    bucket_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class Order(BusinessBase):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("order_no", name="uk_orders_order_no"),
        Index("ix_orders_user_created", "user_id", "created_at"),
        CheckConstraint(
            "status IN ('recorded','completed','cancelled')", name="ck_orders_status"
        ),
        CheckConstraint("total_amount >= 0", name="ck_orders_total_nonnegative"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    order_no: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("business_users.id", ondelete="RESTRICT"), nullable=False
    )
    created_by: Mapped[str] = mapped_column(
        ForeignKey("business_users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="recorded")
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )


class OrderItem(BusinessBase):
    __tablename__ = "order_items"
    __table_args__ = (
        Index("ix_order_items_order", "order_id"),
        CheckConstraint("quantity BETWEEN 1 AND 1000", name="ck_order_items_quantity"),
        CheckConstraint("unit_price >= 0", name="ck_order_items_unit_price"),
        CheckConstraint("line_amount >= 0", name="ck_order_items_line_amount"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    product_model: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    line_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)


class Device(BusinessBase):
    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("serial_no", name="uk_devices_serial_no"),
        Index("ix_devices_user", "user_id"),
        Index("ix_devices_order_item", "order_item_id"),
        CheckConstraint("status IN ('active','retired')", name="ck_devices_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    serial_no: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("business_users.id", ondelete="RESTRICT"), nullable=False
    )
    order_item_id: Mapped[str] = mapped_column(
        ForeignKey("order_items.id", ondelete="RESTRICT"), nullable=False
    )
    model_code: Mapped[str] = mapped_column(String(16), nullable=False)
    firmware_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    purchased_on: Mapped[date] = mapped_column(Date, nullable=False)
    warranty_until: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class Ticket(BusinessBase):
    __tablename__ = "tickets"
    __table_args__ = (
        UniqueConstraint("ticket_no", name="uk_tickets_ticket_no"),
        Index("ix_tickets_user_created", "user_id", "created_at"),
        Index("ix_tickets_status_created", "status", "created_at"),
        CheckConstraint(
            "status IN ('open','in_progress','resolved','closed')", name="ck_tickets_status"
        ),
        CheckConstraint("version >= 1", name="ck_tickets_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    ticket_no: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("business_users.id", ondelete="RESTRICT"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False
    )
    subject: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(4000), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )


class TicketEvent(BusinessBase):
    __tablename__ = "ticket_events"
    __table_args__ = (Index("ix_ticket_events_ticket_created", "ticket_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(
        ForeignKey("business_users.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    message: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class BusinessOperation(BusinessBase):
    __tablename__ = "business_operations"
    __table_args__ = (
        Index("ix_business_operations_user_action", "user_id", "action"),
        CheckConstraint("status IN ('reserved','succeeded')", name="ck_operations_status"),
    )

    # The primary key is the backend-generated operation key used for idempotency.
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("business_users.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved")
    result_resource_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    result_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
