"""Real browser acceptance through Microsoft Edge and the Chrome DevTools Protocol.

Requires the local API at AFTER_SALES_BASE_URL and passwords in environment
variables (same names as the HTTP acceptance script). Screenshots are written to
docs/images/after-sales. No password is printed or written to a file.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import httpx
import websockets

BASE = os.getenv("AFTER_SALES_BASE_URL", "http://127.0.0.1:8001")
ORIGIN = BASE
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
DEBUG_PORT = 9223
SHOTS = Path(__file__).resolve().parents[1] / "docs" / "images" / "after-sales"
PASSED: list[str] = []
FAILED: list[str] = []
EVENTS: list[dict] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name} {detail}")


def credentials() -> dict[str, str]:
    values = {
        "admin": os.getenv("AFTER_SALES_ADMIN_PASSWORD", ""),
        "alice": os.getenv("AFTER_SALES_ALICE_PASSWORD", ""),
        "bob": os.getenv("AFTER_SALES_BOB_PASSWORD", ""),
    }
    if not all(values.values()):
        raise SystemExit("缺少 AFTER_SALES_*_PASSWORD 环境变量")
    return values


class Browser:
    def __init__(self, ws):
        self.ws = ws
        self.next_id = 0

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        request_id = self.next_id
        await self.ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=60))
            if "method" in message:
                if message["method"] in {"Runtime.exceptionThrown", "Log.entryAdded"}:
                    EVENTS.append(message)
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"CDP {method} failed: {message['error']}")
                return message.get("result", {})

    async def eval(self, expression: str, *, await_promise: bool = False):
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
        )
        if result.get("exceptionDetails"):
            raise RuntimeError(json.dumps(result["exceptionDetails"], ensure_ascii=False)[:800])
        return result.get("result", {}).get("value")

    async def wait(self, expression: str, timeout: float = 20.0, label: str = "") -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if await self.eval(expression):
                    return
            except RuntimeError:
                pass
            await asyncio.sleep(0.25)
        raise TimeoutError(f"等待超时：{label or expression}")

    async def click(self, selector: str) -> None:
        await self.eval(
            f"(() => {{ const node = document.querySelector({json.dumps(selector)});"
            " if (!node) throw new Error('missing selector'); node.click(); return true; })()"
        )

    async def text(self, selector: str) -> str:
        return await self.eval(
            f"document.querySelector({json.dumps(selector)})?.textContent || ''"
        )

    async def count(self, selector: str) -> int:
        return int(
            await self.eval(f"document.querySelectorAll({json.dumps(selector)}).length")
        )

    async def screenshot(self, name: str) -> None:
        SHOTS.mkdir(parents=True, exist_ok=True)
        result = await self.send(
            "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False}
        )
        (SHOTS / name).write_bytes(base64.b64decode(result["data"]))


async def login(browser: Browser, username: str, password: str) -> None:
    await browser.wait("document.readyState === 'complete' && !!document.getElementById('login-form')")
    if await browser.eval("document.body.dataset.authenticated === 'true'"):
        await browser.click("#logout-button")
        await browser.wait("document.getElementById('login-view').classList.contains('active-view')")
    values = {
        "username": json.dumps(username),
        "password": json.dumps(password),
    }
    await browser.eval(
        "(() => {"
        f"document.getElementById('login-username').value = {values['username']};"
        f"document.getElementById('login-password').value = {values['password']};"
        "document.getElementById('login-form').requestSubmit(); return true; })()"
    )
    await browser.wait(
        "document.body.dataset.authenticated === 'true' && "
        "document.getElementById('business-view').classList.contains('active-view')",
        timeout=25,
        label=f"登录 {username}",
    )


async def run(browser: Browser) -> None:
    await browser.send("Page.enable")
    await browser.send("Runtime.enable")
    await browser.send("Log.enable")
    await browser.send("Network.enable")
    await browser.send(
        "Emulation.setDeviceMetricsOverride",
        {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False},
    )
    await browser.send("Page.navigate", {"url": BASE + "/"})
    await browser.wait("document.readyState === 'complete' && !!document.getElementById('login-form')")
    check(
        "未登录时只显示登录页，导航隐藏",
        await browser.eval("getComputedStyle(document.querySelector('.topnav')).display === 'none'"),
    )
    await browser.eval("document.getElementById('login-username').focus()")
    for _ in range(2):
        await browser.send(
            "Input.dispatchKeyEvent",
            {"type": "keyDown", "key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
        )
        await browser.send(
            "Input.dispatchKeyEvent",
            {"type": "keyUp", "key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
        )
        await asyncio.sleep(0.1)
    active = await browser.eval("document.activeElement?.id || document.activeElement?.tagName")
    check("登录表单支持键盘 Tab 顺序", active not in {"login-username", ""}, str(active))
    await browser.screenshot("01-login.png")

    await browser.click('[data-auth-target="register"]')
    await browser.wait(
        "document.getElementById('register-view').classList.contains('active-view')"
    )
    check(
        "登录与注册是独立页面",
        await browser.eval(
            "!document.getElementById('login-view').classList.contains('active-view')"
        ),
    )
    await browser.screenshot("01b-register.png")
    await browser.click('[data-auth-target="login"]')
    await browser.wait("document.getElementById('login-view').classList.contains('active-view')")

    await browser.send("Page.navigate", {"url": BASE + "/#/register"})
    await browser.wait(
        "document.getElementById('register-view').classList.contains('active-view')"
    )
    check(
        "注册页支持 #/register 直接访问",
        await browser.eval("location.hash === '#/register'"),
    )
    await browser.send("Page.navigate", {"url": BASE + "/#/login"})
    await browser.wait("document.getElementById('login-view').classList.contains('active-view')")

    creds = credentials()
    await login(browser, "alice", creds["alice"])
    identity = (await browser.text("#identity-bar")).lower()
    check(
        "登录后身份栏显示后端身份",
        "alice" in identity and "爱丽丝" in identity,
        identity,
    )
    await browser.screenshot("02-alice-workbench.png")

    await browser.click('[data-view="orders"]')
    await browser.wait("document.querySelectorAll('#order-list tbody tr').length === 4")
    check("客户订单页只显示自己的 4 笔订单", await browser.count("#order-list tbody tr") == 4)
    unauthorized = await browser.eval(
        "fetch('/api/v1/orders/order-2026-0005').then(r => r.status)", await_promise=True
    )
    check("浏览器内访问他人订单返回 404", unauthorized == 404, str(unauthorized))
    await browser.screenshot("03-alice-orders.png")

    await browser.click("#order-list [data-open-order]")
    await browser.wait(
        "document.getElementById('order-detail-view').classList.contains('active-view')"
    )
    check(
        "订单详情进入独立页面",
        await browser.eval(
            "!document.getElementById('orders-view').classList.contains('active-view')"
        ),
    )
    await browser.screenshot("03b-order-detail.png")
    await browser.click("#back-to-orders")
    await browser.wait("document.getElementById('orders-view').classList.contains('active-view')")

    await browser.click('[data-view="devices"]')
    await browser.wait("document.querySelectorAll('#device-list tbody tr').length === 4")
    check("客户设备页只显示自己的 4 台设备", await browser.count("#device-list tbody tr") == 4)
    await browser.screenshot("04-alice-devices.png")

    await browser.click("#device-list [data-open-device]")
    await browser.wait(
        "document.getElementById('device-detail-view').classList.contains('active-view')"
    )
    check(
        "设备详情进入独立页面",
        await browser.eval(
            "!document.getElementById('devices-view').classList.contains('active-view')"
        ),
    )
    await browser.screenshot("04b-device-detail.png")
    await browser.click("#back-to-devices")
    await browser.wait("document.getElementById('devices-view').classList.contains('active-view')")

    await browser.click('[data-view="tool-lab"]')
    await browser.wait("document.querySelectorAll('#tool-list .tool-item').length === 8", timeout=35)
    await browser.eval(
        "(() => { const b = [...document.querySelectorAll('#tool-list .tool-item')]"
        ".find(item => item.textContent.includes('search_knowledge'));"
        " if (!b) throw new Error('search_knowledge missing');"
        " b.scrollIntoView({block: 'center'}); b.click(); return true; })()"
    )
    await browser.wait("document.querySelectorAll('#tool-parameters label').length === 2")
    check(
        "search_knowledge 只接受 query 与 top_k",
        await browser.eval(
            "[...document.querySelectorAll('#tool-parameters label')]"
            ".every(label => !label.textContent.includes('model_code'))"
        ),
    )
    await browser.screenshot("05-alice-support.png")

    await browser.click('[data-view="tickets"]')
    await browser.wait("document.querySelectorAll('#ticket-device option').length > 0")
    await browser.eval(
        "(() => {"
        " document.getElementById('ticket-subject').value = '浏览器验收：NX100 报 E17';"
        " document.getElementById('ticket-description').value = '真实浏览器提交工单，验证页面与后端服务复用。';"
        " document.getElementById('ticket-form').requestSubmit(); return true; })()"
    )
    await browser.wait(
        "document.querySelector('#ticket-detail h2')?.textContent?.startsWith('T')",
        timeout=30,
        label="创建工单",
    )
    ticket_no = await browser.text("#ticket-detail h2")
    check("页面创建真实工单并展示详情", ticket_no.startswith("T"), ticket_no)
    check(
        "工单详情进入独立页面",
        await browser.eval(
            "document.getElementById('ticket-detail-view').classList.contains('active-view') && "
            "!document.getElementById('tickets-view').classList.contains('active-view')"
        ),
    )
    check(
        "创建成功后工单已有初始处理记录",
        await browser.count("#ticket-detail .event-list li") >= 1,
    )
    pending = await browser.eval(
        "sessionStorage.getItem('after_sales_pending_operation_v1')"
    )
    check("创建成功后清空本地操作键", pending is None, str(pending))
    await browser.screenshot("06-alice-ticket.png")

    await browser.click('[data-view="tool-lab"]')
    await browser.wait("document.querySelectorAll('#tool-list .tool-item').length === 8", timeout=35)
    await browser.click('[data-tool="list_orders"]')
    await browser.eval(
        "(() => { document.getElementById('tool-execute-form').requestSubmit(); return true; })()"
    )
    await browser.wait(
        "document.getElementById('tool-result').textContent.includes('\"status\": \"success\"')",
        timeout=25,
        label="工具执行",
    )
    tool_output = await browser.text("#tool-result")
    check("工具调试页真实执行 list_orders", '"total": 4' in tool_output, tool_output[:200])
    check("工具页显示后端身份与耗时", "耗时" in await browser.text("#tool-observation-meta"))
    await browser.screenshot("07-alice-tool-lab.png")

    await browser.send(
        "Emulation.setDeviceMetricsOverride",
        {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True},
    )
    await asyncio.sleep(0.4)
    overflow = await browser.eval("document.documentElement.scrollWidth")
    check("窄屏页面无横向溢出", int(overflow) <= 440, str(overflow))
    await browser.screenshot("08-alice-narrow.png")
    await browser.send(
        "Emulation.setDeviceMetricsOverride",
        {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False},
    )

    await browser.click("#logout-button")
    await browser.wait("document.getElementById('login-view').classList.contains('active-view')")
    await login(browser, "bob", creds["bob"])
    await browser.click('[data-view="orders"]')
    await browser.wait("document.querySelectorAll('#order-list tbody tr').length === 4")
    bob_identity = (await browser.text("#identity-bar")).lower()
    check("切换账号后身份栏显示 bob", "bob" in bob_identity, bob_identity)
    bob_unauthorized = await browser.eval(
        "fetch('/api/v1/orders/order-2026-0001').then(r => r.status)", await_promise=True
    )
    check("bob 无法访问 alice 的订单", bob_unauthorized == 404, str(bob_unauthorized))
    await browser.screenshot("09-bob-orders.png")

    await browser.click("#logout-button")
    await browser.wait("document.getElementById('login-view').classList.contains('active-view')")
    await login(browser, "admin", creds["admin"])
    await browser.click('[data-view="orders"]')
    await browser.wait("document.querySelectorAll('#order-list tbody tr').length === 12")
    check("管理员订单页显示全部 12 笔订单", await browser.count("#order-list tbody tr") == 12)
    await browser.screenshot("10-admin-orders.png")

    await browser.click('[data-view="customers"]')
    await browser.wait("document.querySelectorAll('#customer-list tbody tr').length === 3")
    check("管理员客户查询返回 3 位客户", await browser.count("#customer-list tbody tr") == 3)

    await browser.click('[data-view="knowledge"]')
    await browser.wait("document.querySelectorAll('#kb-list [data-open-kb]').length >= 2")
    await browser.eval(
        "(() => { const b = [...document.querySelectorAll('#kb-list [data-open-kb]')]"
        ".find(item => item.textContent.includes('售后开放资料'));"
        " if (!b) throw new Error('support kb missing'); b.click(); return true; })()"
    )
    await browser.wait(
        "document.querySelectorAll('#document-list [data-toggle-support]').length === 6",
        timeout=40,
    )
    open_count = await browser.eval(
        "[...document.querySelectorAll('#document-list [data-toggle-support]')]"
        ".filter(b => b.dataset.open === 'true').length"
    )
    check("知识库文档列表显示 6 份已开放资料", open_count == 6, str(open_count))
    await browser.screenshot("11-admin-support.png")
    await browser.eval(
        "(() => { [...document.querySelectorAll('#document-list [data-toggle-support]')]"
        ".find(b => b.dataset.open === 'true').click(); return true; })()"
    )
    await browser.wait(
        "[...document.querySelectorAll('#document-list [data-toggle-support]')]"
        ".filter(b => b.dataset.open === 'true').length === 5",
        timeout=30,
    )
    check("文档列表可关闭单份资料的售后开放", True)
    await browser.eval(
        "(() => { [...document.querySelectorAll('#document-list [data-toggle-support]')]"
        ".find(b => b.dataset.open === 'false').click(); return true; })()"
    )
    await browser.wait(
        "[...document.querySelectorAll('#document-list [data-toggle-support]')]"
        ".filter(b => b.dataset.open === 'true').length === 6",
        timeout=30,
    )
    check("文档列表可重新开放该资料", True)

    await browser.click('[data-view="tickets"]')
    await browser.wait("document.querySelectorAll('#ticket-list tbody tr').length >= 12", timeout=35)
    await browser.eval(
        "(() => { const row = [...document.querySelectorAll('#ticket-list tbody tr')]"
        ".find(item => item.textContent.includes('待处理'));"
        " if (!row) throw new Error('no open ticket');"
        " row.querySelector('[data-open-ticket]').click(); return true; })()"
    )
    await browser.wait("!!document.getElementById('ticket-event-form')")
    before_version = int(
        await browser.eval("document.getElementById('ticket-event-version').value")
    )
    await browser.eval(
        "(() => {"
        " const status = document.getElementById('ticket-event-status');"
        " if (![...status.options].some(o => o.value === 'in_progress')) return false;"
        " status.value = 'in_progress';"
        " document.getElementById('ticket-event-message').value = '浏览器验收：管理员开始处理';"
        " document.getElementById('ticket-event-form').requestSubmit(); return true; })()"
    )
    await browser.wait(
        f"Number(document.getElementById('ticket-event-version')?.value) > {before_version}",
        timeout=40,
        label="管理员处理工单",
    )
    check(
        "管理员页面提交处理记录并递增版本",
        int(await browser.eval("document.getElementById('ticket-event-version').value"))
        > before_version,
    )
    await browser.screenshot("12-admin-ticket-processing.png")

    await browser.send("Page.reload", {"ignoreCache": True})
    await browser.wait(
        "document.body.dataset.authenticated === 'true' && "
        "document.getElementById('business-view').classList.contains('active-view')",
        timeout=25,
        label="刷新后会话保持",
    )
    admin_identity = (await browser.text("#identity-bar")).lower()
    check("刷新后仍保持管理员会话", "admin" in admin_identity, admin_identity)

    await browser.send("Network.clearBrowserCookies")
    await browser.send("Page.reload", {"ignoreCache": True})
    await browser.wait(
        "document.getElementById('login-view').classList.contains('active-view') && "
        "document.body.classList.contains('not-authenticated')"
    )
    check(
        "清除会话后回到登录页并隐藏导航",
        await browser.eval("getComputedStyle(document.querySelector('.topnav')).display === 'none'"),
    )
    check("清除会话后业务 DOM 被清空", await browser.count("#order-list tbody tr") == 0)
    await browser.screenshot("13-session-cleared.png")


def launch_edge(profile: str):
    return subprocess.Popen(
        [
            str(EDGE),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--remote-debugging-port={DEBUG_PORT}",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def main() -> int:
    if not EDGE.exists():
        print(f"未找到 Edge：{EDGE}", file=sys.stderr)
        return 2
    profile = tempfile.mkdtemp(prefix="after-sales-edge-")
    process = launch_edge(profile)
    try:
        target = None
        for _ in range(80):
            try:
                with httpx.Client(timeout=2) as client:
                    client.put(
                        f"http://127.0.0.1:{DEBUG_PORT}/json/new?"
                        + quote(BASE + "/", safe="")
                    ).raise_for_status()
                    targets = client.get(f"http://127.0.0.1:{DEBUG_PORT}/json/list").json()
                    target = next(
                        item for item in targets if item.get("type") == "page"
                    )
                    break
            except Exception:
                await asyncio.sleep(0.25)
        if target is None:
            print("Edge 调试端口未就绪", file=sys.stderr)
            return 2
        async with websockets.connect(
            target["webSocketDebuggerUrl"], max_size=20 * 1024 * 1024
        ) as ws:
            browser = Browser(ws)
            await run(browser)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

    errors = [
        event
        for event in EVENTS
        if event["method"] == "Runtime.exceptionThrown"
        or (
            event["method"] == "Log.entryAdded"
            and event["params"]["entry"].get("level") == "error"
        )
    ]
    for event in errors[:5]:
        print("BROWSER-ERROR", json.dumps(event["params"], ensure_ascii=False)[:500])
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
