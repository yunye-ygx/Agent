# LangChain 售后聊天

聊天页使用 `chat_conversations` 保存用户会话，用 `chat_messages` 按序保存用户消息、模型工具调用、工具 observation 和最终回答。会话归属由服务端登录身份决定；API 不接受客户端提供的 `user_id` 或消息历史。

## 本地启动

```powershell
.\.venv\Scripts\python.exe -m app.cli.chat_schema check
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

新数据库执行 `python -m app.cli.chat_schema apply --yes`。迁移只增加聊天的两张表，DDL 在 [20261027_chat.sql](../migrations/20261027_chat.sql)。启动服务不会自动建表，也不会改变离线知识库 `tasks` 表。

项目额外依赖安装命令：

```powershell
python -m pip install -e ".[agent]"
```

管理员须在“模型配置”中设置一个默认聊天模型。第一版沿用项目里 `ModelConfiguration` 的供应商、密钥、Base URL 和模型名；密钥不会发到浏览器。模型响应超时 60 秒、SDK 自动重试关闭；工具超时和安全策略继续使用 `ToolRuntime` 的设置。

## 一轮消息如何执行

前端先创建会话并保存服务端返回的会话 ID。发送消息时只提交正文；后端从登录 Cookie 取用户身份，验证会话归属和 CSRF，原子占用本轮，然后加载已完成的历史消息。

LangChain 1.x 的 `create_agent` 运行模型和只读工具循环。模型看到的工具 schema 来自现有 `app/tools/catalog.py`。调用进入 Runtime 后，再由 Runtime 校验 Pydantic 参数、权限、业务逻辑、超时和只读重试。Runtime 从数据库重新读取当前用户授权，工具查询复用真实订单、设备、工单及发布知识库服务。聊天首版不允许模型创建工单；用户可从工单页执行写操作。

每个完成的模型工具调用或工具结果立即写入 `chat_messages`，每段生成文字经 SSE 发送给浏览器。仅当最后一条助手回答保存、完整轮完成后，接口才发送 `done`。错误会返回不含供应商原始响应的 `error` 事件。失败轮保存在会话页面供检查，但不会喂给下一轮模型；只有完整轮按 `user → assistant/tool → tool → assistant` 顺序进入后续历史。

核心实现入口：

- [chat.py](../app/api/routes/chat.py)：会话 REST 和流式消息接口。
- [store.py](../app/chat/store.py)：会话归属、原子轮次占用、有序消息和历史筛选。
- [agent.py](../app/chat/agent.py)：模型配置、LangChain Agent、Runtime 工具边界。
- [service.py](../app/chat/service.py)：模型消息持久化及 SSE 事件。
- [chat.js](../app/web/chat.js)：会话页、SSE 读取、账号切换隔离。

## API

- `POST /api/v1/chat/conversations` 创建会话，Cookie 会话及 CSRF 保护。
- `GET /api/v1/chat/conversations?offset=0` 列出当前用户会话。
- `GET /api/v1/chat/conversations/{id}/messages?before={sequence}` 读取最新或更早的 50 条消息。
- `POST /api/v1/chat/conversations/{id}/messages` 以 `{"content":"..."}` 开始一轮，返回 `text/event-stream`。事件为 `start`、`delta`、`message`、`done` 或 `error`。

GET 和 POST 均只返回当前登录用户拥有的会话。对他人会话统一返回 404。POST 每个会话同一时间只准一轮运行；并发发送返回 `CHAT_BUSY`。

## 当前边界

会话和消息表是聊天记录，不是独立持久化工作任务。服务在异常路径会将本轮标为失败，完成的消息仍可查看。进程被强制终止、连接中断后如何判定、释放占用并恢复运行尚未实现；旧 `active_turn` 可能需要管理员处置。它不会被伪装成可恢复成功。聊天当前只开放只读工具；一次返回大量明细时用户可继续追问或分页。

## 验证

使用项目虚拟环境运行完整本地回归（跳过会修改 MySQL 业务数据的既有集成套件）：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --ignore=tests/test_business_mysql_integration.py
node tests/chat_frontend.test.cjs
python -m ruff check app tests tools
```

测试覆盖消息顺序与权限隔离、并发占用、Runtime 工具调用、失败轮、SSE 拆包和跨登录请求失效。已用默认阿里云模型配置与 alice 的真实订单完成两轮真实联调。
