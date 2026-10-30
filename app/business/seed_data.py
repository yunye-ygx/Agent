"""Synthetic seed dataset loading, cross-reference validation and idempotent import."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.passwords import hash_password_async
from app.business.models import (
    BusinessUser,
    Device,
    Order,
    OrderItem,
    Ticket,
    TicketEvent,
)
from app.business.tickets import ALLOWED_TRANSITIONS
from app.business.utils import new_id

SAMPLE_FILES = ("users.json", "orders.json", "devices.json", "tickets.json")


class SeedError(Exception):
    """Invalid dataset or an existing record conflicts with the synthetic IDs."""


@dataclass
class Dataset:
    as_of: date
    users: list[dict]
    orders: list[dict]
    devices: list[dict]
    tickets: list[dict]
    counts: dict = field(default_factory=dict)


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SeedError(f"缺少数据文件：{path.name}") from exc
    except json.JSONDecodeError as exc:
        raise SeedError(f"数据文件不是合法 JSON：{path.name}") from exc


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def load_dataset(root: Path, as_of: date | None = None) -> Dataset:
    manifest = _json(root / "manifest.json")
    base_date = date.fromisoformat(manifest["as_of"])
    if as_of is not None and as_of != base_date:
        raise SeedError(
            f"--as-of 必须是 manifest 固定的 {base_date.isoformat()}；"
            "样例日期已固化，混用不同基准会破坏关联数据"
        )
    for name, expected in manifest.get("files", {}).items():
        if name in {"manifest.json"}:
            continue
        path = root / name
        if not path.exists():
            raise SeedError(f"缺少 manifest 声明的文件：{name}")
        actual = _sha256(path)
        if actual != expected:
            raise SeedError(f"文件哈希与 manifest 不一致：{name}，请先审核变更")
    users = _json(root / "users.json")["users"]
    orders = _json(root / "orders.json")["orders"]
    devices = _json(root / "devices.json")["devices"]
    tickets = _json(root / "tickets.json")["tickets"]

    _validate_users(users)
    user_by_id = {user["id"]: user for user in users}
    item_by_id, order_by_id = _validate_orders(orders, user_by_id)
    _validate_devices(devices, item_by_id, order_by_id, user_by_id, base_date)
    _validate_tickets(tickets, devices, user_by_id)
    counts = {
        "users": len(users),
        "admins": sum(1 for user in users if user["role"] == "admin"),
        "customers": sum(1 for user in users if user["role"] == "customer"),
        "orders": len(orders),
        "order_items": sum(len(order["items"]) for order in orders),
        "devices": len(devices),
        "tickets": len(tickets),
        "ticket_events": sum(len(ticket["events"]) for ticket in tickets),
    }
    for key, expected in manifest.get("counts", {}).items():
        if counts.get(key) != expected:
            raise SeedError(f"计数不匹配：{key} 期望 {expected}，实际 {counts.get(key)}")
    return Dataset(base_date, users, orders, devices, tickets, counts)


def _validate_users(users: list[dict]) -> None:
    ids, names = set(), set()
    for user in users:
        if user["id"] in ids or user["username"] in names:
            raise SeedError("用户 ID 或用户名重复")
        ids.add(user["id"])
        names.add(user["username"])
        if user["role"] not in {"customer", "admin"}:
            raise SeedError(f"用户角色不合法：{user['username']}")
        if len(user["username"]) > 32 or not user["display_name"].strip():
            raise SeedError(f"用户字段不合法：{user['username']}")
    if sum(1 for user in users if user["role"] == "admin") != 1:
        raise SeedError("样例数据必须恰好包含 1 个管理员")


def _validate_orders(orders: list[dict], users: dict) -> tuple[dict, dict]:
    item_by_id, order_by_id = {}, {}
    order_numbers = set()
    for order in orders:
        if order["id"] in order_by_id or order["order_no"] in order_numbers:
            raise SeedError(f"订单 ID 或订单号重复：{order['order_no']}")
        if order["user_id"] not in users or users[order["user_id"]]["role"] != "customer":
            raise SeedError(f"订单 {order['order_no']} 的客户不存在")
        if order["status"] not in {"recorded", "completed", "cancelled"}:
            raise SeedError(f"订单状态不合法：{order['order_no']}")
        if not 1 <= len(order["items"]) <= 50:
            raise SeedError(f"订单明细数量不合法：{order['order_no']}")
        _parse_dt(order["created_at"])
        total = Decimal("0.00")
        for item in order["items"]:
            if item["id"] in item_by_id:
                raise SeedError(f"订单明细 ID 重复：{item['id']}")
            if item["product_model"] not in {"NX100", "NX200"}:
                raise SeedError(f"产品型号不合法：{item['id']}")
            if not 1 <= int(item["quantity"]) <= 1000:
                raise SeedError(f"数量不合法：{item['id']}")
            price = Decimal(str(item["unit_price"]))
            if price < 0 or price.as_tuple().exponent < -2:
                raise SeedError(f"单价不合法：{item['id']}")
            item["_line_amount"] = (price * int(item["quantity"])).quantize(Decimal("0.01"))
            item["_unit_price"] = price.quantize(Decimal("0.01"))
            total += item["_line_amount"]
            item_by_id[item["id"]] = {**item, "order_id": order["id"]}
        order["_total_amount"] = total
        order_by_id[order["id"]] = order
        order_numbers.add(order["order_no"])
    return item_by_id, order_by_id


def _validate_devices(
    devices: list[dict],
    items: dict,
    orders: dict,
    users: dict,
    as_of: date,
) -> None:
    serials, capacities, ids = set(), {}, set()
    for device in devices:
        if device["id"] in ids:
            raise SeedError("设备 ID 重复")
        ids.add(device["id"])
        if device["serial_no"] in serials:
            raise SeedError(f"设备编号重复：{device['serial_no']}")
        serials.add(device["serial_no"])
        item = items.get(device["order_item_id"])
        if item is None:
            raise SeedError(f"设备 {device['serial_no']} 的订单明细不存在")
        order = orders[item["order_id"]]
        if device["user_id"] != order["user_id"] or device["user_id"] not in users:
            raise SeedError(f"设备 {device['serial_no']} 的归属与订单不一致")
        if device["model_code"] != item["product_model"]:
            raise SeedError(f"设备 {device['serial_no']} 型号与订单明细不一致")
        if device["status"] not in {"active", "retired"}:
            raise SeedError(f"设备状态不合法：{device['serial_no']}")
        purchased = as_of - timedelta(days=int(device["purchased_days_before_as_of"]))
        warranty = purchased + timedelta(days=int(device["warranty_days"]))
        if warranty < purchased:
            raise SeedError(f"设备保修日期不合法：{device['serial_no']}")
        device["_purchased_on"] = purchased
        device["_warranty_until"] = warranty
        capacities[item["id"]] = capacities.get(item["id"], 0) + 1
    for item_id, registered in capacities.items():
        if registered > int(items[item_id]["quantity"]):
            raise SeedError(f"订单明细 {item_id} 的设备数量超过购买数量")
    if len(devices) != sum(capacities.values()):
        raise SeedError("设备计数不合法")


def _validate_tickets(tickets: list[dict], devices: list[dict], users: dict) -> None:
    device_by_id = {device["id"]: device for device in devices}
    ids, numbers = set(), set()
    for ticket in tickets:
        if ticket["id"] in ids or ticket["ticket_no"] in numbers:
            raise SeedError(f"工单 ID 或编号重复：{ticket['ticket_no']}")
        ids.add(ticket["id"])
        numbers.add(ticket["ticket_no"])
        device = device_by_id.get(ticket["device_id"])
        if device is None:
            raise SeedError(f"工单 {ticket['ticket_no']} 的设备不存在")
        if ticket["user_id"] != device["user_id"]:
            raise SeedError(f"工单 {ticket['ticket_no']} 的客户与设备归属不一致")
        if ticket["status"] not in {"open", "in_progress", "resolved", "closed"}:
            raise SeedError(f"工单状态不合法：{ticket['ticket_no']}")
        events = ticket["events"]
        if not events or events[0]["event_type"] != "created":
            raise SeedError(f"工单 {ticket['ticket_no']} 缺少创建记录")
        previous_at = None
        status = None
        if ticket["user_id"] not in users:
            raise SeedError(f"工单 {ticket['ticket_no']} 的客户不存在")
        for index, event in enumerate(events):
            if event.get("actor") not in {"customer", "admin"}:
                raise SeedError(f"工单 {ticket['ticket_no']} 的处理人角色不合法")
            if event["actor"] == "customer" and index != 0:
                raise SeedError(f"工单 {ticket['ticket_no']} 的客户记录只能出现在创建时")
            moment = _parse_dt(event["at"])
            if previous_at is not None and moment < previous_at:
                raise SeedError(f"工单 {ticket['ticket_no']} 的处理时间倒序")
            previous_at = moment
            if index == 0:
                if event["from_status"] is not None or event["to_status"] != "open":
                    raise SeedError(f"工单 {ticket['ticket_no']} 初始状态不合法")
                status = "open"
                continue
            if event["to_status"] == event["from_status"]:
                if event["event_type"] != "comment":
                    raise SeedError(f"工单 {ticket['ticket_no']} 备注事件类型不合法")
            elif (event["from_status"], event["to_status"]) not in ALLOWED_TRANSITIONS:
                raise SeedError(f"工单 {ticket['ticket_no']} 状态跳转不合法")
            if event["from_status"] != status:
                raise SeedError(f"工单 {ticket['ticket_no']} 的事件状态不连续")
            status = event["to_status"]
        if status != ticket["status"]:
            raise SeedError(f"工单 {ticket['ticket_no']} 最终状态与事件不一致")
        _parse_dt(ticket["created_at"])


def describe(dataset: Dataset) -> str:
    counts = dataset.counts
    return (
        f"基准日期 {dataset.as_of.isoformat()}（合成练习数据）\n"
        f"用户 {counts['users']}（客户 {counts['customers']}）、"
        f"订单 {counts['orders']}、明细 {counts['order_items']}、"
        f"设备 {counts['devices']}、工单 {counts['tickets']}、"
        f"处理记录 {counts['ticket_events']}"
    )


async def _find_user(session: AsyncSession, user_id: str, username: str):
    by_id = await session.get(BusinessUser, user_id)
    by_name = (
        await session.execute(select(BusinessUser).where(BusinessUser.username == username))
    ).scalar_one_or_none()
    return by_id, by_name


async def plan_missing_users(session: AsyncSession, users: list[dict]) -> list[dict]:
    missing = []
    for user in users:
        by_id, by_name = await _find_user(session, user["id"], user["username"])
        if by_id is None and by_name is None:
            missing.append(user)
            continue
        if by_id is None or by_name is None or by_id.id != by_name.id:
            raise SeedError(
                f"用户名 {user['username']} 已存在但 ID 不同；不会覆盖，请人工核对"
            )
    return missing


async def apply_dataset(
    session: AsyncSession, dataset: Dataset, passwords: dict[str, str]
) -> dict:
    report = {
        "users_created": 0,
        "users_skipped": 0,
        "orders_created": 0,
        "orders_skipped": 0,
        "devices_created": 0,
        "devices_skipped": 0,
        "tickets_created": 0,
        "tickets_skipped": 0,
    }
    try:
        for user in dataset.users:
            by_id, by_name = await _find_user(session, user["id"], user["username"])
            if by_id is not None or by_name is not None:
                if by_id is None or by_name is None or by_id.id != by_name.id:
                    raise SeedError(
                        f"用户名 {user['username']} 已存在但 ID 不同；不会覆盖，请人工核对"
                    )
                report["users_skipped"] += 1
                continue
            password = passwords.get(user["username"])
            if not password:
                raise SeedError(f"缺少新账号 {user['username']} 的密码")
            if not 12 <= len(password) <= 128:
                raise SeedError(f"账号 {user['username']} 的密码长度不合要求")
            session.add(
                BusinessUser(
                    id=user["id"],
                    username=user["username"],
                    display_name=user["display_name"],
                    password_hash=await hash_password_async(password),
                    role=user["role"],
                    status="active",
                )
            )
            report["users_created"] += 1
        await session.flush()

        for order in dataset.orders:
            by_id = await session.get(Order, order["id"])
            by_no = (
                await session.execute(
                    select(Order.id).where(Order.order_no == order["order_no"])
                )
            ).scalar_one_or_none()
            if by_id is not None or by_no is not None:
                if by_id is None or by_no is None or by_id.id != by_no:
                    raise SeedError(
                        f"订单号 {order['order_no']} 已存在但记录不同；不会覆盖"
                    )
                report["orders_skipped"] += 1
                continue
            session.add(
                Order(
                    id=order["id"],
                    order_no=order["order_no"],
                    user_id=order["user_id"],
                    created_by=next(
                        user["id"] for user in dataset.users if user["role"] == "admin"
                    ),
                    status=order["status"],
                    total_amount=order["_total_amount"],
                    currency=order["currency"],
                    created_at=_parse_dt(order["created_at"]),
                )
            )
            for item in order["items"]:
                session.add(
                    OrderItem(
                        id=item["id"],
                        order_id=order["id"],
                        product_name=item["product_name"],
                        product_model=item["product_model"],
                        quantity=int(item["quantity"]),
                        unit_price=item["_unit_price"],
                        line_amount=item["_line_amount"],
                    )
                )
            report["orders_created"] += 1
        await session.flush()

        for device in dataset.devices:
            by_id = await session.get(Device, device["id"])
            by_serial = (
                await session.execute(
                    select(Device.id).where(Device.serial_no == device["serial_no"])
                )
            ).scalar_one_or_none()
            if by_id is not None or by_serial is not None:
                if by_id is None or by_serial is None or by_id.id != by_serial:
                    raise SeedError(
                        f"设备编号 {device['serial_no']} 已存在但记录不同；不会覆盖"
                    )
                report["devices_skipped"] += 1
                continue
            session.add(
                Device(
                    id=device["id"],
                    serial_no=device["serial_no"],
                    user_id=device["user_id"],
                    order_item_id=device["order_item_id"],
                    model_code=device["model_code"],
                    firmware_version=device["firmware_version"],
                    purchased_on=device["_purchased_on"],
                    warranty_until=device["_warranty_until"],
                    status=device["status"],
                )
            )
            report["devices_created"] += 1
        await session.flush()

        admin_id = next(user["id"] for user in dataset.users if user["role"] == "admin")
        for ticket in dataset.tickets:
            by_id = await session.get(Ticket, ticket["id"])
            by_no = (
                await session.execute(
                    select(Ticket.id).where(Ticket.ticket_no == ticket["ticket_no"])
                )
            ).scalar_one_or_none()
            if by_id is not None or by_no is not None:
                if by_id is None or by_no is None or by_id.id != by_no:
                    raise SeedError(
                        f"工单号 {ticket['ticket_no']} 已存在但记录不同；不会覆盖"
                    )
                report["tickets_skipped"] += 1
                continue
            session.add(
                Ticket(
                    id=ticket["id"],
                    ticket_no=ticket["ticket_no"],
                    user_id=ticket["user_id"],
                    device_id=ticket["device_id"],
                    subject=ticket["subject"],
                    description=ticket["description"],
                    status=ticket["status"],
                    version=len(ticket["events"]),
                    created_at=_parse_dt(ticket["created_at"]),
                )
            )
            for event in ticket["events"]:
                actor_id = ticket["user_id"] if event["actor"] == "customer" else admin_id
                session.add(
                    TicketEvent(
                        id=new_id(),
                        ticket_id=ticket["id"],
                        actor_id=actor_id,
                        event_type=event["event_type"],
                        from_status=event["from_status"],
                        to_status=event["to_status"],
                        message=event["message"],
                        created_at=_parse_dt(event["at"]),
                    )
                )
            report["tickets_created"] += 1
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        raise SeedError("导入事务失败，已回滚；请检查数据库结构与约束") from exc
    except Exception:
        await session.rollback()
        raise
    return report
