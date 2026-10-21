"""用最小的例子观察同步、异步，以及“假异步”的区别。

运行：
    python demos/async_vs_sync.py

默认会执行 3 个请求，每个请求等待 2 秒。同步部分约 6 秒，真正的异步
部分约 2 秒，而 async def + time.sleep() 仍然约 6 秒。
"""

from __future__ import annotations

import argparse
import asyncio
import threading
import time


def log(message: str) -> None:
    """同时打印相对时间和线程，方便观察调度过程。"""

    print(
        f"[{time.perf_counter() - PROGRAM_START:6.2f}s] "
        f"[{threading.current_thread().name}] {message}"
    )


def sync_request(request_id: int, delay: float) -> str:
    """同步请求：当前执行流会一直占着线程，直到 sleep 结束。"""

    log(f"sync request-{request_id} 开始等待 {delay:.1f}s")
    time.sleep(delay)
    log(f"sync request-{request_id} 完成")
    return f"sync-{request_id}"


async def async_request(request_id: int, delay: float) -> str:
    """真正的异步请求：await 会挂起当前协程，让事件循环运行别的任务。"""

    log(f"async request-{request_id} 开始等待 {delay:.1f}s")
    await asyncio.sleep(delay)
    log(f"async request-{request_id} 完成")
    return f"async-{request_id}"


async def blocking_async_request(request_id: int, delay: float) -> str:
    """反例：虽然写了 async def，但 time.sleep 仍然阻塞事件循环。"""

    log(f"blocking request-{request_id} 开始等待 {delay:.1f}s")
    time.sleep(delay)
    log(f"blocking request-{request_id} 完成")
    return f"blocking-{request_id}"


def run_sync(request_count: int, delay: float) -> None:
    log("=== 同步：逐个处理请求 ===")
    started = time.perf_counter()
    results = [sync_request(i, delay) for i in range(1, request_count + 1)]
    log(f"同步结果={results}，总耗时 {time.perf_counter() - started:.2f}s")


async def run_async(request_count: int, delay: float) -> None:
    log("=== 异步：并发运行请求 ===")
    started = time.perf_counter()
    results = await asyncio.gather(
        *(async_request(i, delay) for i in range(1, request_count + 1))
    )
    log(f"异步结果={results}，总耗时 {time.perf_counter() - started:.2f}s")


async def run_blocking_async(request_count: int, delay: float) -> None:
    log("=== 反例：async def + time.sleep ===")
    started = time.perf_counter()
    results = await asyncio.gather(
        *(blocking_async_request(i, delay) for i in range(1, request_count + 1))
    )
    log(f"反例结果={results}，总耗时 {time.perf_counter() - started:.2f}s")


async def main(request_count: int, delay: float) -> None:
    run_sync(request_count, delay)
    await run_async(request_count, delay)
    await run_blocking_async(request_count, delay)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Python 同步/异步最小对照实验")
    parser.add_argument("--requests", type=int, default=3, help="请求数量，默认 3")
    parser.add_argument("--delay", type=float, default=2.0, help="每个请求等待秒数，默认 2")
    args = parser.parse_args()
    if args.requests < 1:
        parser.error("--requests 必须大于 0")
    if args.delay < 0:
        parser.error("--delay 不能小于 0")
    return args


PROGRAM_START = time.perf_counter()


if __name__ == "__main__":
    options = parse_args()
    asyncio.run(main(options.requests, options.delay))
