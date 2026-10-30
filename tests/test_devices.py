from datetime import timedelta

import pytest
from pydantic import ValidationError
from test_orders import order_payload, seed_users

from app.business import devices as service
from app.business import orders as order_service
from app.business.devices import business_today
from app.business.errors import Conflict, InvalidRequest, NotFound
from app.business.schemas import DeviceCreate


async def create_order_with_quantity(db, admin, customer_id, order_no, quantity=2):
    return await order_service.create_order(
        db,
        admin,
        order_payload(
            customer_id,
            order_no,
            items=[
                {
                    "product_name": "澄印 NX100",
                    "product_model": "NX100",
                    "quantity": quantity,
                    "unit_price": "1000.00",
                }
            ],
        ),
    )


def device_payload(item_id, serial_no="NX100-A1", **overrides):
    today = business_today()
    payload = {
        "serial_no": serial_no,
        "order_item_id": item_id,
        "model_code": "NX100",
        "firmware_version": "1.6.0",
        "purchased_on": (today - timedelta(days=30)).isoformat(),
        "warranty_until": (today + timedelta(days=335)).isoformat(),
    }
    payload.update(overrides)
    return DeviceCreate(**payload)


@pytest.mark.asyncio
async def test_registration_derives_owner_and_enforces_purchased_quantity(business_db):
    admin, alice, _bob, alice_row = await seed_users(business_db)
    order = await create_order_with_quantity(
        business_db, admin, alice_row.id, "SO-DEV", quantity=2
    )
    item_id = order["items"][0]["id"]
    first = await service.register_device(
        business_db, admin, device_payload(item_id, "nx100-a1")
    )
    assert first["serial_no"] == "NX100-A1"
    assert first["customer_username"] == "alice"
    assert first["warranty_active"] is True
    await service.register_device(business_db, admin, device_payload(item_id, "NX100-A2"))
    with pytest.raises(Conflict) as error:
        await service.register_device(
            business_db, admin, device_payload(item_id, "NX100-A3")
        )
    assert error.value.code == "DEVICE_QUANTITY_EXCEEDED"

    own = await service.get_device(business_db, alice, first["id"])
    assert own["serial_no"] == "NX100-A1"
    assert (await service.list_devices(business_db, alice))[1] == 2


@pytest.mark.asyncio
async def test_device_model_must_match_order_item(business_db):
    admin, _alice, _bob, alice_row = await seed_users(business_db)
    order = await create_order_with_quantity(business_db, admin, alice_row.id, "SO-M")
    with pytest.raises(InvalidRequest):
        await service.register_device(
            business_db,
            admin,
            device_payload(order["items"][0]["id"], "NX100-M1", model_code="NX200"),
        )


@pytest.mark.asyncio
async def test_duplicate_serial_is_case_insensitive_and_rejected(business_db):
    admin, _alice, _bob, alice_row = await seed_users(business_db)
    first = await create_order_with_quantity(business_db, admin, alice_row.id, "SO-S1")
    second = await create_order_with_quantity(business_db, admin, alice_row.id, "SO-S2")
    await service.register_device(
        business_db, admin, device_payload(first["items"][0]["id"], "SN-DUP")
    )
    with pytest.raises(Conflict) as error:
        await service.register_device(
            business_db, admin, device_payload(second["items"][0]["id"], "sn-dup")
        )
    assert error.value.code == "SERIAL_NO_TAKEN"


@pytest.mark.asyncio
async def test_invalid_dates_and_forged_owner_fields_are_rejected():
    with pytest.raises(ValidationError):
        DeviceCreate(
            serial_no="SN-1",
            order_item_id="item",
            model_code="NX100",
            purchased_on="2026-10-10",
            warranty_until="2026-10-09",
        )
    with pytest.raises(ValidationError):
        DeviceCreate(
            serial_no="SN-1",
            order_item_id="item",
            model_code="NX100",
            purchased_on="2026-10-10",
            warranty_until="2027-10-10",
            user_id="forged",
        )


@pytest.mark.asyncio
async def test_warranty_end_date_is_inclusive_in_business_timezone(business_db):
    admin, alice, _bob, alice_row = await seed_users(business_db)
    order = await create_order_with_quantity(
        business_db, admin, alice_row.id, "SO-W", quantity=2
    )
    item_id = order["items"][0]["id"]
    today = business_today()
    active = await service.register_device(
        business_db,
        admin,
        device_payload(
            item_id,
            "SN-TODAY",
            purchased_on=(today - timedelta(days=365)).isoformat(),
            warranty_until=today.isoformat(),
        ),
    )
    expired = await service.register_device(
        business_db,
        admin,
        device_payload(
            item_id,
            "SN-YESTERDAY",
            purchased_on=(today - timedelta(days=366)).isoformat(),
            warranty_until=(today - timedelta(days=1)).isoformat(),
        ),
    )
    assert active["warranty_status"] == "in_warranty"
    assert expired["warranty_status"] == "out_of_warranty"
    assert (await service.list_devices(business_db, alice))[1] == 2


@pytest.mark.asyncio
async def test_device_detail_is_scoped(business_db):
    admin, _alice, bob, alice_row = await seed_users(business_db)
    order = await create_order_with_quantity(business_db, admin, alice_row.id, "SO-P")
    device = await service.register_device(
        business_db, admin, device_payload(order["items"][0]["id"], "SN-PRIVATE")
    )
    with pytest.raises(NotFound):
        await service.get_device(business_db, bob, device["id"])
    admin_items, total = await service.list_devices(business_db, admin)
    assert total == 1 and admin_items[0]["customer_username"] == "alice"
