from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_principal, require_admin, require_csrf
from app.business import devices as service
from app.business.principal import Principal
from app.business.schemas import DeviceCreate, DevicePage, DeviceSummary
from app.db.mysql_session import get_mysql_session

router = APIRouter(prefix="/api/v1/devices", tags=["devices"])


@router.get("", response_model=DevicePage)
async def list_devices(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_mysql_session),
) -> DevicePage:
    items, total = await service.list_devices(
        session, principal, page=page, page_size=page_size
    )
    return DevicePage(items=items, total=total, page=page, page_size=page_size)


@router.post("", response_model=DeviceSummary, status_code=201)
async def register_device(
    payload: DeviceCreate,
    principal: Principal = Depends(require_admin),
    _csrf: Principal = Depends(require_csrf),
    session: AsyncSession = Depends(get_mysql_session),
) -> DeviceSummary:
    return DeviceSummary(**await service.register_device(session, principal, payload))


@router.get("/{device_id}", response_model=DeviceSummary)
async def get_device(
    device_id: str,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_mysql_session),
) -> DeviceSummary:
    return DeviceSummary(**await service.get_device(session, principal, device_id))
