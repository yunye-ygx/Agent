from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.business import orders as service
from app.business.errors import Conflict, Forbidden, InvalidRequest, NotFound, Unavailable
from app.business.models import BusinessUser, Device, Order, OrderItem
from app.business.principal import Principal
from app.business.schemas import OrderCreate


async def seed_users(db):
    admin = BusinessUser(
        username="admin", display_name="管理员", password_hash="unused", role="admin"
    )
    alice = BusinessUser(
        username="alice", display_name="爱丽丝", password_hash="unused", role="customer"
    )
    bob = BusinessUser(
        username="bob", display_name="鲍勃", password_hash="unused", role="customer"
    )
    for user in (admin, alice, bob):
        db.add(user)
    await db.commit()
    return (
        Principal.for_user(
            user_id=admin.id, username="admin", display_name="管理员", role="admin"
        ),
        Principal.for_user(
            user_id=alice.id, username="alice", display_name="爱丽丝", role="customer"
        ),
        Principal.for_user(
            user_id=bob.id, username="bob", display_name="鲍勃", role="customer"
        ),
        alice,
    )


def order_payload(user_id: str, order_no: str = "SO-2026-001", **overrides):
    payload = {
        "order_no": order_no,
        "user_id": user_id,
        "items": [
            {
                "product_name": "澄印 NX100 标签打印机",
                "product_model": "NX100",
                "quantity": 2,
                "unit_price": "1299.50",
            }
        ],
    }
    payload.update(overrides)
    return OrderCreate(**payload)


@pytest.mark.asyncio
async def test_order_totals_are_decimal_and_persisted(business_db):
    admin, _alice, _bob, alice = await seed_users(business_db)
    payload = OrderCreate(
        order_no=" so-1 ",
        user_id=alice.id,
        items=[
            {
                "product_name": "NX100",
                "product_model": "NX100",
                "quantity": 3,
                "unit_price": "19.90",
            },
            {
                "product_name": "NX200",
                "product_model": "NX200",
                "quantity": 2,
                "unit_price": "100.00",
            },
        ],
    )
    result = await service.create_order(business_db, admin, payload)
    assert result["order_no"] == "SO-1"
    assert result["total_amount"] == "259.70"
    assert [item["line_amount"] for item in result["items"]] == ["59.70", "200.00"]
    assert (
        await business_db.execute(select(func.count()).select_from(OrderItem))
    ).scalar_one() == 2


@pytest.mark.asyncio
async def test_duplicate_order_no_rolls_back_the_whole_order(business_db):
    admin, _alice, _bob, alice = await seed_users(business_db)
    first = order_payload(alice.id)
    await service.create_order(business_db, admin, first)
    duplicate = order_payload(alice.id)
    with pytest.raises(Conflict) as error:
        await service.create_order(business_db, admin, duplicate)
    assert error.value.code == "ORDER_NO_TAKEN"
    assert (await business_db.execute(select(func.count()).select_from(Order))).scalar_one() == 1
    assert (
        await business_db.execute(select(func.count()).select_from(OrderItem))
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_invalid_money_and_quantity_are_rejected_before_writing(business_db):
    _admin, _alice, _bob, alice = await seed_users(business_db)
    base = {
        "product_name": "NX100",
        "product_model": "NX100",
        "quantity": 1,
        "unit_price": "1.00",
    }
    for bad in (
        {**base, "unit_price": 1.1},
        {**base, "unit_price": "1.001"},
        {**base, "unit_price": "-1.00"},
        {**base, "quantity": 0},
        {**base, "quantity": 1001},
    ):
        with pytest.raises(ValidationError):
            OrderCreate(order_no="SO-X", user_id=alice.id, items=[bad])
    too_many = [{**base, "product_name": f"item-{index}"} for index in range(51)]
    with pytest.raises(ValidationError):
        OrderCreate(order_no="SO-X", user_id=alice.id, items=too_many)


@pytest.mark.asyncio
async def test_order_over_maximum_total_is_rejected_without_rows(business_db):
    admin, _alice, _bob, alice = await seed_users(business_db)
    payload = order_payload(
        alice.id,
        items=[
            {
                "product_name": "NX200",
                "product_model": "NX200",
                "quantity": 1000,
                "unit_price": "9999999999.99",
            }
        ],
    )
    with pytest.raises(InvalidRequest):
        await service.create_order(business_db, admin, payload)
    assert (await business_db.execute(select(func.count()).select_from(Order))).scalar_one() == 0


@pytest.mark.asyncio
async def test_failed_commit_leaves_no_half_order(business_db, monkeypatch):
    admin, _alice, _bob, alice = await seed_users(business_db)

    async def broken_commit():
        raise SQLAlchemyError("simulated connection failure")

    monkeypatch.setattr(business_db, "commit", broken_commit)
    with pytest.raises(Unavailable):
        await service.create_order(business_db, admin, order_payload(alice.id))
    assert (await business_db.execute(select(func.count()).select_from(Order))).scalar_one() == 0
    assert (
        await business_db.execute(select(func.count()).select_from(OrderItem))
    ).scalar_one() == 0


@pytest.mark.asyncio
async def test_list_and_detail_are_scoped_by_principal(business_db):
    admin, alice, bob, alice_row = await seed_users(business_db)
    await service.create_order(business_db, admin, order_payload(alice_row.id, "SO-A"))
    await service.create_order(
        business_db, admin, order_payload(bob.user_id, "SO-B")
    )
    alice_items, alice_total = await service.list_orders(business_db, alice)
    assert alice_total == 1 and alice_items[0]["order_no"] == "SO-A"
    _admin_items, admin_total = await service.list_orders(business_db, admin)
    assert admin_total == 2
    other = (
        await business_db.execute(select(Order).where(Order.order_no == "SO-B"))
    ).scalar_one()
    with pytest.raises(NotFound):
        await service.get_order(business_db, alice, other.id)
    assert (await service.get_order(business_db, admin, other.id))["order_no"] == "SO-B"
    by_no = await service.get_order_by_no(business_db, alice, " so-a ")
    assert by_no["order_no"] == "SO-A"
    with pytest.raises(NotFound):
        await service.get_order_by_no(business_db, alice, "SO-B")


@pytest.mark.asyncio
async def test_customers_cannot_write_orders(business_db):
    _admin, alice, _bob, alice_row = await seed_users(business_db)
    with pytest.raises(Forbidden):
        await service.create_order(
            business_db, alice, order_payload(alice_row.id, "SO-NOPE")
        )


@pytest.mark.asyncio
async def test_order_status_transitions_and_device_guard(business_db):
    admin, _alice, _bob, alice = await seed_users(business_db)
    result = await service.create_order(business_db, admin, order_payload(alice.id, "SO-S"))
    order_id = result["id"]
    completed = await service.update_order_status(business_db, admin, order_id, "completed")
    assert completed["status"] == "completed"
    with pytest.raises(Conflict):
        await service.update_order_status(business_db, admin, order_id, "recorded")

    second = await service.create_order(business_db, admin, order_payload(alice.id, "SO-D"))
    item = (
        await business_db.execute(
            select(OrderItem).where(OrderItem.order_id == second["id"])
        )
    ).scalar_one()
    business_db.add(
        Device(
            serial_no="NX100-GUARD",
            user_id=alice.id,
            order_item_id=item.id,
            model_code="NX100",
            purchased_on=date(2026, 1, 1),
            warranty_until=date(2027, 1, 1),
            status="active",
        )
    )
    await business_db.commit()
    with pytest.raises(Conflict) as error:
        await service.update_order_status(business_db, admin, second["id"], "cancelled")
    assert error.value.code == "ORDER_HAS_DEVICES"

    third = await service.create_order(business_db, admin, order_payload(alice.id, "SO-C"))
    assert (
        await service.update_order_status(business_db, admin, third["id"], "cancelled")
    )["status"] == "cancelled"
    assert Decimal(third["total_amount"]) == Decimal("2599.00")
