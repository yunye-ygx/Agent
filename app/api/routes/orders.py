from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_principal, require_admin, require_csrf
from app.business import orders as service
from app.business.principal import Principal
from app.business.schemas import (
    OrderCreate,
    OrderDetail,
    OrderPage,
    OrderStatusUpdate,
)
from app.db.mysql_session import get_mysql_session

router = APIRouter(prefix="/api/v1/orders", tags=["orders"])


@router.get("", response_model=OrderPage)
async def list_orders(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_mysql_session),
) -> OrderPage:
    items, total = await service.list_orders(
        session, principal, page=page, page_size=page_size
    )
    return OrderPage(items=items, total=total, page=page, page_size=page_size)


@router.post("", response_model=OrderDetail, status_code=201)
async def create_order(
    payload: OrderCreate,
    principal: Principal = Depends(require_admin),
    _csrf: Principal = Depends(require_csrf),
    session: AsyncSession = Depends(get_mysql_session),
) -> OrderDetail:
    return OrderDetail(**await service.create_order(session, principal, payload))


@router.get("/by-no/{order_no}", response_model=OrderDetail)
async def get_order_by_no(
    order_no: str,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_mysql_session),
) -> OrderDetail:
    return OrderDetail(**await service.get_order_by_no(session, principal, order_no))


@router.get("/{order_id}", response_model=OrderDetail)
async def get_order(
    order_id: str,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_mysql_session),
) -> OrderDetail:
    return OrderDetail(**await service.get_order(session, principal, order_id))


@router.patch("/{order_id}", response_model=OrderDetail)
async def update_order(
    order_id: str,
    payload: OrderStatusUpdate,
    principal: Principal = Depends(require_admin),
    _csrf: Principal = Depends(require_csrf),
    session: AsyncSession = Depends(get_mysql_session),
) -> OrderDetail:
    return OrderDetail(
        **await service.update_order_status(session, principal, order_id, payload.status)
    )
