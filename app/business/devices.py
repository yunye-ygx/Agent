"""Device registration and read use-cases. Ownership is derived from the order."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.business.errors import Conflict, Forbidden, InvalidRequest, NotFound, Unavailable
from app.business.models import BusinessUser, Device, Order, OrderItem
from app.business.principal import Principal
from app.business.schemas import DeviceCreate
from app.business.utils import iso_date, new_id

BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")


def business_today(now: datetime | None = None) -> date:
    moment = now or datetime.now(UTC)
    return moment.astimezone(BUSINESS_TIMEZONE).date()


def _serialize(
    device: Device,
    item: OrderItem,
    order: Order,
    user: BusinessUser,
    today: date,
) -> dict:
    warranty_until = device.warranty_until
    active = today <= warranty_until
    return {
        "id": device.id,
        "serial_no": device.serial_no,
        "model_code": device.model_code,
        "firmware_version": device.firmware_version,
        "purchased_on": iso_date(device.purchased_on),
        "warranty_until": iso_date(warranty_until),
        "warranty_status": "in_warranty" if active else "out_of_warranty",
        "warranty_active": active,
        "status": device.status,
        "order_id": order.id,
        "order_no": order.order_no,
        "order_item_id": item.id,
        "product_name": item.product_name,
        "customer_username": user.username,
        "customer_display_name": user.display_name,
    }


def _base_statement():
    return (
        select(Device, OrderItem, Order, BusinessUser)
        .join(OrderItem, OrderItem.id == Device.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .join(BusinessUser, BusinessUser.id == Device.user_id)
    )


async def list_devices(
    session: AsyncSession,
    principal: Principal,
    *,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    filters = []
    if not principal.is_admin:
        filters.append(Device.user_id == principal.user_id)
    total = (
        await session.execute(select(func.count()).select_from(Device).where(*filters))
    ).scalar_one()
    rows = (
        await session.execute(
            _base_statement()
            .where(*filters)
            .order_by(Device.created_at.desc(), Device.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    today = business_today()
    return [
        _serialize(device, item, order, user, today) for device, item, order, user in rows
    ], total


async def _get_visible(
    session: AsyncSession, principal: Principal, condition
) -> tuple[Device, OrderItem, Order, BusinessUser]:
    statement = _base_statement().where(condition)
    if not principal.is_admin:
        statement = statement.where(Device.user_id == principal.user_id)
    row = (await session.execute(statement)).first()
    if row is None:
        raise NotFound("设备不存在")
    return row[0], row[1], row[2], row[3]


async def get_device(session: AsyncSession, principal: Principal, device_id: str) -> dict:
    device, item, order, user = await _get_visible(session, principal, Device.id == device_id)
    return _serialize(device, item, order, user, business_today())


async def get_device_by_serial(
    session: AsyncSession, principal: Principal, serial_no: str
) -> dict:
    normalized = serial_no.strip().upper()
    if not normalized:
        raise InvalidRequest("设备编号不能为空")
    device, item, order, user = await _get_visible(
        session, principal, Device.serial_no == normalized
    )
    return _serialize(device, item, order, user, business_today())


async def register_device(
    session: AsyncSession, principal: Principal, payload: DeviceCreate
) -> dict:
    if not principal.is_admin:
        raise Forbidden("只有管理员可以登记设备")
    item = (
        await session.execute(
            select(OrderItem)
            .where(OrderItem.id == payload.order_item_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if item is None:
        raise NotFound("订单明细不存在")
    order = await session.get(Order, item.order_id)
    if order is None:
        raise NotFound("订单不存在")
    if order.status == "cancelled":
        raise Conflict("已取消订单不能登记设备", code="ORDER_CANCELLED")
    if payload.model_code != item.product_model:
        raise InvalidRequest("设备型号必须与订单明细一致")
    registered = (
        await session.execute(
            select(func.count())
            .select_from(Device)
            .where(Device.order_item_id == item.id)
        )
    ).scalar_one()
    if registered >= item.quantity:
        raise Conflict("该订单明细的购买数量已全部登记", code="DEVICE_QUANTITY_EXCEEDED")
    duplicate = (
        await session.execute(select(Device.id).where(Device.serial_no == payload.serial_no))
    ).scalar_one_or_none()
    if duplicate is not None:
        raise Conflict("设备编号已存在", code="SERIAL_NO_TAKEN")
    device = Device(
        id=new_id(),
        serial_no=payload.serial_no,
        user_id=order.user_id,
        order_item_id=item.id,
        model_code=payload.model_code,
        firmware_version=payload.firmware_version,
        purchased_on=payload.purchased_on,
        warranty_until=payload.warranty_until,
        status="active",
    )
    session.add(device)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if "serial_no" in str(exc) or "uk_devices_serial_no" in str(exc):
            raise Conflict("设备编号已存在", code="SERIAL_NO_TAKEN") from exc
        raise Unavailable("设备未保存，请重试") from exc
    except SQLAlchemyError as exc:
        await session.rollback()
        raise Unavailable("设备未保存，请重试") from exc
    await session.refresh(device)
    user = await session.get(BusinessUser, device.user_id)
    return _serialize(device, item, order, user, business_today())
