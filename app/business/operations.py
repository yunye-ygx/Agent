"""Idempotency reservation/query use-cases for business writes."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.business.errors import InvalidRequest, NotFound, Unavailable
from app.business.models import BusinessOperation
from app.business.principal import Principal
from app.business.schemas import TicketCreate
from app.business.tickets import (
    OPERATION_TTL,
    get_ticket_detail,
    resolve_accessible_device,
    ticket_request_hash,
)
from app.business.utils import as_utc, iso_datetime, new_id, utc_now

SUPPORTED_ACTIONS = ("create_ticket",)


def _serialize(operation: BusinessOperation) -> dict:
    now = utc_now()
    expires_at = as_utc(operation.expires_at)
    return {
        "id": operation.id,
        "action": operation.action,
        "status": operation.status,
        "expires_at": iso_datetime(operation.expires_at),
        "expired": bool(
            operation.status == "reserved" and expires_at is not None and expires_at <= now
        ),
        "result_resource_id": operation.result_resource_id,
        "result": operation.result_snapshot,
        "resource_available": False,
        "created_at": iso_datetime(operation.created_at),
        "completed_at": iso_datetime(operation.completed_at) or None,
    }


async def reserve_operation(
    session: AsyncSession,
    principal: Principal,
    *,
    action: str,
    parameters: TicketCreate,
) -> dict:
    if action not in SUPPORTED_ACTIONS:
        raise InvalidRequest("不支持的操作类型")
    # Validate resource access before issuing a key: a bad operation is never
    # persisted and the caller never receives a reservation it cannot execute.
    await resolve_accessible_device(session, principal, parameters.serial_no)
    operation = BusinessOperation(
        id=new_id(),
        user_id=principal.user_id,
        action=action,
        request_hash=ticket_request_hash(parameters),
        status="reserved",
        expires_at=utc_now() + OPERATION_TTL,
    )
    session.add(operation)
    try:
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        raise Unavailable("操作预留失败，请重试") from exc
    await session.refresh(operation)
    return _serialize(operation)


async def get_operation(
    session: AsyncSession, principal: Principal, operation_id: str
) -> dict:
    operation = (
        await session.execute(
            select(BusinessOperation).where(
                BusinessOperation.id == operation_id,
                BusinessOperation.user_id == principal.user_id,
            )
        )
    ).scalar_one_or_none()
    if operation is None:
        raise NotFound("操作不存在")
    payload = _serialize(operation)
    if operation.result_resource_id:
        try:
            await get_ticket_detail(
                session, principal, operation.result_resource_id, events_page=1, events_page_size=1
            )
        except NotFound:
            payload["resource_available"] = False
        else:
            payload["resource_available"] = True
    return payload
