from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """Normalize datetimes read from MySQL (which returns naive UTC values)."""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def money(value: Decimal | int | None) -> str:
    if value is None:
        return "0.00"
    return format(Decimal(value).quantize(Decimal("0.01")), "f")


def iso_datetime(value: datetime | None) -> str:
    normalized = as_utc(value)
    return normalized.isoformat() if normalized else ""


def iso_date(value: date | None) -> str:
    return value.isoformat() if value else ""
