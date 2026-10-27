import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from test_business_operations import reserve, setup_environment, ticket_params

from app.business import tickets as service
from app.business.errors import (
    ConcurrentUpdate,
    Conflict,
    Forbidden,
    InvalidRequest,
    NotFound,
)
from app.business.models import Ticket, TicketEvent
from app.business.schemas import TicketEventCreate


async def create_ticket(db, principal, params=None):
    params = params or ticket_params()
    operation = await reserve(db, principal, params)
    return await service.create_ticket(db, principal, operation["id"], params)


@pytest.mark.asyncio
async def test_ticket_list_and_detail_are_scoped(business_db):
    admin, alice, bob, _device = await setup_environment(business_db)
    created = await create_ticket(business_db, alice)
    assert created["status"] == "open" and created["version"] == 1

    alice_items, alice_total = await service.list_tickets(business_db, alice)
    assert alice_total == 1 and alice_items[0]["ticket_no"] == created["ticket_no"]
    bob_items, bob_total = await service.list_tickets(business_db, bob)
    assert bob_total == 0 and bob_items == []
    admin_items, admin_total = await service.list_tickets(business_db, admin)
    assert admin_total == 1 and admin_items[0]["customer_username"] == "alice"

    with pytest.raises(NotFound):
        await service.get_ticket_detail(business_db, bob, created["id"])


@pytest.mark.asyncio
async def test_admin_creates_ticket_for_customer_device_and_owner_comes_from_device(
    business_db,
):
    admin, alice, _bob, _device = await setup_environment(business_db)
    created = await create_ticket(business_db, admin)
    assert created["customer_username"] == "alice"
    own = await service.get_ticket_detail(business_db, alice, created["id"])
    assert own["ticket_no"] == created["ticket_no"]


@pytest.mark.asyncio
async def test_status_transitions_optimistic_lock_and_terminal_state(business_db):
    admin, alice, _bob, _device = await setup_environment(business_db)
    created = await create_ticket(business_db, alice)
    ticket_id = created["id"]

    comment = await service.add_ticket_event(
        business_db,
        admin,
        ticket_id,
        TicketEventCreate(expected_version=1, message="已收到，正在核查"),
    )
    assert comment["version"] == 2
    assert comment["events_total"] == 2
    assert comment["events"][-1]["event_type"] == "comment"

    with pytest.raises(ConcurrentUpdate):
        await service.add_ticket_event(
            business_db,
            admin,
            ticket_id,
            TicketEventCreate(expected_version=1, message="旧版本写入"),
        )

    in_progress = await service.add_ticket_event(
        business_db,
        admin,
        ticket_id,
        TicketEventCreate(expected_version=2, message="开始处理", to_status="in_progress"),
    )
    assert in_progress["status"] == "in_progress" and in_progress["version"] == 3

    reopened = await service.add_ticket_event(
        business_db,
        admin,
        ticket_id,
        TicketEventCreate(expected_version=3, message="缺少信息，退回客户", to_status="open"),
    )
    assert reopened["status"] == "open" and reopened["version"] == 4

    with pytest.raises(Conflict) as error:
        await service.add_ticket_event(
            business_db,
            admin,
            ticket_id,
            TicketEventCreate(expected_version=4, message="直接解决", to_status="resolved"),
        )
    assert error.value.code == "INVALID_TICKET_TRANSITION"

    current = await service.add_ticket_event(
        business_db,
        admin,
        ticket_id,
        TicketEventCreate(expected_version=4, message="重新处理", to_status="in_progress"),
    )
    current = await service.add_ticket_event(
        business_db,
        admin,
        ticket_id,
        TicketEventCreate(expected_version=5, message="已更换配件", to_status="resolved"),
    )
    current = await service.add_ticket_event(
        business_db,
        admin,
        ticket_id,
        TicketEventCreate(expected_version=6, message="客户确认关闭", to_status="closed"),
    )
    assert current["status"] == "closed" and current["version"] == 7

    with pytest.raises(Conflict):
        await service.add_ticket_event(
            business_db,
            admin,
            ticket_id,
            TicketEventCreate(expected_version=7, message="重新打开", to_status="in_progress"),
        )
    closed_comment = await service.add_ticket_event(
        business_db,
        admin,
        ticket_id,
        TicketEventCreate(expected_version=7, message="归档备注"),
    )
    assert closed_comment["version"] == 8


@pytest.mark.asyncio
async def test_customers_cannot_add_processing_events(business_db):
    _admin, alice, _bob, _device = await setup_environment(business_db)
    created = await create_ticket(business_db, alice)
    with pytest.raises(Forbidden):
        await service.add_ticket_event(
            business_db,
            alice,
            created["id"],
            TicketEventCreate(expected_version=1, message="我要自己处理"),
        )


@pytest.mark.asyncio
async def test_events_are_paginated_and_ordered(business_db):
    admin, alice, _bob, _device = await setup_environment(business_db)
    created = await create_ticket(business_db, alice)
    version = 1
    for index in range(3):
        await service.add_ticket_event(
            business_db,
            admin,
            created["id"],
            TicketEventCreate(expected_version=version, message=f"处理记录 {index}"),
        )
        version += 1
    page = await service.get_ticket_detail(
        business_db, alice, created["id"], events_page=2, events_page_size=2
    )
    assert page["events_total"] == 4
    assert [event["message"] for event in page["events"]] == ["处理记录 1", "处理记录 2"]


@pytest.mark.asyncio
async def test_ticket_text_validation_and_status_filter(business_db):
    admin, alice, _bob, _device = await setup_environment(business_db)
    with pytest.raises(ValidationError):
        ticket_params(subject="   ")
    with pytest.raises(ValidationError):
        ticket_params(description="x" * 4001)
    created = await create_ticket(
        business_db, alice, ticket_params(subject="  有空格  ", description=" 描述 ")
    )
    assert created["subject"] == "有空格"
    assert created["description"] == "描述"
    assert (await service.list_tickets(business_db, admin, status="closed"))[1] == 0
    with pytest.raises(InvalidRequest):
        await service.list_tickets(business_db, admin, status="not-a-status")
    assert (
        await business_db.execute(select(func.count()).select_from(TicketEvent))
    ).scalar_one() == 1
    assert (
        await business_db.execute(select(func.count()).select_from(Ticket))
    ).scalar_one() == 1
