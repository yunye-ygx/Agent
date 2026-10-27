from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_principal, require_csrf
from app.business import operations as service
from app.business.principal import Principal
from app.business.schemas import OperationReserve, OperationResponse, TicketCreate
from app.db.mysql_session import get_mysql_session

router = APIRouter(prefix="/api/v1/operations", tags=["operations"])


def _issues(error: ValidationError) -> list[dict]:
    return [
        {"path": list(item["loc"]), "type": item["type"]}
        for item in error.errors(include_input=False, include_context=False, include_url=False)
    ]


@router.post("", response_model=OperationResponse, status_code=201)
async def reserve_operation(
    payload: OperationReserve,
    principal: Principal = Depends(require_csrf),
    session: AsyncSession = Depends(get_mysql_session),
) -> OperationResponse:
    try:
        parameters = TicketCreate(**payload.parameters)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_PARAMETERS",
                "message": "操作参数不合法",
                "issues": _issues(exc),
            },
        ) from exc
    result = await service.reserve_operation(
        session, principal, action=payload.action, parameters=parameters
    )
    return OperationResponse(**result)


@router.get("/{operation_id}", response_model=OperationResponse)
async def get_operation(
    operation_id: str,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_mysql_session),
) -> OperationResponse:
    return OperationResponse(**await service.get_operation(session, principal, operation_id))
