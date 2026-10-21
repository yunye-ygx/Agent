# Python 异步对照 Demo

这个 Demo 观察的是“等待 I/O 时，当前执行流能不能去处理别的请求”，不是让 CPU 计算变快。

## 第一部分：纯 `asyncio` 实验

不启动服务，直接运行：

```powershell
python demos/async_vs_sync.py
```

默认会模拟 3 个请求，每个请求等待 2 秒：

- 同步：一个请求等待完，才开始下一个，约 6 秒。
- 真异步：3 个协程在第一次 `await` 处挂起，事件循环切换到其他协程，约 2 秒。
- 反例：虽然函数写成 `async def`，但里面调用 `time.sleep()`，事件循环无法切换，仍约 6 秒。

## 第二部分：真实 HTTP 请求

终端一启动服务：

```powershell
python -m uvicorn demos.async_http_demo:app --port 8000
```

终端二并发发送 3 个 HTTP 请求：

```powershell
python demos/async_http_client.py --path /async
python demos/async_http_client.py --path /blocking
python demos/async_http_client.py --path /sync
```

观察结果：

| 路径 | 后端代码 | 并发时的现象 |
|---|---|---|
| `/async` | `async def` + `await asyncio.sleep()` | 约 2 秒；等待时事件循环可处理其他请求 |
| `/blocking` | `async def` + `time.sleep()` | 约 6 秒；一个请求阻塞整个事件循环 |
| `/sync` | 普通 `def` + `time.sleep()` | FastAPI 通常放到线程池，可能约 2 秒；这是线程并发，不是协程让出 |

也可以加 `--sequential`，对比客户端按顺序发送请求：

```powershell
python demos/async_http_client.py --path /async --sequential
```

## 你应该从输出里记住什么

`async` 不是“自动开启一个新线程”，也不是“自动并行”。`async def` 定义的是协程函数；调用它会得到一个协程对象，真正运行通常需要 `await` 或交给 `asyncio.create_task()` / `asyncio.gather()`。

`await` 只能等待可等待对象，例如异步网络请求、异步数据库驱动和 `asyncio.sleep()`。遇到 `await` 时，当前协程暂停，事件循环可以运行其他就绪协程；等待结束后再回来继续执行。

因此，Python 异步和 Java 中常见的 `CompletableFuture`、线程池并不是同一层概念：Python `asyncio` 通常是单线程事件循环 + 协程协作式调度；Java 的异步代码经常依赖线程池中的线程切换。两者都可以实现“等待期间处理其他任务”，但底层执行模型不同。

最后，异步主要适合 I/O 密集型任务。如果把大量 CPU 计算直接放在事件循环线程里，依旧会阻塞其他协程；这时应考虑进程池、线程池或专门的任务队列。
