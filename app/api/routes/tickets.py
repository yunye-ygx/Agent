from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_principal, require_admin, require_csrf
from app.business import tickets as service
from app.business.principal import Principal
from app.business.schemas import (
    TicketCreate,
    TicketCreateRequest,
    TicketDetail,
    TicketEventCreate,
    TicketPage,
)
from app.db.mysql_session import get_mysql_session

router = APIRouter(prefix="/api/v1/tickets", tags=["tickets"])


@router.get("", response_model=TicketPage)
async def list_tickets(
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_mysql_session),
) -> TicketPage:
    items, total = await service.list_tickets(
        session, principal, status=status, page=page, page_size=page_size
    )
    return TicketPage(items=items, total=total, page=page, page_size=page_size)


@router.post("", response_model=TicketDetail, status_code=201)
async def create_ticket(
    payload: TicketCreateRequest,
    principal: Principal = Depends(require_csrf),
    session: AsyncSession = Depends(get_mysql_session),
) -> TicketDetail:
    fields = TicketCreate.model_validate(payload.model_dump(exclude={"operation_id"}))
    return TicketDetail(
        **await service.create_ticket(session, principal, payload.operation_id, fields)
    )


@router.get("/{ticket_id}", response_model=TicketDetail)
async def get_ticket(
    ticket_id: str,
    events_page: int = Query(default=1, ge=1),
    events_page_size: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_mysql_session),
) -> TicketDetail:
    return TicketDetail(
        **await service.get_ticket_detail(
            session,
            principal,
            ticket_id,
            events_page=events_page,
            events_page_size=events_page_size,
        )
    )


@router.post("/{ticket_id}/events", response_model=TicketDetail)
async def add_ticket_event(
    ticket_id: str,
    payload: TicketEventCreate,
    principal: Principal = Depends(require_admin),
    _csrf: Principal = Depends(require_csrf),
    session: AsyncSession = Depends(get_mysql_session),
) -> TicketDetail:
    return TicketDetail(
        **await service.add_ticket_event(session, principal, ticket_id, payload)
    )
