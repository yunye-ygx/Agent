"""Order use-cases. Services never depend on FastAPI request/response objects."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.business.errors import Conflict, Forbidden, InvalidRequest, NotFound, Unavailable
from app.business.models import BusinessUser, Device, Order, OrderItem
from app.business.principal import Principal
from app.business.schemas import MAX_ORDER_TOTAL, OrderCreate
from app.business.utils import iso_datetime, money, new_id


def _serialize_summary(order: Order, item_count: int, user: BusinessUser | None = None) -> dict:
    return {
        "id": order.id,
        "order_no": order.order_no,
        "status": order.status,
        "total_amount": money(order.total_amount),
        "currency": order.currency,
        "item_count": item_count,
        "created_at": iso_datetime(order.created_at),
        "customer_username": user.username if user else None,
        "customer_display_name": user.display_name if user else None,
    }


def _serialize_detail(order: Order, items: list[OrderItem], user: BusinessUser | None) -> dict:
    payload = _serialize_summary(order, len(items), user)
    payload.update(
        {
            "user_id": order.user_id,
            "items": [
                {
                    "id": item.id,
                    "product_name": item.product_name,
                    "product_model": item.product_model,
                    "quantity": item.quantity,
                    "unit_price": money(item.unit_price),
                    "line_amount": money(item.line_amount),
                }
                for item in items
            ],
        }
    )
    return payload


async def _load_customer(session: AsyncSession, user_id: str) -> BusinessUser:
    user = await session.get(BusinessUser, user_id)
    if user is None or user.role != "customer":
        raise NotFound("客户不存在")
    return user


async def create_order(
    session: AsyncSession, principal: Principal, payload: OrderCreate
) -> dict:
    if not principal.is_admin:
        raise Forbidden("只有管理员可以录入订单")
    customer = await _load_customer(session, payload.user_id)
    existing = (
        await session.execute(select(Order.id).where(Order.order_no == payload.order_no))
    ).scalar_one_or_none()
    if existing is not None:
        raise Conflict("订单号已存在", code="ORDER_NO_TAKEN")

    total = Decimal("0.00")
    items: list[OrderItem] = []
    order = Order(
        id=new_id(),
        order_no=payload.order_no,
        user_id=customer.id,
        created_by=principal.user_id,
        status="recorded",
        total_amount=Decimal("0.00"),
        currency="CNY",
    )
    for entry in payload.items:
        line_amount = (entry.unit_price * entry.quantity).quantize(Decimal("0.01"))
        total += line_amount
        items.append(
            OrderItem(
                id=new_id(),
                order_id=order.id,
                product_name=entry.product_name,
                product_model=entry.product_model,
                quantity=entry.quantity,
                unit_price=entry.unit_price,
                line_amount=line_amount,
            )
        )
    if total > MAX_ORDER_TOTAL:
        raise InvalidRequest("订单总金额超出允许范围")
    order.total_amount = total
    session.add(order)
    for item in items:
        session.add(item)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if "order_no" in str(exc) or "uk_orders_order_no" in str(exc):
            raise Conflict("订单号已存在", code="ORDER_NO_TAKEN") from exc
        raise Unavailable("订单未保存，请重试") from exc
    except SQLAlchemyError as exc:
        await session.rollback()
        raise Unavailable("订单未保存，请重试") from exc
    await session.refresh(order)
    return _serialize_detail(order, items, customer)


async def list_orders(
    session: AsyncSession,
    principal: Principal,
    *,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    filters = []
    if not principal.is_admin:
        filters.append(Order.user_id == principal.user_id)
    total = (
        await session.execute(select(func.count()).select_from(Order).where(*filters))
    ).scalar_one()
    rows = list(
        (
            await session.execute(
                select(Order, BusinessUser)
                .join(BusinessUser, BusinessUser.id == Order.user_id)
                .where(*filters)
                .order_by(Order.created_at.desc(), Order.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
    )
    counts: dict[str, int] = {}
    if rows:
        counts = dict(
            (
                await session.execute(
                    select(OrderItem.order_id, func.count())
                    .where(OrderItem.order_id.in_([order.id for order, _ in rows]))
                    .group_by(OrderItem.order_id)
                )
            ).all()
        )
    return [
        _serialize_summary(order, counts.get(order.id, 0), user) for order, user in rows
    ], total


async def _get_visible_order(
    session: AsyncSession, principal: Principal, order_id: str
) -> tuple[Order, BusinessUser]:
    statement = (
        select(Order, BusinessUser)
        .join(BusinessUser, BusinessUser.id == Order.user_id)
        .where(Order.id == order_id)
    )
    if not principal.is_admin:
        statement = statement.where(Order.user_id == principal.user_id)
    row = (await session.execute(statement)).first()
    if row is None:
        raise NotFound("订单不存在")
    return row[0], row[1]


async def get_order(session: AsyncSession, principal: Principal, order_id: str) -> dict:
    order, user = await _get_visible_order(session, principal, order_id)
    items = list(
        (
            await session.execute(
                select(OrderItem)
                .where(OrderItem.order_id == order.id)
                .order_by(OrderItem.id)
            )
        )
        .scalars()
        .all()
    )
    return _serialize_detail(order, items, user)


async def get_order_by_no(
    session: AsyncSession, principal: Principal, order_no: str
) -> dict:
    normalized = order_no.strip().upper()
    if not normalized:
        raise InvalidRequest("订单号不能为空")
    statement = select(Order.id).where(Order.order_no == normalized)
    if not principal.is_admin:
        statement = statement.where(Order.user_id == principal.user_id)
    order_id = (await session.execute(statement)).scalar_one_or_none()
    if order_id is None:
        raise NotFound("订单不存在")
    return await get_order(session, principal, order_id)


async def update_order_status(
    session: AsyncSession, principal: Principal, order_id: str, status: str
) -> dict:
    if not principal.is_admin:
        raise Forbidden("只有管理员可以修改订单状态")
    order = (
        await session.execute(select(Order).where(Order.id == order_id).with_for_update())
    ).scalar_one_or_none()
    if order is None:
        raise NotFound("订单不存在")
    if status != order.status:
        if order.status == "cancelled":
            raise Conflict("已取消订单不能再修改", code="ORDER_CANCELLED")
        allowed = {
            ("recorded", "completed"),
            ("recorded", "cancelled"),
            ("completed", "cancelled"),
        }
        if (order.status, status) not in allowed:
            raise Conflict("当前状态不允许该变更", code="INVALID_ORDER_TRANSITION")
        if status == "cancelled":
            devices = (
                await session.execute(
                    select(func.count())
                    .select_from(Device)
                    .join(OrderItem, OrderItem.id == Device.order_item_id)
                    .where(OrderItem.order_id == order.id)
                )
            ).scalar_one()
            if devices:
                raise Conflict("订单已有登记设备，不能取消", code="ORDER_HAS_DEVICES")
        order.status = status
        try:
            await session.commit()
        except SQLAlchemyError as exc:
            await session.rollback()
            raise Unavailable("订单状态未保存，请重试") from exc
        await session.refresh(order)
    return await get_order(session, principal, order_id)
