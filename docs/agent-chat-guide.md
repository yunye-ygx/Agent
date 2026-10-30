# 持久执行的 Agent 聊天链路

聊天不再由 HTTP 请求直接执行 Agent。一次用户提交在 MySQL 事务中创建
`Turn + Run + 用户消息 + OutboxEvent`，接口返回 `202`；Outbox Publisher 把
`run_id` 投递到 RabbitMQ；独立 Chat Worker 通过数据库租约领取 Run 并执行；
浏览器只通过 `GET /runs/{id}/events` 观察已持久化的事件。

```
Browser ──POST message + Idempotency-Key──▶ FastAPI
   FastAPI ──Turn + Run + Message + Outbox──▶ MySQL
   Chat Outbox Publisher ──run_id──▶ RabbitMQ
   Chat Worker ──claim/lease/fencing──▶ MySQL
   Chat Worker ──checkpoint──▶ PostgreSQL (LangGraph)
   Chat Worker ──messages/tools/events──▶ MySQL
   Browser ──GET events?Last-Event-ID──▶ SSE reader ──tail RunEvent──▶ MySQL
```

## 1. 职责边界

| 存储 | 负责 | 不负责 |
|---|---|---|
| MySQL | Turn、Run、工具执行、消息、事件、Outbox、租约、最终业务状态 | 不保存 LangGraph 图状态副本 |
| PostgreSQL | LangGraph checkpoint、pending writes、图恢复位置 | 不作为前端状态接口 |
| RabbitMQ | 通知 Worker“某个 Run 可以尝试领取” | 不承载 Run 状态或执行历史 |
| SSE | 传输已持久化的事件 | 不推进 Agent，不决定 Run 是否存活 |

四个 ID：

- `conversation_id`：一段对话。
- `turn_id`：一次用户意图；重试/重新生成仍在同一 Turn 下创建新 Run。
- `run_id`：一次完整执行尝试；同时是 LangGraph 的 `thread_id`。
- `tool_call_id`：模型协议中的一次工具调用。

`run.lease_generation` 是 fencing token；Worker 的每次写入都必须匹配
`lease_owner + lease_generation`，旧 Worker 在接管后无法再写消息、事件或终态。

## 2. 状态机

Turn：`active → completed | failed | cancelled | requires_attention`。
终态 Turn 可通过创建新 Run 回到 `active`。

Run：`queued → running → completed | failed | cancelled`；
租约过期由 Recovery Scanner 推进为 `recovering`；
用户取消进入 `cancelling`，Worker 到达安全边界后写 `cancelled`；
副作用结果未知且无法核对时进入 `requires_attention`。

不变量：

1. 一个 Conversation 最多一个非终态 Turn。
2. 一个 Turn 最多一个非终态 Run。
3. `run.attempt_no` 在 Turn 内从 1 递增。
4. `run_events.seq` 在 Run 内严格递增且不可修改。
5. `(run_id, tool_call_id)` 只能有一条 ToolExecution。
6. 完成事务同时写最终消息、`response.completed`、`run.completed`、Turn 终态并释放 Conversation。
7. 只有 Turn 选中的已完成 Run 进入下一轮模型历史。

## 3. 本地启动

依赖：MySQL、RabbitMQ、PostgreSQL（仅 checkpoint）。

```powershell
docker compose up -d mysql rabbitmq checkpoint-postgres
.\.venv\Scripts\python.exe -m app.cli.chat_schema check
# 首次或升级时（需要用户确认后才执行）：
.\.venv\Scripts\python.exe -m app.cli.chat_schema apply --yes
```

迁移是**只增结构**的 `migrations/20261029_durable_chat.sql`：新增
`chat_turns / chat_runs / chat_tool_executions / chat_run_events /
chat_outbox_events`，并给 `chat_messages` 增加 `run_id`、`source_key`。
旧聊天消息保持可读：没有 Turn 行的旧会话会回退到旧 `messages.status`
历史读取；旧消息不会伪造 Turn、Run 或 ToolExecution。

本地开发时 FastAPI lifespan 会在 MySQL、Chat RabbitMQ、Checkpoint PostgreSQL
都就绪后启动 Publisher、Worker、Scanner。Windows 上请使用项目启动器，
因为它会把事件循环固定为 Selector（psycopg 异步驱动不支持 Proactor）：

```powershell
.\.venv\Scripts\python.exe -m app.cli.serve --host 127.0.0.1 --port 8001
```

Linux 上可以直接 `uvicorn app.main:app`。生产环境拆成独立进程：

```powershell
.\.venv\Scripts\python.exe -m app.cli.chat_outbox
.\.venv\Scripts\python.exe -m app.cli.chat_worker
.\.venv\Scripts\python.exe -m app.cli.chat_recovery
```

`GET /health/ready` 分别报告 `business / rag / chat`；`chat.ready=false`
表示执行组件不可用，但已提交的 Run 仍留在 MySQL/Outbox，恢复后自动补发。

## 4. API

### 提交一条消息

```http
POST /api/v1/chat/conversations/{conversation_id}/messages
Idempotency-Key: 6e7bb01b-7c83-4f12-9ac9-e25b55ca4f10
Content-Type: application/json

{"content":"查一下我的订单"}
```

```http
202 Accepted
```

```json
{
  "conversation_id": "C1",
  "turn_id": "T1",
  "run_id": "R1",
  "status": "queued",
  "events_url": "/api/v1/chat/runs/R1/events"
}
```

`Idempotency-Key` 必须提供；同一 `(conversation_id, key)` 重试返回原
`turn_id/run_id`，不会重复写用户消息。会话忙时返回 `409 CHAT_BUSY`。

### 观察

```http
GET /api/v1/chat/runs/{run_id}/events
Last-Event-ID: 105
```

事件使用 `id: seq`、`event: type`、`data: json`。客户端断线后按
`Last-Event-ID`（或调试用 `?after=`）续读；多个观察者互不影响。

固定事件：`run.queued / run.started / run.recovering / run.cancelling /
response.started / response.restarted / response.completed / text.delta /
text.reset / tool.started / tool.completed / tool.failed /
tool.requires_attention / run.completed / run.failed / run.cancelled /
run.requires_attention`。

`response.completed` 携带完整权威文本；前端用它替换 delta 拼接结果，
避免最后一批 delta 丢失。`text.delta` 按字符数或时间批量落库。

### 查询、取消、重试

- `GET /api/v1/chat/runs/{run_id}`：可靠业务状态，不返回 lease/worker 细节。
- `POST /api/v1/chat/runs/{run_id}/cancel`：`running → cancelling`；
  `queued` 直接 `cancelled`。
- `POST /api/v1/chat/turns/{turn_id}/runs`
  `{"trigger_reason":"failure_retry"}`，允许
  `failure_retry / cancel_retry / regenerate`。
- `GET /api/v1/chat/turns/{turn_id}/runs`：执行历史。
- `GET /api/v1/chat/conversations/{id}/turns`：页面刷新后找到当前 Run。

消息列表只展示每个 Turn 选中的最新 Run；旧 Run 通过 runs 历史接口查看。

## 5. 恢复规则

- RabbitMQ 只负责通知；投递重复时由数据库 claim 去重，终态 Run 直接 ACK。
- Worker 在 **claim 提交后**才 ACK；执行中断由租约 + Scanner 接管。
- 心跳协程独立续租，不依赖模型/工具调用耗时；fencing 失败立即停止提交新工作。
- Recovery Scanner 推进 `recovery_count` 并发布 `recovery_requested`；
  再次 claim 必须匹配新的 `dispatch_generation`。
- 达到 `max_recovery_count` 后按最后状态收敛：
  取消中的 Run → `cancelled`；存在 `unknown/requires_attention` 工具 →
  `requires_attention`；否则 `failed (CHAT_RECOVERY_EXHAUSTED)`。
- 工具结果先写 MySQL，再交回 LangGraph；同 Run 恢复时
  `succeeded` 的工具直接复用，`pending/running` 的继续执行。
- 最终 checkpoint 已提交但 MySQL 终态未写时，恢复器通过
  `source_key=final:{model_response_id}` 幂等补投影。
- MySQL Run 已终态后，旧 Worker 的任何写入都被 fencing 拒绝。

## 6. 当前边界

- 聊天仍只开放只读工具；写工具需要 `operation_id`、业务幂等键和
  `reconcile` 流程，未在本版开放。
- 副作用结果未知时进入 `requires_attention`，不自动盲重试。
- 取消是协作式的：正在进行的模型调用要等它返回后才到安全边界；
  fencing 保证它无法再写业务状态。
- `chat_messages.status` 在兼容期保留，但不再是 Run/Turn 状态真相。
- PostgreSQL 不可用时 `chat.ready=false`，Run 保持 queued，不会假装完成。

## 7. 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_chat_store.py tests/test_chat_lifecycle.py `
  tests/test_chat_executor.py tests/test_chat_events.py tests/test_chat_outbox.py `
  tests/test_chat_worker.py tests/test_chat_recovery.py tests/test_chat_checkpoint_recovery.py `
  tests/test_chat_api.py -q
node --test tests/chat_frontend.test.cjs
.\.venv\Scripts\python.exe -m ruff check --no-cache app tests
```

PostgreSQL checkpoint 恢复测试需要显式测试库：

```powershell
$env:TEST_CHECKPOINT_POSTGRES_URL='postgresql://checkpoint_user:checkpoint_password@localhost:15432/checkpoint_db'
.\.venv\Scripts\python.exe -m pytest tests/test_chat_checkpoint_recovery.py -q
```

进程级/真实 broker 测试需要 `CHAT_E2E=1`（会启动独立 Worker/Scanner/Publisher 子进程，
用假模型执行，不调用真实 LLM）：

```powershell
$env:CHAT_E2E='1'
.\.venv\Scripts\python.exe -m pytest tests/test_chat_worker_process.py tests/test_chat_outbox.py -q
```

## 8. 验收场景与测试对照

| 计划验收场景 | 自动化测试 |
|---|---|
| 浏览器关闭后 Run 继续完成 | `test_closing_the_event_stream_does_not_stop_execution` |
| 刷新后按 Last-Event-ID 补齐 | `test_sse_replays_events_after_last_event_id`、`reloading an active run resumes EventSource...` |
| 同一幂等键不重复创建 Turn | `test_submit_returns_202_and_is_idempotent`、store 幂等用例 |
| API 进程退出不影响 Worker | `test_standalone_worker_completes_run_without_api`（独立 Worker 子进程，无 API） |
| A 完成、B/C 未执行时崩溃接管 | `test_abc_tool_batch_resumes_after_worker_crash`（真实 PG checkpoint + 租约接管） |
| 无 HTTP 请求时 Scanner 自动恢复 | `test_scanner_recovery_completes_without_any_http_request`、`test_recovery_scanner_restarts_an_expired_run_without_api` |
| 旧 Worker 写入被 fencing 拒绝 | `test_expired_lease_is_fenced...`、`test_event_writer_rejects_a_lost_lease` |
| RabbitMQ 不可用后 Outbox 补发 | `test_unreachable_broker_then_real_publish_after_recovery` |
| 重复投递不重复执行 | `test_ten_duplicate_deliveries_claim_and_execute_once` |
| 部分输出后 retry 不拼接错误文本 | `text.reset removes the partial segment...` |
| checkpoint 已写、MySQL 未投影时补投影 | `test_completed_checkpoint_without_mysql_projection_is_recovered` |
| 工具结果未知进入 requires_attention | `test_executor_marks_run_requires_attention_on_tool_unknown`、lifecycle 用例 |
| cancel → cancelling → cancelled | `test_cancel_running_run_waits_for_worker_safe_boundary`、API cancel 用例 |
| 失败重试在同一 Turn 建新 Run | `test_retry_requires_no_active_run_and_respects_attempt_limit`、API retry 用例 |
| 编辑重发创建新 Turn | `test_edit_and_resend_creates_a_new_turn_after_completion` |
| 后续 Turn 已存在时旧 Turn 不可重跑 | `test_only_latest_turn_can_be_re_executed` |
| 多个 SSE 客户端不改变执行 | `test_two_sse_observers_replay_the_same_events_without_mutating_run` |
| checkpoint 版本不兼容不恢复 | `test_checkpoint_version_mismatch_fails_without_building_agent` |
| Message / Run 终态 / 终态事件一致 | `test_execute_run_projects_final_answer_and_events`、`test_run_completion_event_carries_terminal_status_once` |
| 旧消息仍可读、旧 Run 不混入消息列表 | `test_legacy_messages_without_turns_remain_readable`、`test_messages_show_only_the_selected_run_of_a_turn` |
| 事件保留清理 | `test_retention_purge_removes_only_expired_events` |