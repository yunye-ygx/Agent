"""Ticket use-cases, including transactional idempotency with business_operations."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.business.errors import (
    ConcurrentUpdate,
    Conflict,
    Forbidden,
    InvalidRequest,
    NotFound,
    OperationUnknown,
    Unavailable,
)
from app.business.models import (
    BusinessOperation,
    BusinessUser,
    Device,
    Ticket,
    TicketEvent,
)
from app.business.principal import Principal
from app.business.schemas import TICKET_STATUS_VALUES, TicketCreate, TicketEventCreate
from app.business.utils import as_utc, iso_datetime, new_id, utc_now

OPERATION_TTL = timedelta(minutes=30)
ALLOWED_TRANSITIONS = {
    ("open", "in_progress"),
    ("in_progress", "open"),
    ("in_progress", "resolved"),
    ("resolved", "in_progress"),
    ("resolved", "closed"),
}


def ticket_request_hash(payload: TicketCreate) -> str:
    canonical = json.dumps(
        payload.model_dump(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ticket_no() -> str:
    return f"T{utc_now():%Y%m%d}-{uuid4().hex[:10].upper()}"


def _summary(ticket: Ticket, device: Device, user: BusinessUser) -> dict:
    return {
        "id": ticket.id,
        "ticket_no": ticket.ticket_no,
        "subject": ticket.subject,
        "status": ticket.status,
        "version": ticket.version,
        "created_at": iso_datetime(ticket.created_at),
        "updated_at": iso_datetime(ticket.updated_at),
        "device_id": device.id,
        "device_serial_no": device.serial_no,
        "device_model": device.model_code,
        "customer_username": user.username,
        "customer_display_name": user.display_name,
    }


def _base_statement():
    return (
        select(Ticket, Device, BusinessUser)
        .join(Device, Device.id == Ticket.device_id)
        .join(BusinessUser, BusinessUser.id == Ticket.user_id)
    )


def _visible_statement(principal: Principal):
    statement = _base_statement()
    if not principal.is_admin:
        statement = statement.where(Ticket.user_id == principal.user_id)
    return statement


async def resolve_accessible_device(
    session: AsyncSession, principal: Principal, serial_no: str, *, lock: bool = False
) -> Device:
    normalized = serial_no.strip().upper()
    if not normalized:
        raise InvalidRequest("设备编号不能为空")
    statement = select(Device).where(Device.serial_no == normalized)
    if lock:
        statement = statement.with_for_update()
    device = (await session.execute(statement)).scalar_one_or_none()
    if device is None or (not principal.is_admin and device.user_id != principal.user_id):
        raise NotFound("设备不存在")
    if device.status != "active":
        raise Conflict("设备已退役，不能提交新工单", code="DEVICE_RETIRED")
    return device


async def list_tickets(
    session: AsyncSession,
    principal: Principal,
    *,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    if status is not None and status not in TICKET_STATUS_VALUES:
        raise InvalidRequest("工单状态不合法")
    filters = []
    if status:
        filters.append(Ticket.status == status)
    if not principal.is_admin:
        filters.append(Ticket.user_id == principal.user_id)
    total = (
        await session.execute(select(func.count()).select_from(Ticket).where(*filters))
    ).scalar_one()
    rows = (
        await session.execute(
            _visible_statement(principal)
            .where(*filters)
            .order_by(Ticket.created_at.desc(), Ticket.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return [_summary(ticket, device, user) for ticket, device, user in rows], total


async def get_ticket_detail(
    session: AsyncSession,
    principal: Principal,
    ticket_id: str,
    *,
    events_page: int = 1,
    events_page_size: int = 20,
) -> dict:
    row = (await session.execute(_visible_statement(principal).where(Ticket.id == ticket_id))).first()
    if row is None:
        raise NotFound("工单不存在")
    ticket, device, user = row
    total = (
        await session.execute(
            select(func.count()).select_from(TicketEvent).where(TicketEvent.ticket_id == ticket.id)
        )
    ).scalar_one()
    events = (
        await session.execute(
            select(TicketEvent, BusinessUser)
            .join(BusinessUser, BusinessUser.id == TicketEvent.actor_id)
            .where(TicketEvent.ticket_id == ticket.id)
            .order_by(TicketEvent.created_at.asc(), TicketEvent.id.asc())
            .offset((events_page - 1) * events_page_size)
            .limit(events_page_size)
        )
    ).all()
    payload = _summary(ticket, device, user)
    payload.update(
        {
            "description": ticket.description,
            "events": [
                {
                    "id": event.id,
                    "event_type": event.event_type,
                    "from_status": event.from_status,
                    "to_status": event.to_status,
                    "message": event.message,
                    "actor_username": actor.username,
                    "actor_display_name": actor.display_name,
                    "created_at": iso_datetime(event.created_at),
                }
                for event, actor in events
            ],
            "events_total": total,
            "events_page": events_page,
            "events_page_size": events_page_size,
        }
    )
    return payload


async def get_ticket_by_no(
    session: AsyncSession,
    principal: Principal,
    ticket_no: str,
    *,
    events_page: int = 1,
    events_page_size: int = 20,
) -> dict:
    normalized = ticket_no.strip().upper()
    if not normalized:
        raise InvalidRequest("工单编号不能为空")
    ticket = (
        await session.execute(
            _visible_statement(principal).where(Ticket.ticket_no == normalized)
        )
    ).first()
    if ticket is None:
        raise NotFound("工单不存在")
    return await get_ticket_detail(
        session,
        principal,
        ticket[0].id,
        events_page=events_page,
        events_page_size=events_page_size,
    )


async def create_ticket(
    session: AsyncSession,
    principal: Principal,
    operation_id: str,
    payload: TicketCreate,
) -> dict:
    """Create a ticket exactly once for a backend-issued operation key.

    The operation row is locked for the duration of the transaction. A succeeded
    operation returns the original ticket; a reserved one writes ticket, initial
    event and the result credential together.
    """

    now = utc_now()
    operation = (
        await session.execute(
            select(BusinessOperation)
            .where(
                BusinessOperation.id == operation_id,
                BusinessOperation.user_id == principal.user_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if operation is None:
        raise NotFound("操作不存在或已过期")
    if operation.action != "create_ticket":
        raise Conflict("操作类型不匹配", code="OPERATION_ACTION_MISMATCH")
    expected_hash = ticket_request_hash(payload)
    if not hmac.compare_digest(operation.request_hash, expected_hash):
        raise Conflict("操作参数与预留时不一致", code="OPERATION_PARAMETER_MISMATCH")
    if operation.status == "succeeded":
        if not operation.result_resource_id:
            raise OperationUnknown("操作已成功但缺少结果标识", code="OPERATION_RESULT_MISSING")
        return await get_ticket_detail(session, principal, operation.result_resource_id)
    expires_at = as_utc(operation.expires_at)
    if expires_at is None or expires_at <= now:
        raise Conflict("操作已过期，请重新发起", code="OPERATION_EXPIRED")

    device = await resolve_accessible_device(session, principal, payload.serial_no)
    ticket = Ticket(
        id=new_id(),
        ticket_no=_ticket_no(),
        user_id=device.user_id,
        device_id=device.id,
        subject=payload.subject,
        description=payload.description,
        status="open",
        version=1,
    )
    event = TicketEvent(
        id=new_id(),
        ticket_id=ticket.id,
        actor_id=principal.user_id,
        event_type="created",
        from_status=None,
        to_status="open",
        message="工单已创建",
    )
    snapshot = {
        "id": ticket.id,
        "ticket_no": ticket.ticket_no,
        "subject": ticket.subject,
        "status": ticket.status,
        "device_serial_no": device.serial_no,
    }
    session.add(ticket)
    session.add(event)
    operation.status = "succeeded"
    operation.result_resource_id = ticket.id
    operation.result_snapshot = snapshot
    operation.completed_at = now
    try:
        await session.commit()
    except SQLAlchemyError as exc:
        # The server may have committed before the connection failed. Do not
        # report a definite failure: the client must reconcile this same key.
        await session.rollback()
        raise OperationUnknown(
            "工单创建结果尚未确认，请用原操作键查询结果", code="OPERATION_RESULT_UNKNOWN"
        ) from exc
    return await get_ticket_detail(session, principal, ticket.id)


async def add_ticket_event(
    session: AsyncSession,
    principal: Principal,
    ticket_id: str,
    payload: TicketEventCreate,
) -> dict:
    if not principal.is_admin:
        raise Forbidden("只有管理员可以处理工单")
    ticket = (
        await session.execute(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
    ).scalar_one_or_none()
    if ticket is None:
        raise NotFound("工单不存在")
    if ticket.version != payload.expected_version:
        raise ConcurrentUpdate("工单已被其他管理员更新，请刷新后重试")
    from_status = ticket.status
    target = payload.to_status
    if target is not None and target != from_status:
        if (from_status, target) not in ALLOWED_TRANSITIONS:
            raise Conflict("工单状态不允许该变更", code="INVALID_TICKET_TRANSITION")
        ticket.status = target
    ticket.version += 1
    ticket.updated_at = utc_now()
    event = TicketEvent(
        id=new_id(),
        ticket_id=ticket.id,
        actor_id=principal.user_id,
        event_type="status_change" if target is not None and target != from_status else "comment",
        from_status=from_status,
        to_status=target if target is not None else from_status,
        message=payload.message,
    )
    session.add(event)
    try:
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        raise Unavailable("工单处理未保存，请重试") from exc
    return await get_ticket_detail(session, principal, ticket.id)
