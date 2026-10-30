import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from test_devices import create_order_with_quantity, device_payload
from test_orders import seed_users

from app.business import operations as operation_service
from app.business import tickets as ticket_service
from app.business.devices import register_device
from app.business.errors import Conflict, NotFound, OperationUnknown
from app.business.models import BusinessOperation, Device, Ticket, TicketEvent
from app.business.schemas import TicketCreate
from app.business.utils import utc_now


async def setup_environment(db, quantity=2, serial="SN-TICKET-1"):
    admin, alice, bob, alice_row = await seed_users(db)
    order = await create_order_with_quantity(
        db, admin, alice_row.id, "SO-TICKET", quantity=quantity
    )
    device = await register_device(
        db, admin, device_payload(order["items"][0]["id"], serial)
    )
    return admin, alice, bob, device


def ticket_params(serial="SN-TICKET-1", subject="NX100 报 E17", description="上盖已清理仍报错"):
    return TicketCreate(serial_no=serial, subject=subject, description=description)


async def reserve(db, principal, params=None):
    return await operation_service.reserve_operation(
        db, principal, action="create_ticket", parameters=params or ticket_params()
    )


@pytest.mark.asyncio
async def test_reserve_and_create_are_idempotent(business_db):
    _admin, alice, _bob, _device = await setup_environment(business_db)
    params = ticket_params()
    operation = await reserve(business_db, alice, params)
    assert operation["status"] == "reserved"
    first = await ticket_service.create_ticket(
        business_db, alice, operation["id"], params
    )
    second = await ticket_service.create_ticket(
        business_db, alice, operation["id"], params
    )
    assert first["id"] == second["id"]
    assert first["customer_username"] == "alice"
    assert first["status"] == "open" and first["version"] == 1
    assert (
        await business_db.execute(select(func.count()).select_from(Ticket))
    ).scalar_one() == 1
    assert (
        await business_db.execute(select(func.count()).select_from(TicketEvent))
    ).scalar_one() == 1
    stored = (
        await business_db.execute(
            select(BusinessOperation).where(BusinessOperation.id == operation["id"])
        )
    ).scalar_one()
    assert stored.status == "succeeded" and stored.result_resource_id == first["id"]


@pytest.mark.asyncio
async def test_same_key_with_different_parameters_is_rejected(business_db):
    _admin, alice, _bob, _device = await setup_environment(business_db)
    params = ticket_params()
    operation = await reserve(business_db, alice, params)
    changed = ticket_params(subject="完全不同的主题")
    with pytest.raises(Conflict) as error:
        await ticket_service.create_ticket(business_db, alice, operation["id"], changed)
    assert error.value.code == "OPERATION_PARAMETER_MISMATCH"
    assert (
        await business_db.execute(select(func.count()).select_from(Ticket))
    ).scalar_one() == 0


@pytest.mark.asyncio
async def test_expired_reservation_refuses_execution(business_db):
    _admin, alice, _bob, _device = await setup_environment(business_db)
    params = ticket_params()
    operation = await reserve(business_db, alice, params)
    stored = (
        await business_db.execute(
            select(BusinessOperation).where(BusinessOperation.id == operation["id"])
        )
    ).scalar_one()
    stored.expires_at = utc_now().replace(year=2020)
    await business_db.commit()
    with pytest.raises(Conflict) as error:
        await ticket_service.create_ticket(business_db, alice, operation["id"], params)
    assert error.value.code == "OPERATION_EXPIRED"
    queried = await operation_service.get_operation(business_db, alice, operation["id"])
    assert queried["expired"] is True


@pytest.mark.asyncio
async def test_unknown_commit_outcome_is_reconcilable_with_the_same_key(
    business_db, monkeypatch
):
    _admin, alice, _bob, _device = await setup_environment(business_db)
    params = ticket_params()
    operation = await reserve(business_db, alice, params)
    original_commit = business_db.commit

    async def broken_commit():
        raise SQLAlchemyError("simulated lost connection")

    monkeypatch.setattr(business_db, "commit", broken_commit)
    with pytest.raises(OperationUnknown):
        await ticket_service.create_ticket(business_db, alice, operation["id"], params)
    monkeypatch.setattr(business_db, "commit", original_commit)

    queried = await operation_service.get_operation(business_db, alice, operation["id"])
    assert queried["status"] == "reserved"
    assert queried["expired"] is False
    created = await ticket_service.create_ticket(business_db, alice, operation["id"], params)
    assert created["ticket_no"].startswith("T")
    assert (
        await business_db.execute(select(func.count()).select_from(Ticket))
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_reservation_rejects_another_users_device_and_retired_devices(business_db):
    _admin, alice, bob, device = await setup_environment(business_db)
    with pytest.raises(NotFound):
        await operation_service.reserve_operation(
            business_db, bob, action="create_ticket", parameters=ticket_params()
        )
    row = (
        await business_db.execute(select(Device).where(Device.id == device["id"]))
    ).scalar_one()
    row.status = "retired"
    await business_db.commit()
    with pytest.raises(Conflict) as error:
        await operation_service.reserve_operation(
            business_db,
            alice,
            action="create_ticket",
            parameters=ticket_params(),
        )
    assert error.value.code == "DEVICE_RETIRED"


@pytest.mark.asyncio
async def test_operation_is_private_to_its_owner(business_db):
    _admin, alice, bob, _device = await setup_environment(business_db)
    operation = await reserve(business_db, alice)
    with pytest.raises(NotFound):
        await operation_service.get_operation(business_db, bob, operation["id"])
