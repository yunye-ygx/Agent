"""Real ticket tools. create_ticket uses the operation envelope for idempotency."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.business import tickets as service
from app.business.errors import BusinessError, OperationUnknown
from app.business.schemas import TicketCreate
from app.tool_runtime import ExecutionContext, ToolArguments, ToolRejected
from app.tools import base
from app.tools.base import load_principal, read_session


class ListTicketsArguments(ToolArguments):
    status: Literal["open", "in_progress", "resolved", "closed"] | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class QueryTicketArguments(ToolArguments):
    ticket_no: str = Field(min_length=1, max_length=32)
    events_page: int = Field(default=1, ge=1)


class CreateTicketArguments(ToolArguments):
    serial_no: str = Field(min_length=1, max_length=64)
    subject: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=4000)


async def list_tickets(arguments: ListTicketsArguments, context: ExecutionContext) -> dict:
    async with read_session() as session:
        principal = await load_principal(session, context)
        items, total = await service.list_tickets(
            session,
            principal,
            status=arguments.status,
            page=arguments.page,
            page_size=arguments.page_size,
        )
        return {
            "items": items,
            "total": total,
            "page": arguments.page,
            "page_size": arguments.page_size,
        }


async def query_ticket(arguments: QueryTicketArguments, context: ExecutionContext) -> dict:
    async with read_session() as session:
        principal = await load_principal(session, context)
        try:
            return await service.get_ticket_by_no(
                session,
                principal,
                arguments.ticket_no,
                events_page=arguments.events_page,
            )
        except BusinessError as exc:
            raise ToolRejected(exc.code, exc.message) from exc


async def create_ticket(arguments: CreateTicketArguments, context: ExecutionContext) -> dict:
    async with base.session_factory() as session:
        principal = await load_principal(session, context)
        try:
            parameters = TicketCreate(
                serial_no=arguments.serial_no,
                subject=arguments.subject,
                description=arguments.description,
            )
            return await service.create_ticket(
                session, principal, context.operation_id or "", parameters
            )
        except OperationUnknown:
            raise
        except BusinessError as exc:
            raise ToolRejected(exc.code, exc.message) from exc
