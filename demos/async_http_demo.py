"""FastAPI HTTP 版本：观察 await、阻塞调用和普通 def 的差异。

启动：
    python -m uvicorn demos.async_http_demo:app --port 8000

然后在另一个终端运行：
    python demos/async_http_client.py --path /async
    python demos/async_http_client.py --path /blocking
    python demos/async_http_client.py --path /sync
"""

from __future__ import annotations

import asyncio
import time

from fastapi import FastAPI, Query


app = FastAPI(title="Python Async Demo")


def response(request_id: str, mode: str, started: float) -> dict[str, object]:
    return {
        "request_id": request_id,
        "mode": mode,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


@app.get("/async")
async def async_endpoint(
    request_id: str = "unknown",
    delay: float = Query(2.0, ge=0, le=10),
) -> dict[str, object]:
    """正确的异步等待：等待期间事件循环可以处理其他请求。"""

    started = time.perf_counter()
    print(f"/async  {request_id} start")
    await asyncio.sleep(delay)
    print(f"/async  {request_id} finish")
    return response(request_id, "await asyncio.sleep", started)


@app.get("/blocking")
async def blocking_endpoint(
    request_id: str = "unknown",
    delay: float = Query(2.0, ge=0, le=10),
) -> dict[str, object]:
    """反例：async def 里面的阻塞调用会卡住整个事件循环。"""

    started = time.perf_counter()
    print(f"/blocking {request_id} start")
    time.sleep(delay)
    print(f"/blocking {request_id} finish")
    return response(request_id, "time.sleep", started)


@app.get("/sync")
def sync_endpoint(
    request_id: str = "unknown",
    delay: float = Query(2.0, ge=0, le=10),
) -> dict[str, object]:
    """普通 def：FastAPI 通常会把它放入线程池执行。"""

    started = time.perf_counter()
    print(f"/sync  {request_id} start")
    time.sleep(delay)
    print(f"/sync  {request_id} finish")
    return response(request_id, "普通 def + time.sleep", started)
