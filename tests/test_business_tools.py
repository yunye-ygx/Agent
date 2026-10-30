from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from test_business_operations import setup_environment, ticket_params

from app.business import operations as operation_service
from app.business.models import Ticket
from app.business.schemas import TicketCreate
from app.tool_runtime import ExecutionContext
from app.tools import base, catalog


def tool_context(principal, *, operation_id=None, permissions=None):
    return ExecutionContext(
        user_id=principal.user_id,
        permissions=permissions if permissions is not None else principal.permissions,
        operation_id=operation_id,
    )


@pytest.fixture
def bind_tools(business_db, monkeypatch):
    @asynccontextmanager
    async def factory():
        yield business_db

    monkeypatch.setattr(base, "session_factory", factory)
    return business_db


async def execute(name, arguments, context):
    return await catalog.runtime.execute(
        {"call_id": "call-1", "name": name, "arguments": arguments}, context
    )


def test_directory_is_generated_from_the_same_argument_models():
    from app.tools.catalog import TOOL_SPECS

    assert {spec.name for spec in TOOL_SPECS} == {
        "list_orders",
        "query_order",
        "list_devices",
        "query_device",
        "list_tickets",
        "query_ticket",
        "create_ticket",
        "search_knowledge",
    }
    from app.business.principal import Principal

    alice = Principal.for_user(
        user_id="u", username="alice", display_name="爱丽丝", role="customer"
    )
    directory = catalog.tool_directory(alice)
    assert {item["name"] for item in directory} == {spec.name for spec in TOOL_SPECS}
    create = next(item for item in directory if item["name"] == "create_ticket")
    assert set(create["parameters"]["properties"]) == {
        "serial_no",
        "subject",
        "description",
    }
    assert "operation_id" not in create["parameters"]["properties"]
    assert "user_id" not in create["parameters"]["properties"]
    nobody = Principal(
        user_id="u2",
        username="nobody",
        display_name="无权限",
        role="customer",
        permissions=frozenset(),
    )
    assert catalog.tool_directory(nobody) == []


@pytest.mark.asyncio
async def test_read_tools_query_real_business_rows(bind_tools):
    _admin, alice, _bob, _device = await setup_environment(bind_tools)
    context = tool_context(alice)

    orders = await execute("list_orders", {"page": 1, "page_size": 20}, context)
    assert orders.status == "success"
    assert orders.data["total"] == 1
    assert orders.data["items"][0]["order_no"] == "SO-TICKET"

    order = await execute("query_order", {"order_no": "so-ticket"}, context)
    assert order.status == "success"
    assert order.data["order_no"] == "SO-TICKET"
    assert order.data["items"][0]["unit_price"] == "1000.00"

    devices = await execute("list_devices", {}, context)
    assert devices.status == "success" and devices.data["total"] == 1
    device = await execute("query_device", {"serial_no": "sn-ticket-1"}, context)
    assert device.status == "success"
    assert device.data["warranty_status"] in {"in_warranty", "out_of_warranty"}

    tickets = await execute("list_tickets", {}, context)
    assert tickets.status == "success" and tickets.data["total"] == 0


@pytest.mark.asyncio
async def test_cross_user_and_forged_identity_are_rejected_before_reads(bind_tools):
    _admin, alice, bob, _device = await setup_environment(bind_tools)
    denied = await execute("query_order", {"order_no": "SO-TICKET"}, tool_context(bob))
    assert denied.status == "rejected" and denied.code == "NOT_FOUND"
    assert denied.data is None

    forged = await execute(
        "list_orders",
        {"page": 1, "page_size": 20, "user_id": alice.user_id},
        tool_context(alice),
    )
    assert forged.status == "rejected" and forged.code == "INVALID_ARGUMENTS"
    assert forged.attempts == 0


@pytest.mark.asyncio
async def test_create_ticket_requires_envelope_and_is_idempotent(bind_tools):
    _admin, alice, _bob, _device = await setup_environment(bind_tools)
    params = ticket_params()
    missing = await execute(
        "create_ticket",
        {"serial_no": params.serial_no, "subject": params.subject, "description": params.description},
        tool_context(alice),
    )
    assert missing.status == "rejected" and missing.code == "OPERATION_ID_REQUIRED"
    assert missing.attempts == 0

    operation = await operation_service.reserve_operation(
        bind_tools, alice, action="create_ticket", parameters=params
    )
    context = tool_context(alice, operation_id=operation["id"])
    arguments = {
        "serial_no": params.serial_no,
        "subject": params.subject,
        "description": params.description,
    }
    first = await execute("create_ticket", arguments, context)
    second = await execute("create_ticket", arguments, context)
    assert first.status == second.status == "success"
    assert first.data["id"] == second.data["id"]
    assert first.operation_id == second.operation_id == operation["id"]
    assert (
        await bind_tools.execute(select(func.count()).select_from(Ticket))
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_same_operation_key_with_other_arguments_is_rejected(bind_tools):
    _admin, alice, _bob, _device = await setup_environment(bind_tools)
    params = ticket_params()
    operation = await operation_service.reserve_operation(
        bind_tools, alice, action="create_ticket", parameters=params
    )
    result = await execute(
        "create_ticket",
        {
            "serial_no": params.serial_no,
            "subject": "另一个主题",
            "description": params.description,
        },
        tool_context(alice, operation_id=operation["id"]),
    )
    assert result.status == "rejected"
    assert result.code == "OPERATION_PARAMETER_MISMATCH"
    assert (
        await bind_tools.execute(select(func.count()).select_from(Ticket))
    ).scalar_one() == 0


@pytest.mark.asyncio
async def test_ambiguous_write_outcome_returns_reconcile_with_original_key(
    bind_tools, monkeypatch
):
    _admin, alice, _bob, _device = await setup_environment(bind_tools)
    params = TicketCreate(serial_no="SN-TICKET-1", subject="超时", description="结果未知")
    operation = await operation_service.reserve_operation(
        bind_tools, alice, action="create_ticket", parameters=params
    )
    original_commit = bind_tools.commit

    async def broken_commit():
        raise SQLAlchemyError("lost connection")

    monkeypatch.setattr(bind_tools, "commit", broken_commit)
    result = await execute(
        "create_ticket",
        {"serial_no": params.serial_no, "subject": params.subject, "description": params.description},
        tool_context(alice, operation_id=operation["id"]),
    )
    assert result.status == "unknown"
    assert result.next_action == "reconcile"
    assert result.operation_id == operation["id"]

    monkeypatch.setattr(bind_tools, "commit", original_commit)
    queried = await operation_service.get_operation(bind_tools, alice, operation["id"])
    assert queried["status"] == "reserved"
    retried = await execute(
        "create_ticket",
        {"serial_no": params.serial_no, "subject": params.subject, "description": params.description},
        tool_context(alice, operation_id=operation["id"]),
    )
    assert retried.status == "success"


@pytest.mark.asyncio
async def test_search_knowledge_tool_reuses_open_flag_and_hides_internal_fields(
    knowledge_db, monkeypatch
):
    from test_support_knowledge import FakeStore, hit, install_fakes, seed_environment

    _admin, alice, doc, _run = await seed_environment(knowledge_db)
    store = FakeStore([hit()])
    install_fakes(monkeypatch, store)

    @asynccontextmanager
    async def factory():
        yield knowledge_db

    monkeypatch.setattr(base, "session_factory", factory)
    result = await execute(
        "search_knowledge",
        {"query": "E17", "top_k": 3},
        tool_context(alice),
    )
    assert result.status == "success"
    assert result.data["results"][0]["document_title"] == "NX100 产品手册.md"
    assert "task-secret" not in str(result.data)

    # 型号不再是工具参数；未开放的文档即使被召回也会被剔除。
    invalid = await execute(
        "search_knowledge",
        {"query": "E17", "model_code": "NX100", "top_k": 3},
        tool_context(alice),
    )
    assert invalid.status == "rejected" and invalid.code == "INVALID_ARGUMENTS"

    doc.support_open = False
    await knowledge_db.commit()
    closed = await execute(
        "search_knowledge",
        {"query": "E17", "top_k": 3},
        tool_context(alice),
    )
    assert closed.status == "rejected" and closed.code == "NOT_FOUND"
