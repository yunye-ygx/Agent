"""Generate the synthetic after-sales dataset used by seed_business.

The generated JSON is the reviewed deliverable; this script keeps the counts and
relative dates consistent and recalculates the manifest hashes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "samples" / "after-sales-business-v1"
CORPUS = ROOT / "samples" / "rag-eval-after-sales-v1" / "corpus"
AS_OF = date(2026, 10, 26)

USERS = [
    {"id": "user-admin-001", "username": "admin", "display_name": "售后管理员", "role": "admin"},
    {"id": "user-alice-001", "username": "alice", "display_name": "爱丽丝", "role": "customer"},
    {"id": "user-bob-001", "username": "bob", "display_name": "鲍勃", "role": "customer"},
    {"id": "user-carol-001", "username": "carol", "display_name": "卡罗尔", "role": "customer"},
]
USER_BY_NAME = {user["username"]: user["id"] for user in USERS}

ORDER_SPECS = [
    # no, customer, status, model, quantity, unit price, days before base
    ("SO-2026-1001", "alice", "completed", "NX100", 1, "1299.00", 60),
    ("SO-2026-1002", "alice", "completed", "NX200", 1, "1899.00", 55),
    ("SO-2026-1003", "alice", "completed", "NX100", 1, "1299.00", 40),
    ("SO-2026-1004", "alice", "recorded", "NX200", 1, "1899.00", 20),
    ("SO-2026-1005", "bob", "completed", "NX100", 1, "1299.00", 50),
    ("SO-2026-1006", "bob", "completed", "NX200", 1, "1899.00", 45),
    ("SO-2026-1007", "bob", "recorded", "NX100", 1, "1299.00", 15),
    ("SO-2026-1008", "bob", "recorded", "NX200", 1, "1899.00", 12),
    ("SO-2026-1009", "carol", "recorded", "NX100", 3, "1299.00", 30),
    ("SO-2026-1010", "carol", "recorded", "NX200", 3, "1899.00", 25),
    ("SO-2026-1011", "carol", "recorded", "NX100", 2, "1299.00", 18),
    ("SO-2026-1012", "carol", "recorded", "NX200", 2, "1899.00", 8),
]

DEVICE_SPECS = [
    # serial, item index, model, firmware, purchased days before, warranty days, status
    ("NX100-2024-001", 1, "NX100", "1.4.2", 700, 730, "active"),
    ("NX200-2024-002", 2, "NX200", "2.0.3", 800, 365, "active"),
    ("NX100-2026-003", 3, "NX100", "1.6.0", 30, 730, "active"),
    ("NX200-2025-004", 4, "NX200", "2.1.0", 400, 365, "active"),
    ("NX100-2025-005", 5, "NX100", "1.2.0", 200, 365, "active"),
    ("NX200-2024-006", 6, "NX200", "2.0.1", 750, 730, "active"),
    ("NX100-2025-007", 7, "NX100", "1.5.0", 500, 365, "active"),
    ("NX200-2025-008", 8, "NX200", "2.0.0", 1000, 365, "retired"),
    ("NX100-2026-009", 9, "NX100", "1.6.1", 90, 730, "active"),
    ("NX100-2025-010", 9, "NX100", "1.4.2", 600, 730, "active"),
    ("NX100-2025-011", 9, "NX100", "1.5.0", 365, 365, "active"),
    ("NX200-2026-012", 10, "NX200", "2.1.0", 100, 365, "active"),
    ("NX200-2024-013", 10, "NX200", "2.0.3", 730, 730, "active"),
    ("NX200-2024-014", 10, "NX200", "2.0.0", 731, 730, "active"),
    ("NX100-2026-015", 11, "NX100", "1.6.0", 50, 365, "active"),
    ("NX100-2025-016", 11, "NX100", "1.2.0", 366, 365, "active"),
    ("NX200-2026-017", 12, "NX200", "2.1.0", 120, 730, "active"),
    ("NX200-2024-018", 12, "NX200", "2.0.1", 900, 730, "active"),
]


def iso_utc(moment: datetime) -> str:
    return moment.astimezone().strftime("%Y-%m-%dT%H:%M:%SZ")


BASE = datetime.combine(AS_OF, time(1, 0), tzinfo=UTC)


def build_orders():
    orders = []
    for index, (order_no, customer, status, model, quantity, price, days) in enumerate(
        ORDER_SPECS, start=1
    ):
        item_id = f"item-{index:03d}-1"
        created = BASE - timedelta(days=days)
        orders.append(
            {
                "id": f"order-2026-{index:03d}",
                "order_no": order_no,
                "user_id": USER_BY_NAME[customer],
                "status": status,
                "currency": "CNY",
                "created_at": iso_utc(created),
                "items": [
                    {
                        "id": item_id,
                        "product_name": f"澄印 {model} 标签打印机",
                        "product_model": model,
                        "quantity": quantity,
                        "unit_price": price,
                    }
                ],
            }
        )
    return orders


def build_devices():
    devices = []
    for index, (serial, item_no, model, firmware, purchased_days, warranty_days, status) in enumerate(
        DEVICE_SPECS, start=1
    ):
        order = ORDER_SPECS[item_no - 1]
        devices.append(
            {
                "id": f"device-2026-{index:03d}",
                "serial_no": serial,
                "order_item_id": f"item-{item_no:03d}-1",
                "user_id": USER_BY_NAME[order[1]],
                "model_code": model,
                "firmware_version": firmware,
                "purchased_days_before_as_of": purchased_days,
                "warranty_days": warranty_days,
                "status": status,
            }
        )
    return devices


TICKET_SPECS = [
    # no, device index, subject, status, days before, events [(actor, from, to, message, hours after)]
    (1, 1, "NX100 报 E17，上盖反复提示未闭合", "open", 10, [
        ("customer", None, "open", "工单已创建", 0),
    ]),
    (2, 3, "标签打印逐张偏移，校准后仍偏移", "open", 9, [
        ("customer", None, "open", "工单已创建", 0),
        ("admin", "open", "open", "已收到，请补充标签尺寸和固件版本照片", 3),
    ]),
    (3, 4, "NX200 提示 E42 地址冲突", "open", 8, [
        ("customer", None, "open", "工单已创建", 0),
    ]),
    (4, 2, "设备无法开机，电源指示灯不亮", "in_progress", 7, [
        ("customer", None, "open", "工单已创建", 0),
        ("admin", "open", "in_progress", "已安排远程排查电源和插座供电", 2),
    ]),
    (5, 5, "打印内容有纵向白条", "in_progress", 6, [
        ("customer", None, "open", "工单已创建", 0),
        ("admin", "open", "in_progress", "请提供打印样张照片，正在核查打印头", 4),
        ("admin", "in_progress", "in_progress", "样张已收到，等待配件确认", 20),
    ]),
    (6, 7, "NX100 固件 1.5.0 无法进入介质校准", "in_progress", 5, [
        ("customer", None, "open", "工单已创建", 0),
        ("admin", "open", "in_progress", "正在核对管理页维护菜单版本", 5),
        ("admin", "in_progress", "in_progress", "用户已升级到 1.6.0，继续观察", 30),
    ]),
    (7, 6, "NX200 网络打印间歇中断", "resolved", 4, [
        ("customer", None, "open", "工单已创建", 0),
        ("admin", "open", "in_progress", "检查交换机端口与 IP 分配方式", 2),
        ("admin", "in_progress", "resolved", "重新绑定静态地址后连续打印正常", 30),
    ]),
    (8, 9, "NX100 清洁后仍频繁卡纸", "resolved", 3, [
        ("customer", None, "open", "工单已创建", 0),
        ("admin", "open", "in_progress", "指导清理导纸片附近纸屑", 1),
        ("admin", "in_progress", "resolved", "更换纸卷后恢复正常", 8),
        ("admin", "resolved", "resolved", "已回访，客户确认无复现", 28),
    ]),
    (9, 10, "打印浓度不稳定", "resolved", 3, [
        ("customer", None, "open", "工单已创建", 0),
        ("admin", "open", "in_progress", "确认介质类型和浓度设置", 2),
        ("admin", "in_progress", "resolved", "调整介质类型后恢复", 26),
    ]),
    (10, 12, "NX200 无法连接管理页", "closed", 2, [
        ("customer", None, "open", "工单已创建", 0),
        ("admin", "open", "in_progress", "核对网络地址和浏览器访问方式", 1),
        ("admin", "in_progress", "resolved", "清除错误代理设置后可访问", 5),
        ("admin", "resolved", "closed", "客户确认关闭", 24),
    ]),
    (11, 13, "固件升级后打印速度变慢", "closed", 2, [
        ("customer", None, "open", "工单已创建", 0),
        ("admin", "open", "in_progress", "核对升级前后配置差异", 2),
        ("admin", "in_progress", "resolved", "恢复介质校准参数后速度正常", 6),
        ("admin", "resolved", "closed", "已确认关闭", 26),
        ("admin", "closed", "closed", "归档：保留升级记录", 30),
    ]),
    (12, 8, "退役设备的历史打印异常核对", "closed", 1, [
        ("customer", None, "open", "工单已创建", 0),
        ("admin", "open", "in_progress", "设备已退役，核对历史记录", 2),
        ("admin", "in_progress", "resolved", "历史故障与已更换主板相关", 9),
        ("admin", "resolved", "closed", "客户不再使用该设备，关闭", 25),
    ]),
]


def build_tickets():
    tickets = []
    for number, device_index, subject, status, days, event_specs in TICKET_SPECS:
        device = build_devices()[device_index - 1]
        created = BASE - timedelta(days=days)
        events = []
        for actor, from_status, to_status, message, hours in event_specs:
            events.append(
                {
                    "actor": actor,
                    "event_type": "created" if from_status is None else (
                        "comment" if from_status == to_status else "status_change"
                    ),
                    "from_status": from_status,
                    "to_status": to_status,
                    "message": message,
                    "at": iso_utc(created + timedelta(hours=hours)),
                }
            )
        assert events[-1]["to_status"] == status, (number, status, events[-1]["to_status"])
        tickets.append(
            {
                "id": f"ticket-2026-{number:03d}",
                "ticket_no": f"T2026-{number:04d}",
                "user_id": device["user_id"],
                "device_id": device["id"],
                "subject": subject,
                "description": f"{subject}。合成练习数据，用于验证售后工单与处理记录。",
                "status": status,
                "created_at": iso_utc(created),
                "events": events,
            }
        )
    return tickets


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    users = {"synthetic": True, "users": USERS}
    orders = {"synthetic": True, "orders": build_orders()}
    devices = {"synthetic": True, "as_of": AS_OF.isoformat(), "devices": build_devices()}
    tickets = {"synthetic": True, "tickets": build_tickets()}
    write_json(OUT / "users.json", users)
    write_json(OUT / "orders.json", orders)
    write_json(OUT / "devices.json", devices)
    write_json(OUT / "tickets.json", tickets)

    docs = [
        ("01_nx100_manual.md", "NX100 产品手册"),
        ("02_nx200_manual.md", "NX200 产品手册"),
        ("03_nx100_faults.md", "NX100 故障排查指南"),
        ("04_nx200_network.md", "NX200 网络故障指南"),
        ("05_safety_and_service.md", "安全与服务规则"),
        ("06_nx100_firmware_notice.md", "NX100 固件公告"),
    ]
    documents = []
    for source, title in docs:
        path = CORPUS / source
        documents.append(
            {
                "source": source,
                "title": title,
                "sha256": sha256(path),
                "mime_type": "text/markdown",
            }
        )
    knowledge = {
        "synthetic": True,
        "version": "support-knowledge-v1",
        "knowledge_base_name": "售后开放资料",
        "corpus_directory": "samples/rag-eval-after-sales-v1/corpus",
        "documents": documents,
        "excluded_sources": [
            "07_archived_calibration.md",
            "08_ticket_cover.md",
            "09_ticket_network.md",
            "10_ticket_unverified.md",
            "eval/dev_cases.jsonl",
        ],
    }
    write_json(OUT / "knowledge-manifest.json", knowledge)

    files = {}
    for name in ("users.json", "orders.json", "devices.json", "tickets.json", "knowledge-manifest.json"):
        files[name] = sha256(OUT / name)
    manifest = {
        "version": "after-sales-business-v1",
        "synthetic": True,
        "as_of": AS_OF.isoformat(),
        "timezone": "Asia/Shanghai",
        "counts": {
            "users": len(USERS),
            "admins": sum(1 for user in USERS if user["role"] == "admin"),
            "customers": sum(1 for user in USERS if user["role"] == "customer"),
            "orders": len(orders["orders"]),
            "order_items": sum(len(order["items"]) for order in orders["orders"]),
            "devices": len(devices["devices"]),
            "tickets": len(tickets["tickets"]),
            "ticket_events": sum(len(ticket["events"]) for ticket in tickets["tickets"]),
        },
        "files": files,
        "notes": [
            "全部账号、订单、设备、工单均为合成练习数据，不代表真实客户或厂商。",
            "密码不在任何样例文件中；seed_business 交互读取或从标准输入读取。",
            "购买日期和保修截止日以 as_of 为基准相对生成。",
            "重复执行只跳过已存在记录，不覆盖用户维护内容。",
        ],
    }
    write_json(OUT / "manifest.json", manifest)
    print(json.dumps(manifest["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
