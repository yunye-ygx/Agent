# 聊天 / Agent 工具调用链路架构评审

评审对象：`POST /api/v1/chat/conversations/{cid}/messages` 及其下游
（`app/chat/store.py` → `service.py` → LangChain `create_agent` → `ToolRuntime` → SSE → `app/web/chat.js`）。

评审日期：2026-10-28。评审方式：静态阅读 + 与成熟 Agent 产品的机制对照，未修改任何代码。

---

## 0. 结论

正常路径的设计**高于多数第一版**：原子抢轮、短事务、Runtime 与供应商解耦、
整轮取历史、不把上游异常喂给模型，这几件事很多项目第一版都做不对。

问题集中在两个维度，且都不是“再写一个 if”能解决的：

1. **时间维度**：超时、断线、取消、重试、恢复。当前一个 turn 的生命周期
   **等于一次 HTTP 连接的寿命**，这是绝大多数 P0 的根因。
2. **证据维度**：token 用量、finish_reason、执行轨迹、评测。Agent 的失败
   大多数不是抛异常，而是“答得不全 / 调错工具 / 引用了旧数据”，没有轨迹
   和评测就永远只能靠用户投诉来发现问题。

---

## 1. 已经做对、应当保留的

- `chat_conversations.active_turn` 的条件 UPDATE 是真正的跨进程互斥，不是内存 bool。
- `turn_id` 同时充当 **fencing token**：`_append()` 的 `WHERE active_turn == turn_id`
  保证“旧 writer 写不进去”。这一点在将来做租约回收/取消时是现成的地基。
- LLM 调用期间不持有数据库事务；消息逐条短事务落库。
- `ToolRuntime` 把“模型请求”和“是否允许执行”分开；Observation 是受控协议，
  上游异常字符串不下发给模型。
- 历史按整轮读取，不会把 tool 结果和它的 assistant 调用切断。
- 前端不上传身份和历史，账号切换有 `RequestScope` 失效机制。

---

## 2. P0：会产生错误答案、资损或不可用

### P0-1 被截断的回答被当成“完成”

**证据**

- `app/chat/agent.py:121-123` 只取 `content` 和 `tool_calls`，丢弃 `response_metadata`。
- `app/chat/service.py:89` `final_answer = not calls and bool(data["content"].strip())`
- `app/chat/agent.py:32` `max_tokens=4096`

**后果**

模型输出在第 4096 个 token 被截断时，`finish_reason == "length"`，但代码认为
“没有 tool_calls + 有文字 = 最终回答”，于是：

1. 半截回答被标记 `complete`，前端显示“回复已保存”；
2. 这半截回答进入下一轮历史，模型会把不完整结论当成既成事实继续推理；
3. 用户不知道答案被截断。

`finish_reason == "content_filter"`（内容过滤）同样会被当成正常完成。

**修复**

在 `stream_agent` 里把 `finish_reason` / `usage_metadata` 一并带出来：

```python
finish = (message.response_metadata or {}).get("finish_reason")
yield ("message", {..., "finish_reason": finish})
```

`stream_reply` 里：

```python
if data.get("finish_reason") == "length":
    # 方案 A：最多续写一次（追加“继续”指令，复用已有工具结果）
    # 方案 B：本轮标记 failed，错误码 CHAT_TRUNCATED，提示“回答过长，请拆分问题”
elif data.get("finish_reason") == "content_filter":
    raise ChatRejected("CHAT_CONTENT_FILTER", "该问题无法回答。")
```

**验收**：mock 一个 `finish_reason="length"` 的响应，断言不会发 `done`，
且该 assistant 消息不进入下一轮 `history()`。

---

### P0-2 供应商 429 / 5xx 一次就失败

**证据**：`app/chat/agent.py:30` `max_retries=0`，且整条链路没有第二层重试。

**后果**

国内 OpenAI 兼容端点（DashScope、vLLM、各类网关）在并发稍高时 429 非常常见。
现在一次 429 = 整轮 `CHAT_FAILED`，用户看到“本轮回复未完成”，重发又要重跑全部工具。
这是上线后第一周就会收到的投诉，而且日志里只留一个 `HTTPStatusError` 类型名。

**修复（按优先级）**

1. **开重试**：`max_retries=2`，只对 429/500/502/503/504，遵守 `Retry-After`，
   指数退避 + jitter。
2. **区分“还没吐字”和“吐了一半”**：前者重试完全安全；后者不能静默重试
   （用户已经看到文字），要么从头重生成并清屏，要么失败并让用户点“重新生成”。
3. **错误分类**：把供应商异常映射成稳定错误码
   `RATE_LIMITED | PROVIDER_TIMEOUT | CONTEXT_OVERFLOW | CONTENT_FILTER | PROVIDER_ERROR`，
   前端据此决定“稍后重试 / 拆分问题 / 联系管理员”。
4. **降级模型**：`model_configurations` 已经支持多条配置，选一条非默认的作为
   fallback，在 429/5xx 时切换，并把实际使用的模型记进 turn。

---

### P0-3 会话可能永久卡死；没有取消

**证据**

- `app/chat/store.py:61-72` 只有 `active_turn IS NULL` 才能抢到；没有租约、没有过期。
- 文档 `docs/chat-flow.md` 与 `docs/agent-chat-guide.md` 已承认“进程被 kill 后
  `active_turn` 残留，需要人工处置”，但**没有实现任何处置入口**。
- 全链路没有 cancel 端点；RAG 侧已有 `cancel_requested_at` + `request_generation_cancel`
  的成熟模式（`app/db/task_lifecycle.py:380`），聊天侧没有复用。

**后果**

- 一次 `kill -9`、一次 OOM、一次部署重启，某个用户的会话就永久 `busy`，
  前端输入框永久禁用，只能新建会话，历史上下文丢失。
- 用户发错问题、或模型开始跑长工具链时，**没有任何办法停止**。关掉标签页也不停
  （见 P0-5），token 继续烧。

**修复（推荐直接复用 RAG 任务的租约模式）**

```sql
ALTER TABLE chat_conversations
  ADD COLUMN active_turn_lease_until DATETIME NULL,
  ADD COLUMN active_turn_heartbeat_at DATETIME NULL,
  ADD COLUMN cancel_requested_at DATETIME NULL;
```

- `begin_turn`：`WHERE active_turn IS NULL OR active_turn_lease_until < now()`
  可回收；回收时换新 `turn_id`，旧 writer 后续写入自动被 `_append` 的
  `active_turn == turn_id` 挡住（**你已经有 fencing 了**）。
- 流式循环每个事件之间 `touch_turn_heartbeat`，租约 = 30s；单轮总预算单独限制。
- `cancel_requested_at` 在**每次模型调用前**和**每次工具调用前**检查：
  跨进程也能生效（不能只靠 `asyncio.Task.cancel()`）。
- 端点：`POST /conversations/{cid}/cancel`（本人）、`POST /conversations/{cid}/unlock`
  （管理员，用于历史脏数据）。
- 前端：发送按钮变“停止”。

**注意**：租约 TTL 必须大于单轮最大时长，否则会误回收一个“很慢但还活着”的轮次，
造成两个 writer。用 heartbeat + 足够长的 TTL + fencing 三者一起兜底。

---

### P0-4 历史预算按字符算，不是 token

**证据**

- `app/chat/store.py:133` `max_chars=60000`
- `app/chat/store.py:166` `sum(len(r.content) + len(json.dumps(r.tool_calls)))`
- `app/chat/agent.py:32` 输出固定 4096，没有任何输入预算
- `model_configurations` 没有 `context_window` / `max_output_tokens` 字段

**后果**

中文 60k 字符 ≈ 40k–60k token，加上 8k 用户输入、工具 observation、
system prompt 和 4096 输出预算，会直接超过 32k 甚至 8k 上下文模型的窗口。
表现是供应商返回 400，用户看到“本轮回复未完成”，而日志里只有一个异常类型名。
**这不是边界情况，是中文场景的默认情况。**

**修复**

1. `model_configurations` 增加 `context_window`、`max_output_tokens`（可从
   `capabilities` JSON 演进，但目前 `capabilities` 完全没被聊天用到）。
2. 用 tokenizer 计算预算（项目已依赖 `tiktoken`，RAG 侧在用）；按
   `context_window - max_output_tokens - 安全余量` 决定历史长度。
3. 工具 observation 也计入预算，并在截断时给出提示，而不是静默丢弃最早轮次。
4. 超预算时**明确告诉模型“早期对话已省略”**，否则模型会假装记得。

---

### P0-5 并行工具调用的顺序与配对没有验证

**证据**

- 模型可以一次返回多个 `tool_calls`；LangChain `ToolNode` 默认并发执行。
- `app/chat/service.py:92-94` 按 `updates` 到达顺序逐条 `append_message`，
  顺序 = 执行完成顺序，不保证等于 `tool_calls` 顺序。
- `app/chat/agent.py:72-83` `restore_messages` 按数据库 sequence 顺序恢复。
- `tests/test_chat_agent.py` 只测了单个工具，没有并行工具用例。

**后果**

部分 OpenAI 兼容服务（vLLM、部分网关、部分 Qwen 版本）要求 tool 消息与
assistant 的 `tool_calls` **一一对应且顺序一致**。一旦顺序错位，下一轮请求
直接 400，而这一轮的失败会**永久污染历史**——之后每次发消息都失败，用户完全
无法恢复（因为失败轮被排除，旧的成功轮仍然带着错位顺序）。

这是最难排查的一类故障：错误发生在“下一轮”，根因在“上一轮”。

**修复**

1. 在 `stream_agent` 的 assistant 消息里带上 `tool_calls` 顺序；落库时把
   `call_index` 一起存（或按 `tool_calls` 顺序重排后再写）。
2. `restore_messages` 前做一次**历史校验**：每个 `tool_call_id` 恰好一条 tool
   消息；orphan tool 消息丢弃；缺失的合成
   `{"status":"failed","code":"MISSING_TOOL_RESULT"}`。
3. 校验失败时降级（丢弃最近一轮工具块）而不是把 400 透传给用户。
4. 补测试：两个工具，一个 `sleep(0.2)`，断言数据库顺序 == `tool_calls` 顺序。

---

## 3. P1：上线后一定会遇到

### P1-1 delta 不落库，断线即丢答案

`docs/chat-flow.md` 明确“delta 不落库”。用户看了一半回答，刷新/切网/代理超时，
已生成的内容全部消失，数据库里只有一条 `failed` 的用户消息。用户会认为“钱白花了”。

成熟做法：把“正在生成的 assistant 消息”作为一行 `status='streaming'` 持续更新
（每 N 字符或每 1s 批量 upsert），断线后历史里能看到部分答案，并提供“继续生成”。
最差也要在 `finally` 里用新 session 保存已累积的 buffer（best-effort）。

### P1-2 没有 `tool_start`，长工具全程静默

`search_knowledge` 超时 20s。现在前端只在工具**结束后**收到 `message` 事件，
中间 20s 只有“正在处理…”。用户会以为卡死并疯狂点发送。

修复：`wrap_tool_call` 没法 `yield` SSE，需要旁路：给 `ExecutionContext` 或
上下文变量挂一个 `asyncio.Queue`，中间件执行前后 push `tool_start` / `tool_end`，
`stream_reply` 用 `asyncio.wait` 同时消费 agent 事件与队列事件。
这也是后面做“工具时间线 / 步骤展示”的基础。

### P1-3 失败轮不能重试；重发没有幂等键

- 失败轮的 user 消息 `status='failed'`，被 `history()` 排除；用户只能重新输入，
  于是数据库里出现两条一样的用户问题，上下文却不包含第一条。
- `POST /messages` 没有 `client_message_id`。网络超时后客户端重试，会在
  “上一轮刚好结束”的窗口里写入第二条用户消息。

修复：`chat_turns` 表 + `retry_turn(turn_id)`（复用原 user 消息、新 turn）；
`SendMessage` 增加 `client_message_id`，`(conversation_id, client_message_id)` 唯一。

### P1-4 turn 级事实无处存放

现在一个 turn 的状态全部隐含在 `chat_messages.status` + `active_turn` 里：

- 不知道这一轮用了哪个模型、哪版 prompt、多少 token、耗时、finish_reason、
  错误码、是否被取消；
- `finish_turn` 把整轮消息一起改状态，导致“assistant 完整但最终判定失败”这类
  中间状态无法表达。

建议新增 `chat_turns`（id, conversation_id, status, model, prompt_version,
finish_reason, error_code, input_tokens, output_tokens, started_at, finished_at,
lease_until, cancel_requested_at, request_id）。它同时是 P0-3、P1-3、P1-5、
可观测性和审计的共同地基。

### P1-5 没有 token 用量、成本、配额

`usage_metadata` 被丢弃；没有按用户/按天的用量统计；没有并发上限。
一个用户开 20 个标签页就是 20 路并发模型调用。建议：

- 每轮记录 input/output token（流式需要显式开 `stream_options.include_usage`
  或供应商等价参数，不是所有 provider 默认给）；
- 每用户每日 token 配额 + 每用户并发 1–2 路（超限返回 429 + `Retry-After`）；
- 全局模型并发信号量，避免把供应商打到限流。

### P1-6 没有 trace，只有一条 warning

`app/chat/service.py:106-111` 只记 `error_type`。`request_id` 中间件
（`app/main.py:106`）生成的 ID 没有传进 `stream_reply`，也没有进 SSE。
出问题无法回答“这个用户这一轮到底发了什么、模型看到了什么、调了哪些工具、各花多久”。

建议：结构化日志（conversation_id/turn_id/request_id/tool/latency/status）+
指标（turn 成功率、p95 延迟、工具错误率、token/成本）+ OTel/Langfuse/LangSmith
一类 trace（每轮一个 span，每次模型/工具一个子 span）。注意日志不要记 content。

### P1-7 历史截断是静默的

`app/chat/store.py:166-168` 超预算时从最早的轮次开始丢，模型完全不知道。
用户问“我上次说的那个订单”，模型会真的不记得，还一本正经地编。

修复：截断时插入一条系统可见的说明；更好的做法是“滚动摘要”（把更早的轮次
压缩成一段摘要，保留实体/结论），这是 Claude Code 等产品的标准做法。

### P1-8 工具输出没有大小上限

`list_orders(page_size=100)`、`search_knowledge(top_k=10)` 的结果直接进上下文。
现在检索侧截断到 2000 字符/条，但订单明细没有上限。建议在 Runtime 统一加
`max_observation_bytes`，超限截断并附 `truncated: true` + “请分页/缩小范围”。

### P1-9 模型能力没有校验

`ModelConfiguration.capabilities` 字段存在，但聊天完全没用。管理员把默认模型设成
不支持 tool calling 的模型、或上下文只有 8k 的模型，要到用户发第一条消息才炸，
错误还是“本轮回复未完成”。建议设置默认模型时就校验 `supports_tools` /
`supports_streaming` / `context_window`，并在聊天页显示当前模型。

### P1-10 没有删除 / 留存 / 合规

- 没有删除会话、重命名、清空的接口；
- `migrations/20261027_chat.sql:10` 外键没有 `ON DELETE CASCADE`，将来做
  “删除用户”会被 RESTRICT 挡住；
- `chat_messages` 里明文存着订单、设备、工单 observation（含客户姓名、电话等）；
- 没有留存期，表会无限增长。

建议：软删除 + 级联 + 留存任务（如 180 天）+ 隐私说明（“对话会被保存”）+
管理端访问审计（谁通过 Agent 看过谁的数据）。

---

## 4. P2：体验与工程细节

| # | 问题 | 位置 | 建议 |
|---|---|---|---|
| 1 | SSE 没有心跳注释，代理 60s 空闲会断 | `service.py` 全链路 | 每 15s 发 `:\n\n` |
| 2 | 没有 `id:` / `retry:`，无法 `Last-Event-ID` 续传 | 同上 | 事件带 `id: turn_id:seq` |
| 3 | 客户端断开不终止生成 | 无 `request.is_disconnected()` | 断开即置 cancel 标记 |
| 4 | 单轮没有总时长预算 | `recursion_limit=24` 只限步数 | 加 120s deadline + 明确错误码 |
| 5 | `pending.remove()` 遇到未知 id 抛 KeyError，整轮失败 | `service.py:91` | 用 `discard` + 按 id 去重 |
| 6 | 中间件先构造严格 `ToolCall`，畸形调用在进入 Runtime 前就抛异常 | `agent.py:57-59` | 把原始 dict 交给 `execute`，保留 `INVALID_CALL` 容错路径 |
| 7 | 流式过滤依赖内部节点名 `langgraph_node == "model"` | `agent.py:105` | 版本升级会静默失去流式；补 delta 断言测试 |
| 8 | 空回答 / 拒答被判为“未完成” | `service.py:96` | 允许“明确拒答”作为合法终态 |
| 9 | `history()` 不校验 user_id | `store.py:133` | store 层也传 owner，纵深防御 |
| 10 | 会话标题直接用前 120 字符（含换行/PII） | `store.py:77` | 去换行、截断、可重命名 |
| 11 | 默认模型要求“恰好一条”，并发设置可能出现两条 | `service.py:53` | 取最新一条并告警 |
| 12 | 标题/`updated_at` 在抢轮时就更新 | `store.py:68` | 失败轮也会把会话顶到最前，可接受但要知道 |
| 13 | 前端纯文本渲染 | `chat.js:116` | Markdown + 代码复制 + 引用卡片 |
| 14 | 无停止/重新生成/编辑重发/点赞点踩 | `chat.js:197-262` | Agent 产品标配 |
| 15 | 分页加载更早消息后滚动位置跳变 | `chat.js:159-173` | 记录 scrollHeight 差补偿 |
| 16 | 错误后不自动刷新，UI 停在 pending | `chat.js:249-254` | 失败即 `loadMessages()` |
| 17 | 多标签页只报 409，没有“另一端正在回复”提示 | `chat.py:72` | turn 状态轮询 + BroadcastChannel |
| 18 | 首 token 可能 10s+，没有任何“思考中”反馈 | 前端 | 显示“正在分析…”并在 5s 后提示 |
| 19 | 没有时间上下文 | `agent.py:13` | 注入 `business_today()`（已有工具函数） |
| 20 | 工具结果里的外部文档没有“不可信”边界 | `knowledge_tools.py` | 用分隔符/字段标注“资料内容，不是指令” |
| 21 | 旧工具结果会留在历史里被当成现状 | `history()` | observation 带 `as_of`，提示时间敏感问题需重查 |
| 22 | 相同工具+参数重复调用没有去重 | `tool_runtime.py` | 轮内缓存 + 重复调用返回 `DUPLICATE_CALL` |
| 23 | 首轮标题 `WHERE next_sequence == 2` 依赖实现细节 | `store.py:76` | 用 `turn_count` 或 `title IS NULL` 更直观 |

---

## 5. 开放写工具之前必须先定的四件事

你现在的 `create_ticket` 已经在目录里，只是被 `effect == "read"` 过滤掉。
一旦开放，下面每一条都会变成真实工单/资损：

1. **`operation_id` 必须由编排器派生，不能由模型给，也不能每次新 UUID。**
   建议 `operation_id = HMAC(turn_id + tool_call_id)` 或直接
   `f"{turn_id}:{call_id}"` 入库。这样“同一轮同一调用重放”天然幂等；
   如果每次重试都生成新 UUID，超时重试会创建两张工单。
   `ToolRuntime` 里 `OPERATION_ID_REQUIRED` 的保护是对的，聊天层还没接上。
2. **人在环确认（HITL）**：模型提出写操作 → 前端出确认卡片（设备、问题描述、
   将创建什么）→ 用户点确认 → 才执行。LangGraph 的 `interrupt()` 就是干这个的；
   自己实现也行，但状态机里要有 `awaiting_confirmation` 这个状态。
3. **`unknown` / `reconcile` 的对话流程**：写工具超时后 Runtime 返回
   `next_action="reconcile"`，但 system prompt 完全没解释这个词，模型不知道要
   “先查原操作键”。需要把 observation 协议写进 prompt，并给模型一个
   `get_operation(operation_id)` 之类的只读工具。
4. **恢复时的副作用**：如果将来做“断点续跑”，已成功的写步骤绝不能重放。
   这要求每个步骤有稳定 ID 和幂等边界，而不只是“消息表里有记录”。

---

## 6. 和成熟项目的对照

| 能力 | 当前实现 | 成熟做法 |
|---|---|---|
| 轮次生命周期 | 单字段 `active_turn`，无租约/取消/重试 | 独立 turn 表 + 状态机 + lease/heartbeat/reclaim（Temporal 风格） |
| 执行与连接 | 一个 POST 直出 SSE，连接即生命 | 后台执行 + 可续传事件流（OpenAI Responses `background`/retrieve、Vercel AI SDK resumable stream） |
| 截断/停止原因 | 丢弃 `finish_reason` | 显式 `stop_reason`（Anthropic）/`incomplete_details`（OpenAI），据此续写或报错 |
| 限流重试 | `max_retries=0`，无配额 | 429 退避 + `Retry-After` + fallback 模型 + 用户配额 |
| 上下文 | 字符数，静默丢最早轮 | token 预算 + 摘要压缩 + 告知模型（Claude Code compaction） |
| 工具过程可见 | 工具结束后才有事件 | `tool_start`/`tool_end` 实时事件（Claude Code、Codex） |
| 写操作 | 不开放 | HITL 审批 + 幂等键 + `reconcile`（LangGraph `interrupt`、OpenAI MCP approval） |
| 可观测 | 一条 warning | trace + token/成本 + 工具成功率 + 会话回放（LangSmith/Langfuse） |
| 评测 | 只有协议单测 | golden 对话集：工具选择、参数、拒答、注入、多轮指代 |
| 记忆 | 只回放最近 20 轮 | 滚动摘要 + 结构化用户上下文 |

---

## 7. 推荐的演进顺序

### 阶段 A（不改架构，1–2 天）

1. `finish_reason` 处理 + `usage` 记录（P0-1、P1-5 的一半）。
2. `max_retries=2` + 429 分类 + 稳定错误码（P0-2）。
3. token 预算替换字符预算，配置里加 `context_window`（P0-4）。
4. `pending` 用 `discard`，中间件容错，补并行工具测试（P0-5、P2-5/6）。
5. SSE 心跳 + `request_id` 进日志（P2-1、P1-6 的一半）。

### 阶段 B（一个迭代）

6. `chat_turns` 表 + 租约 + heartbeat + cancel 端点 + 前端停止按钮（P0-3、P1-4）。
7. `client_message_id` 幂等 + 失败轮重试（P1-3）。
8. delta 落库为 `streaming` 草稿（P1-1）。
9. `tool_start`/`tool_end` 旁路事件（P1-2）。
10. 删除/重命名/留存任务（P1-10）。

### 阶段 C（架构升级）

11. 把“开始一轮”和“订阅事件”拆开：
    `POST /turns` 返回 `202 + turn_id`，`GET /turns/{id}/events?last_event_id=`
    负责流式；执行在受租约保护的后台任务里。断线、多端、刷新、恢复一次性解决。
12. 用 LangGraph checkpointer（`thread_id=conversation_id`）做步骤级恢复；
    已完成的只读工具结果复用，失败只重跑最后一步。
13. HITL 写工具 + 确认卡片 + `reconcile` 工具。
14. golden 评测集 + 每次 prompt 变更跑回归。

---

## 8. 建议补的测试（按价值排序）

1. `finish_reason="length"` 不产生 `done`，且不进入下一轮历史。
2. 两个并行工具（一慢一快）落库顺序 == `tool_calls` 顺序。
3. 历史被供应商 400 拒绝时能降级/修复，而不是永久失败。
4. 客户端断开后：轮次在 N 秒内释放，token 停止消耗。
5. cancel 与 append 竞态：取消后旧 writer 的写入被 `CHAT_TURN_CLOSED` 挡住。
6. 租约过期回收后，老 writer 无法再写（类似现有 `late_writer` 测试）。
7. 429 → 重试成功；连续 429 → 明确错误码。
8. 中文 60k 字符历史 + 8k 输入不会触发供应商 400（预算正确）。
9. 同一 `client_message_id` 重发只产生一条用户消息。
10. 失败轮 retry 复用原用户消息，不产生重复问题。
11. 知识库文档内含“忽略以上指令”时，模型不执行写操作（注入用例）。
12. golden set：工具选择、越权查询拒答、多轮指代、引用来源。

---

## 9. 如果只做三件事

1. **给 turn 一个真正的状态机 + 租约 + 取消**——它决定所有其他机制能否成立。
2. **记录 `finish_reason` / token 用量 / 工具轨迹并接入 trace**——没有证据就无法改进。
3. **把“事件流”从“消息表”里解耦**——断线、多端、恢复、后台执行一次解决。

这三件事都不需要重写现有代码，但会让整条链路从“能跑的 Demo”变成“可以上线运维的 Agent”。