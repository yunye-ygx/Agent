# 工具执行器：第一步实现

> 本文前半部分记录最初的演示链路。2026-10-26 已接入真实售后业务工具：
> 目录与服务实现在 `app/tools/catalog.py`，调试入口是 `/tool-lab` 和
> `/api/v1/tool-lab/*`。真实工具查询/写入 MySQL，不再使用 fixture 数据。

先观察一条路径：模型提出调用 → Runtime 校验 → 执行工具 → observation。
执行器在 `app/tool_runtime.py`，可重复运行的输入在
`demos/tool_runtime_walkthrough.py`。

## 从一个现象开始

在项目根目录执行：

```powershell
python -X utf8 -m demos.tool_runtime_walkthrough --case invalid-arguments
```

模拟模型把订单编号传成数字 `100`。输出应出现 `INVALID_ARGUMENTS`，
实际工具执行次数为 **0**。这说明“模型提出了调用”不等于“工具已经执行”。

然后运行合法输入：

```powershell
python -X utf8 -m demos.tool_runtime_walkthrough --case success
```

此次执行次数为 **1**，返回带 `source: fixture` 的测试订单数据。
这些输入模拟模型输出，订单/写操作也是明确标注的测试数据；没有调用真实
LLM、订单系统或工单系统。校验、工具函数执行、超时和重试则走实际应用模块。

## 其他可观察场景

将 `--case` 换成以下值；`all` 可以一次运行全部：

| case | 预期现象 |
| --- | --- |
| `unknown-tool` | 拒绝未知工具，执行 0 次 |
| `forbidden` | 拒绝没有权限的调用，执行 0 次 |
| `retry` | 只读工具第一次暂时失败，第二次成功 |
| `read-timeout` | 两次查询均超时，返回 failed/TIMEOUT |
| `write-timeout` | 先发生一次模拟业务写入，再超时；返回 unknown，写入不回滚、不自动重试 |

演示故意使用 0.05 秒的短超时，不是生产环境推荐值。

## 代码阅读顺序

1. `OrderArguments`：工具参数的严格 Schema，多余字段也拒绝。
2. `ToolSpec`：注册允许的工具、权限、资源校验函数、超时和尝试次数。
3. `ExecutionContext`：后端提供用户身份、权限、业务操作 ID，不接收模型填入的身份。
4. `ToolRuntime.execute()`：校验 → 权限检查 → 调用 → 结果分类。
5. `Observation.to_tool_message()`：将合法调用的 observation 转成模型协议消息。

`max_attempts=2` 指总共最多执行两次（第一次 + 一次重试）。只读工具仅对
超时或适配器明确标为 `RetryableToolError` 的故障进行退避重试。
普通程序错误和业务拒绝不重试。写操作本阶段不自动重试；超时、无法判断
执行结果的异常都返回 `unknown` 和原业务操作 ID。

`ToolRejected` 只能表达“确认没有提交副作用”的业务拒绝，其消息必须是
适配器准备好的安全公开文本。其他异常不把原始消息/堆栈送给模型。

## 已实现与接入边界

- 不创建 Task、不投递 MQ、不保存检查点；这是双方讨论中的基础工具执行层。
- 参数格式错误、权限不足的调用不会进入业务函数。真实身份认证已由
  `app/auth/dependencies.py` 接入：后端从 HttpOnly 会话 Cookie 读取用户，
  调试页/模型参数中的 `user_id`、角色和权限一律不能成为执行身份。
- `authorize` 只做快速本地检查；真实资源归属由异步适配器内的业务服务在
  数据库边界再次校验。越权资源统一表现为未找到。
- 写操作要求后端事先提供 `operation_id`，业务去重由
  `business_operations` 表、事务行锁和唯一约束共同落实。相同键重复提交
  返回同一个工单；相同键改参数返回冲突；提交结果不明返回 `unknown` 和
  `reconcile`，必须先查原操作键，不能换新键重试。
- `next_action` 是给编排器和模型的提示，不是安全锁。未来 Agent 循环必须落实
  `unknown` 操作的核对流程，不能让模型换一个调用 ID 就重新发起相同写操作。
- 尝试次数限制属于单次 `execute()`。尚未接入模型循环，总轮数/总调用预算
  应由未来编排层限制。此阶段没有批量并发调度。
- 仅支持遵循取消协议、不会阻塞事件循环的 async I/O 工具。
  `asyncio.timeout` 依靠协作式取消：阻塞计算或吞掉取消的工具不能靠它强制终止。
  CPU/普通阻塞函数需单独的隔离适配器；停止本地等待不保证远端停止执行。
- 调用方取消会向上传播，不会被当成可重试错误。若写调用被取消，其结果同样
  可能未知，需要上层按原业务操作 ID 核对；此阶段不提供取消后的持久化恢复。
- 成功结果由适配器筛选可公开字段；Runtime 只验证可序列化为 JSON，不会自动
  识别成功数据中的隐私信息。格式损坏且无调用 ID 的模型输出不能生成协议
  `tool` 消息，应由模型响应解析层处理。

## 真实业务工具（2026-10-26）

| 工具 | 权限 | 效果 | 起始预算 | 真实数据来源 |
| --- | --- | --- | --- | --- |
| `list_orders` | `orders:read` | read | 5s × 2 | `orders` |
| `query_order` | `orders:read` | read | 5s × 2 | `orders` + `order_items` |
| `list_devices` | `devices:read` | read | 5s × 2 | `devices` + 订单 |
| `query_device` | `devices:read` | read | 5s × 2 | `devices` + 订单 |
| `list_tickets` | `tickets:read` | read | 5s × 2 | `tickets` |
| `query_ticket` | `tickets:read` | read | 5s × 2 | `tickets` + `ticket_events` |
| `create_ticket` | `tickets:write` | write | 10s × 1 | 事务写入工单、初始事件、幂等结果 |
| `search_knowledge` | `knowledge:search` | read | 20s × 1 | `documents.support_open` + 已发布版本 + ES（型号由 Agent 澄清） |

- 工具参数模型全部继承 `ToolArguments`，禁止额外字段和隐式类型转换。
- 工具目录与 JSON Schema 由 `app/tools/catalog.py` 的同一组 `ToolSpec`
  导出，调试页不隐藏就无法执行：`GET /api/v1/tool-lab/tools` 只返回当前
  身份有权限的工具。
- 调试接口 `POST /api/v1/tool-lab/execute` 只接收 call_id、工具名、参数和
  操作键；不接收任意 SQL、导入路径、身份、权限、超时或重试次数。
- 工具页展示 Observation、耗时、实际尝试次数、操作键和 `request_id`；
  创建工单会先调用 `/api/v1/operations` 预留，再用同一键执行，结果未知时
  保留原键并提供查询入口。

演示命令与业务工具的区别：

- `demos/tool_runtime_walkthrough.py` 使用 fixture 订单和故意制造的
  `RetryableToolError`/超时，只用于观察执行边界，不访问业务数据库。
- `app/tools/*.py` 是生产适配器，不含 sleep、失败开关或演示身份。

## 验证

```powershell
python -m pytest tests/test_tool_runtime.py tests/test_business_tools.py tests/test_tool_lab_api.py -q -p no:cacheprovider
python -X utf8 -m demos.tool_runtime_walkthrough --case all
```

真实页面验收见 [售后业务验证](after-sales-business-verification.md)。
