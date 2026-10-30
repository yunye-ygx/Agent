"""End-to-end HTTP acceptance against a running API.

Credentials are read from environment variables; no password is stored in this
file or printed. Example:

    $env:AFTER_SALES_ADMIN_PASSWORD = "..."
    python -X utf8 tools/verify_after_sales_api.py
"""

from __future__ import annotations

import os
import sys
import uuid

import httpx

BASE = os.getenv("AFTER_SALES_BASE_URL", "http://127.0.0.1:8001")
ORIGIN = BASE
ACCOUNTS = {
    "admin": os.getenv("AFTER_SALES_ADMIN_PASSWORD", ""),
    "alice": os.getenv("AFTER_SALES_ALICE_PASSWORD", ""),
    "bob": os.getenv("AFTER_SALES_BOB_PASSWORD", ""),
    "carol": os.getenv("AFTER_SALES_CAROL_PASSWORD", ""),
}
PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name} {detail}")


def client_for(username: str) -> httpx.Client:
    password = ACCOUNTS[username]
    if not password:
        raise SystemExit(f"缺少环境变量密码：{username}")
    client = httpx.Client(base_url=BASE, timeout=40)
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
        headers={"Origin": ORIGIN},
    )
    check(f"登录 {username}", response.status_code == 200, response.text[:200])
    return client


def csrf_headers(client: httpx.Client) -> dict[str, str]:
    return {"Origin": ORIGIN, "X-CSRF-Token": client.cookies.get("business_csrf", "")}


def main() -> int:
    admin = client_for("admin")
    alice = client_for("alice")
    bob = client_for("bob")

    me = admin.get("/api/v1/auth/me")
    check("管理员身份由后端会话提供", me.json().get("role") == "admin")

    orders = alice.get("/api/v1/orders?page=1&page_size=100")
    devices = alice.get("/api/v1/devices?page=1&page_size=100")
    tickets = alice.get("/api/v1/tickets?page=1&page_size=100")
    check("alice 只看到自己的 4 笔订单", orders.json()["total"] == 4, orders.text[:200])
    check("alice 只看到自己的 4 台设备", devices.json()["total"] == 4, devices.text[:200])
    check(
        "alice 看到自己的工单",
        tickets.json()["total"] >= 4
        and all(item["customer_username"] == "alice" for item in tickets.json()["items"]),
        tickets.text[:300],
    )

    admin_orders = admin.get("/api/v1/orders?page=1&page_size=100").json()["items"]
    bob_order = next(item for item in admin_orders if item["customer_username"] == "bob")
    denied = alice.get(f"/api/v1/orders/{bob_order['id']}")
    check("客户访问他人订单返回 404", denied.status_code == 404, denied.text[:200])

    forged = alice.get("/api/v1/orders?page=1&user_id=bob")
    check("查询参数不能伪造他人身份", forged.status_code == 200 and forged.json()["total"] == 4)

    search = alice.post(
        "/api/v1/tool-lab/execute",
        json={
            "call_id": f"api-search-{uuid.uuid4().hex[:8]}",
            "name": "search_knowledge",
            "arguments": {"query": "NX100 显示 E17 上盖未闭合", "top_k": 3},
        },
        headers=csrf_headers(alice),
    )
    observation = search.json().get("observation", {}) if search.status_code == 200 else {}
    results = (observation.get("data") or {}).get("results", [])
    check(
        "search_knowledge 工具可返回开放资料片段",
        search.status_code == 200
        and observation.get("status") == "success"
        and len(results) > 0,
        search.text[:300],
    )
    if results:
        titles = " ".join(item["document_title"] for item in results)
        check("检索结果保留文档来源", titles.strip() != "", titles)
        check("检索结果不含内部路径", "object_key" not in str(observation))

    titled = alice.post(
        "/api/v1/tool-lab/execute",
        json={
            "call_id": f"api-search-{uuid.uuid4().hex[:8]}",
            "name": "search_knowledge",
            "arguments": {"query": "E17 怎么处理", "top_k": 3, "model_code": "NX100"},
        },
        headers=csrf_headers(alice),
    )
    check(
        "search_knowledge 不再接受型号参数",
        titled.status_code == 200
        and titled.json()["observation"]["status"] == "rejected"
        and titled.json()["observation"]["code"] == "INVALID_ARGUMENTS",
        titled.text[:300],
    )

    knowledge_bases = admin.get("/api/v1/knowledge-bases").json()
    support_kb = next(item for item in knowledge_bases if item["name"] == "售后开放资料")
    documents = admin.get(
        f"/api/v1/knowledge-bases/{support_kb['id']}/documents"
    ).json()
    firmware = next(item for item in documents if item["filename"].startswith("06_"))
    check("已发布文档默认开放", firmware["support_open"] is True, str(firmware))

    forbidden_toggle = alice.patch(
        f"/api/v1/documents/{firmware['id']}/support",
        json={"open": False},
        headers=csrf_headers(alice),
    )
    check("客户不能调整售后资料开放", forbidden_toggle.status_code == 403, forbidden_toggle.text[:200])

    closed = admin.patch(
        f"/api/v1/documents/{firmware['id']}/support",
        json={"open": False},
        headers=csrf_headers(admin),
    )
    check(
        "管理员可关闭单份文档的售后开放",
        closed.status_code == 200 and closed.json()["support_open"] is False,
        closed.text[:200],
    )
    hidden = alice.post(
        "/api/v1/tool-lab/execute",
        json={
            "call_id": f"api-search-{uuid.uuid4().hex[:8]}",
            "name": "search_knowledge",
            "arguments": {"query": "固件升级后需要重新校准吗", "top_k": 5},
        },
        headers=csrf_headers(alice),
    )
    hidden_titles = [
        item["document_title"]
        for item in (hidden.json()["observation"].get("data") or {}).get("results", [])
    ]
    check(
        "关闭后该文档不再被召回",
        all("06_" not in title for title in hidden_titles),
        str(hidden_titles),
    )
    reopened = admin.patch(
        f"/api/v1/documents/{firmware['id']}/support",
        json={"open": True},
        headers=csrf_headers(admin),
    )
    check(
        "管理员可重新开放该文档",
        reopened.status_code == 200 and reopened.json()["support_open"] is True,
        reopened.text[:200],
    )

    device = alice.get("/api/v1/devices?page=1&page_size=1").json()["items"][0]
    params = {
        "serial_no": device["serial_no"],
        "subject": f"验收工单 {uuid.uuid4().hex[:6]}",
        "description": "API 验收：创建工单并验证幂等与处理记录。",
    }
    reserved = alice.post(
        "/api/v1/operations",
        json={"action": "create_ticket", "parameters": params},
        headers=csrf_headers(alice),
    )
    check("操作键预留成功", reserved.status_code == 201, reserved.text[:200])
    operation_id = reserved.json()["id"]
    ticket_payload = {"operation_id": operation_id, **params}
    first = alice.post("/api/v1/tickets", json=ticket_payload, headers=csrf_headers(alice))
    second = alice.post("/api/v1/tickets", json=ticket_payload, headers=csrf_headers(alice))
    check("创建工单成功", first.status_code == 201, first.text[:300])
    check(
        "同一操作键重复提交返回同一工单",
        second.status_code == 201 and second.json()["id"] == first.json()["id"],
        second.text[:300],
    )
    ticket = first.json()
    check("新工单有初始处理记录", ticket["events_total"] == 1 and ticket["version"] == 1)

    changed = dict(params)
    changed["subject"] = "不同参数"
    mismatch = alice.post(
        "/api/v1/tickets",
        json={"operation_id": operation_id, **changed},
        headers=csrf_headers(alice),
    )
    check("同一操作键改参数被拒绝", mismatch.status_code == 409, mismatch.text[:200])

    operation = alice.get(f"/api/v1/operations/{operation_id}")
    check(
        "操作查询返回成功凭据",
        operation.json()["status"] == "succeeded"
        and operation.json()["result_resource_id"] == ticket["id"],
    )

    processed = admin.post(
        f"/api/v1/tickets/{ticket['id']}/events",
        json={"expected_version": 1, "message": "验收：开始处理", "to_status": "in_progress"},
        headers=csrf_headers(admin),
    )
    check("管理员可追加处理记录并改状态", processed.status_code == 200 and processed.json()["version"] == 2, processed.text[:300])
    stale = admin.post(
        f"/api/v1/tickets/{ticket['id']}/events",
        json={"expected_version": 1, "message": "旧版本"},
        headers=csrf_headers(admin),
    )
    check("旧版本处理被拒绝", stale.status_code == 409, stale.text[:200])
    refreshed = alice.get(f"/api/v1/tickets/{ticket['id']}")
    check("客户可见管理员处理记录", refreshed.json()["events_total"] == 2)

    tools = alice.get("/api/v1/tool-lab/tools").json()
    check("工具目录返回 8 个工具", len(tools["tools"]) == 8, str(len(tools["tools"])))
    executed = alice.post(
        "/api/v1/tool-lab/execute",
        json={"call_id": "api-check-1", "name": "list_orders", "arguments": {}},
        headers=csrf_headers(alice),
    )
    observation = executed.json()["observation"] if executed.status_code == 200 else {}
    check(
        "工具执行真实订单查询",
        observation.get("status") == "success" and observation["data"]["total"] == 4,
        executed.text[:300],
    )
    tool_ticket = alice.post(
        "/api/v1/tool-lab/execute",
        json={
            "call_id": "api-check-2",
            "name": "create_ticket",
            "arguments": params,
            "operation_id": operation_id,
        },
        headers=csrf_headers(alice),
    )
    tool_observation = tool_ticket.json()["observation"]
    check(
        "工具复用同一操作键返回原工单",
        tool_observation.get("status") == "success"
        and tool_observation["data"]["id"] == ticket["id"],
        tool_ticket.text[:300],
    )

    session_cookie = alice.cookies.get("business_session")
    alice.post("/api/v1/auth/logout", headers=csrf_headers(alice))
    replay = httpx.get(
        f"{BASE}/api/v1/auth/me", headers={"Cookie": f"business_session={session_cookie}"}
    )
    check("退出后旧会话重放返回 401", replay.status_code == 401, replay.text[:200])

    bob_orders = bob.get("/api/v1/orders?page=1&page_size=100").json()
    check("bob 只看到自己的订单", bob_orders["total"] == 4, str(bob_orders.get("total")))

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for client in (admin, alice, bob):
        client.close()
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
