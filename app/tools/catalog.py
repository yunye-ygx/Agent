"""Trusted tool registry shared by the runtime and the debugging page."""

from __future__ import annotations

from app.business.principal import Principal
from app.tool_runtime import ToolRuntime, ToolSpec
from app.tools import device_tools, knowledge_tools, order_tools, ticket_tools

TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="list_orders",
        arguments_model=order_tools.ListOrdersArguments,
        handler=order_tools.list_orders,
        permission="orders:read",
        authorize=lambda arguments, context: True,
        timeout_seconds=5.0,
        max_attempts=2,
        description="查看当前身份可访问的订单摘要。",
    ),
    ToolSpec(
        name="query_order",
        arguments_model=order_tools.QueryOrderArguments,
        handler=order_tools.query_order,
        permission="orders:read",
        authorize=lambda arguments, context: True,
        timeout_seconds=5.0,
        max_attempts=2,
        description="按订单号查看订单明细；越权订单返回未找到。",
    ),
    ToolSpec(
        name="list_devices",
        arguments_model=device_tools.ListDevicesArguments,
        handler=device_tools.list_devices,
        permission="devices:read",
        authorize=lambda arguments, context: True,
        timeout_seconds=5.0,
        max_attempts=2,
        description="查看当前身份可访问的打印机设备摘要。",
    ),
    ToolSpec(
        name="query_device",
        arguments_model=device_tools.QueryDeviceArguments,
        handler=device_tools.query_device,
        permission="devices:read",
        authorize=lambda arguments, context: True,
        timeout_seconds=5.0,
        max_attempts=2,
        description="按设备编号查看型号、固件、保修状态与关联订单。",
    ),
    ToolSpec(
        name="list_tickets",
        arguments_model=ticket_tools.ListTicketsArguments,
        handler=ticket_tools.list_tickets,
        permission="tickets:read",
        authorize=lambda arguments, context: True,
        timeout_seconds=5.0,
        max_attempts=2,
        description="查看当前身份可访问的工单摘要，可按状态筛选。",
    ),
    ToolSpec(
        name="query_ticket",
        arguments_model=ticket_tools.QueryTicketArguments,
        handler=ticket_tools.query_ticket,
        permission="tickets:read",
        authorize=lambda arguments, context: True,
        timeout_seconds=5.0,
        max_attempts=2,
        description="按工单编号查看工单及分页处理记录。",
    ),
    ToolSpec(
        name="create_ticket",
        arguments_model=ticket_tools.CreateTicketArguments,
        handler=ticket_tools.create_ticket,
        permission="tickets:write",
        authorize=lambda arguments, context: True,
        effect="write",
        timeout_seconds=10.0,
        max_attempts=1,
        description="为当前身份有权访问的设备创建真实工单；必须携带后端操作键。",
    ),
    ToolSpec(
        name="search_knowledge",
        arguments_model=knowledge_tools.SearchKnowledgeArguments,
        handler=knowledge_tools.search_knowledge,
        permission="knowledge:search",
        authorize=lambda arguments, context: True,
        timeout_seconds=20.0,
        max_attempts=1,
        description=(
            "在已开放的售后资料中检索片段，只返回 support_open 且已发布的文档。"
            "不按型号过滤：调用前请先用 query_device 确认设备型号，或在信息不足时向用户澄清。"
        ),
    ),
)

runtime = ToolRuntime(TOOL_SPECS)


def tool_directory(principal: Principal) -> list[dict]:
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "permission": spec.permission,
            "effect": spec.effect,
            "timeout_seconds": spec.timeout_seconds,
            "max_attempts": spec.max_attempts,
            "parameters": spec.arguments_model.model_json_schema(),
        }
        for spec in TOOL_SPECS
        if spec.permission in principal.permissions
    ]
