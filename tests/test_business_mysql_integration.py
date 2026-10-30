"""Real MySQL/InnoDB acceptance for constraints, rollback, locks and idempotency.

Runs only when an explicitly dedicated test database is supplied:

    BUSINESS_TEST_MYSQL_HOST / PORT / USER / PASSWORD / DATABASE

The database name must end with `_test` and must differ from the application
database. The test creates the reviewed business tables if absent and only
deletes its own `itest-` records; it never calls drop_all and never touches the
application database.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.business import devices as device_service
from app.business import operations as operation_service
from app.business import tickets as ticket_service
from app.business.errors import ConcurrentUpdate, Conflict
from app.business.models import (
    AuthRateLimit,
    AuthSession,
    BusinessOperation,
    BusinessUser,
    Device,
    Order,
    OrderItem,
    Ticket,
    TicketEvent,
)
from app.business.principal import Principal
from app.business.schemas import DeviceCreate, TicketCreate, TicketEventCreate
from app.core.config import settings
from app.db.business_schema import MIGRATION_FILE

ROOT = Path(__file__).resolve().parents[1]
ALICE_ID = "itest-alice"
ADMIN_ID = "itest-admin"
OPERATION_PARAMS = TicketCreate(
    serial_no="ITEST-SN-1", subject="MySQL 并发验收", description="真实数据库幂等测试"
)


def _connection_url() -> str:
    values = {
        "host": os.getenv("BUSINESS_TEST_MYSQL_HOST", ""),
        "port": os.getenv("BUSINESS_TEST_MYSQL_PORT", ""),
        "user": os.getenv("BUSINESS_TEST_MYSQL_USER", ""),
        "password": os.getenv("BUSINESS_TEST_MYSQL_PASSWORD", ""),
        "database": os.getenv("BUSINESS_TEST_MYSQL_DATABASE", ""),
    }
    if not all(values.values()):
        pytest.skip("未提供隔离 MySQL 测试库（BUSINESS_TEST_MYSQL_*），未运行真实并发验收")
    if not values["database"].endswith("_test"):
        pytest.fail("测试库名必须以 _test 结尾")
    if values["database"] == settings.mysql_database:
        pytest.fail("测试库不能是应用库")
    from urllib.parse import quote_plus

    return (
        f"mysql+asyncmy://{quote_plus(values['user'])}:{quote_plus(values['password'])}"
        f"@{values['host']}:{values['port']}/{quote_plus(values['database'])}"
    )


async def _create_schema(engine) -> None:
    from app.cli.business_schema import load_statements
    from app.db.mysql_models import Base

    # The allowlist has a real FK to documents. Create the legacy ingestion
    # tables first in the dedicated test database (no drop, no data copy).
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    for statement in load_statements(ROOT / MIGRATION_FILE):
        async with engine.begin() as connection:
            await connection.execute(text(statement))


async def _cleanup(factory) -> None:
    async with factory() as session:
        ticket_ids = select(Ticket.id).where(Ticket.user_id.in_([ALICE_ID, ADMIN_ID]))
        await session.execute(
            delete(TicketEvent).where(TicketEvent.ticket_id.in_(ticket_ids))
        )
        await session.execute(delete(Ticket).where(Ticket.user_id.in_([ALICE_ID, ADMIN_ID])))
        await session.execute(
            delete(BusinessOperation).where(
                BusinessOperation.user_id.in_([ALICE_ID, ADMIN_ID])
            )
        )
        for model in (Device, OrderItem, Order):
            await session.execute(
                delete(model).where(
                    or_(model.id.like("itest-%"), model.id.like("ITEST-%"))
                    if model is not Device
                    else or_(
                        Device.id.like("itest-%"), Device.serial_no.like("ITEST-%")
                    )
                )
            )
        await session.execute(
            delete(AuthRateLimit).where(AuthRateLimit.bucket_key.like("itest-%"))
        )
        await session.execute(
            delete(AuthSession).where(AuthSession.id.like("itest-%"))
        )
        await session.execute(delete(BusinessUser).where(BusinessUser.id.like("itest-%")))
        await session.commit()


async def _seed(factory) -> None:
    async with factory() as session:
        session.add_all(
            [
                BusinessUser(
                    id=ADMIN_ID,
                    username="itest_admin",
                    display_name="测试管理员",
                    password_hash="unused",
                    role="admin",
                ),
                BusinessUser(
                    id=ALICE_ID,
                    username="itest_alice",
                    display_name="测试客户",
                    password_hash="unused",
                    role="customer",
                ),
            ]
        )
        await session.flush()
        session.add(
            Order(
                id="itest-order-1",
                order_no="ITEST-SO-1",
                user_id=ALICE_ID,
                created_by=ADMIN_ID,
                status="recorded",
                total_amount=Decimal("1000.00"),
                currency="CNY",
            )
        )
        await session.flush()
        session.add(
            OrderItem(
                id="itest-item-1",
                order_id="itest-order-1",
                product_name="测试打印机",
                product_model="NX100",
                quantity=2,
                unit_price=Decimal("500.00"),
                line_amount=Decimal("1000.00"),
            )
        )
        await session.flush()
        session.add(
            Device(
                id="itest-device-1",
                serial_no="ITEST-SN-1",
                user_id=ALICE_ID,
                order_item_id="itest-item-1",
                model_code="NX100",
                firmware_version="1.6.0",
                purchased_on=date(2026, 1, 1),
                warranty_until=date(2027, 1, 1),
                status="active",
            )
        )
        await session.commit()


@pytest_asyncio.fixture
async def mysql_factory():
    engine = create_async_engine(_connection_url(), pool_pre_ping=True)
    await _create_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _cleanup(factory)
    await _seed(factory)
    try:
        yield factory
    finally:
        await _cleanup(factory)
        await engine.dispose()


def customer() -> Principal:
    return Principal.for_user(
        user_id=ALICE_ID, username="itest_alice", display_name="测试客户", role="customer"
    )


def admin() -> Principal:
    return Principal.for_user(
        user_id=ADMIN_ID, username="itest_admin", display_name="测试管理员", role="admin"
    )


@pytest.mark.asyncio
async def test_unique_and_foreign_key_constraints_are_enforced(mysql_factory):
    async with mysql_factory() as session:
        session.add(
            Device(
                id="itest-device-dup",
                serial_no="ITEST-SN-1",
                user_id=ALICE_ID,
                order_item_id="itest-item-1",
                model_code="NX100",
                purchased_on=date(2026, 1, 1),
                warranty_until=date(2027, 1, 1),
                status="active",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async with mysql_factory() as session:
        session.add(
            Device(
                id="itest-device-bad",
                serial_no="ITEST-SN-BAD",
                user_id="missing-user",
                order_item_id="itest-item-1",
                model_code="NX100",
                purchased_on=date(2026, 1, 1),
                warranty_until=date(2027, 1, 1),
                status="active",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async with mysql_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(Device))
        ).scalar_one() == 1


@pytest.mark.asyncio
async def test_failed_order_insert_rolls_back(mysql_factory):
    async with mysql_factory() as session:
        session.add(
            Order(
                id="itest-order-bad",
                order_no="ITEST-SO-BAD",
                user_id="missing-user",
                created_by=ADMIN_ID,
                status="recorded",
                total_amount=Decimal("1.00"),
                currency="CNY",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
    async with mysql_factory() as session:
        assert await session.get(Order, "itest-order-bad") is None


@pytest.mark.asyncio
async def test_concurrent_same_operation_creates_exactly_one_ticket(mysql_factory):
    async with mysql_factory() as session:
        operation = await operation_service.reserve_operation(
            session, customer(), action="create_ticket", parameters=OPERATION_PARAMS
        )

    async def submit():
        async with mysql_factory() as session:
            return await ticket_service.create_ticket(
                session, customer(), operation["id"], OPERATION_PARAMS
            )

    first, second = await asyncio.gather(submit(), submit())
    assert first["id"] == second["id"]
    async with mysql_factory() as session:
        tickets = (
            await session.execute(select(Ticket).where(Ticket.user_id == ALICE_ID))
        ).scalars().all()
        events = (
            await session.execute(
                select(TicketEvent).where(TicketEvent.ticket_id == first["id"])
            )
        ).scalars().all()
        assert len(tickets) == 1
        assert len(events) == 1
        stored = await session.get(BusinessOperation, operation["id"])
        assert stored.status == "succeeded"


@pytest.mark.asyncio
async def test_same_operation_key_differs_in_parameters_is_conflict(mysql_factory):
    async with mysql_factory() as session:
        operation = await operation_service.reserve_operation(
            session, customer(), action="create_ticket", parameters=OPERATION_PARAMS
        )
    changed = TicketCreate(
        serial_no=OPERATION_PARAMS.serial_no,
        subject="另一个主题",
        description=OPERATION_PARAMS.description,
    )
    async with mysql_factory() as session:
        with pytest.raises(Conflict) as error:
            await ticket_service.create_ticket(session, customer(), operation["id"], changed)
    assert error.value.code == "OPERATION_PARAMETER_MISMATCH"


@pytest.mark.asyncio
async def test_concurrent_device_registration_cannot_exceed_purchase_quantity(mysql_factory):
    def payload(serial: str) -> DeviceCreate:
        return DeviceCreate(
            serial_no=serial,
            order_item_id="itest-item-1",
            model_code="NX100",
            firmware_version="1.6.0",
            purchased_on=date(2026, 2, 1),
            warranty_until=date(2027, 2, 1),
        )

    async def register(serial: str):
        async with mysql_factory() as session:
            return await device_service.register_device(
                session, admin(), payload(serial)
            )

    results = await asyncio.gather(
        register("ITEST-SN-A"), register("ITEST-SN-B"), return_exceptions=True
    )
    successes = [value for value in results if isinstance(value, dict)]
    errors = [value for value in results if isinstance(value, Conflict)]
    assert len(successes) == 1 and len(errors) == 1
    assert errors[0].code == "DEVICE_QUANTITY_EXCEEDED"
    async with mysql_factory() as session:
        assert (
            await session.execute(
                select(func.count())
                .select_from(Device)
                .where(Device.order_item_id == "itest-item-1")
            )
        ).scalar_one() == 2


@pytest.mark.asyncio
async def test_concurrent_processing_has_one_version_winner(mysql_factory):
    async with mysql_factory() as session:
        operation = await operation_service.reserve_operation(
            session, customer(), action="create_ticket", parameters=OPERATION_PARAMS
        )
        ticket = await ticket_service.create_ticket(
            session, customer(), operation["id"], OPERATION_PARAMS
        )
    ticket_id = ticket["id"]

    async def process(message: str):
        async with mysql_factory() as session:
            return await ticket_service.add_ticket_event(
                session,
                admin(),
                ticket_id,
                TicketEventCreate(expected_version=1, message=message),
            )

    results = await asyncio.gather(process("并发 A"), process("并发 B"), return_exceptions=True)
    successes = [value for value in results if isinstance(value, dict)]
    errors = [value for value in results if isinstance(value, ConcurrentUpdate)]
    assert len(successes) == 1 and len(errors) == 1
    async with mysql_factory() as session:
        stored = await session.get(Ticket, ticket_id)
        assert stored.version == 2 and stored.status == "open"
        assert (
            await session.execute(
                select(func.count())
                .select_from(TicketEvent)
                .where(TicketEvent.ticket_id == ticket_id)
            )
        ).scalar_one() == 2
