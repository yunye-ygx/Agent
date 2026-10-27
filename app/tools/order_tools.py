"""Real order tools backed by the same business services as the HTTP pages."""

from __future__ import annotations

from pydantic import Field

from app.business import orders as service
from app.business.errors import BusinessError
from app.tool_runtime import ExecutionContext, ToolArguments, ToolRejected
from app.tools.base import load_principal, read_session


class ListOrdersArguments(ToolArguments):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class QueryOrderArguments(ToolArguments):
    order_no: str = Field(min_length=1, max_length=64)


async def list_orders(arguments: ListOrdersArguments, context: ExecutionContext) -> dict:
    async with read_session() as session:
        principal = await load_principal(session, context)
        items, total = await service.list_orders(
            session, principal, page=arguments.page, page_size=arguments.page_size
        )
        return {
            "items": items,
            "total": total,
            "page": arguments.page,
            "page_size": arguments.page_size,
        }


async def query_order(arguments: QueryOrderArguments, context: ExecutionContext) -> dict:
    async with read_session() as session:
        principal = await load_principal(session, context)
        try:
            return await service.get_order_by_no(session, principal, arguments.order_no)
        except BusinessError as exc:
            raise ToolRejected(exc.code, exc.message) from exc
