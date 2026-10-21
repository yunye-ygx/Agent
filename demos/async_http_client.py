"""并发调用 async_http_demo 的小客户端。"""

from __future__ import annotations

import argparse
import asyncio
import time

import httpx


async def call(client: httpx.AsyncClient, path: str, request_id: int, delay: float) -> dict:
    response = await client.get(
        path,
        params={"request_id": f"request-{request_id}", "delay": delay},
    )
    response.raise_for_status()
    return response.json()


async def main(base_url: str, path: str, request_count: int, delay: float, sequential: bool) -> None:
    started = time.perf_counter()
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        if sequential:
            results = [
                await call(client, path, request_id, delay)
                for request_id in range(1, request_count + 1)
            ]
        else:
            results = await asyncio.gather(
                *(call(client, path, request_id, delay) for request_id in range(1, request_count + 1))
            )

    mode = "sequential" if sequential else "concurrent"
    print(f"path={path}, mode={mode}, requests={request_count}, total={time.perf_counter() - started:.2f}s")
    for result in results:
        print(result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="调用 FastAPI 异步 Demo")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--path", choices=("/async", "/blocking", "/sync"), default="/async")
    parser.add_argument("--requests", type=int, default=3)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--sequential", action="store_true", help="按顺序发送请求")
    return parser.parse_args()


if __name__ == "__main__":
    options = parse_args()
    if options.requests < 1 or options.delay < 0:
        raise SystemExit("--requests 必须大于 0，--delay 不能小于 0")
    asyncio.run(
        main(
            options.base_url,
            options.path,
            options.requests,
            options.delay,
            options.sequential,
        )
    )
