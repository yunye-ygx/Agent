# 打印机售后业务底座：运行与操作说明

本文件说明 2026-10-26 交付的用户、订单、设备、工单、开放资料、真实工具和页面如何启动、准备账号和验收。完整的实际测试结果与未验证项见 [验证记录](after-sales-business-verification.md)。

## 1. 已交付范围

已交付：

- 用户注册、登录、退出、服务端会话、CSRF、来源校验、数据库限速。
- 客户只能读取自己的订单、设备和工单；管理员可查询客户、录入订单、登记设备、处理工单、在知识库文档列表开关售后资料开放。
- 订单金额使用 `Decimal` 计算并持久化；订单、设备、工单的真实关联和约束。
- 工单创建使用 `business_operations` 预留键、事务行锁和幂等结果；处理记录使用版本乐观并发检查。
- 客户只检索管理员开放且已完整发布建库的售后资料；检索范围在 ES/向量召回内过滤并在返回前复核。
- 8 个真实工具、工具目录 JSON Schema、工具调试页；创建工单工具必须使用后端操作键。
- 原生页面上的登录、身份栏、账号切换、订单/设备/工单详情、知识库文档开放开关、客户、工具调试。
- 可重复执行的合成业务数据初始化、售后资料开放标记导入、管理员创建、密码重置、过期会话清理命令。
- 真实 MySQL 集成测试、HTTP 验收脚本、Edge 无头浏览器验收脚本。

本次**不包含**：

- 完整 Agent 对话循环、模型自动选题/规划、会话历史持久化、任务检查点与恢复。
- 支付、购物车、库存、物流、通知发送、企业多租户。
- 对已有离线建库 Task 表用途的改变；业务表与任务表是两套独立边界。

## 2. 架构与代码位置

```text
页面 / 工具调试页
  → 后端会话认证（Cookie + CSRF + Origin）
  → 业务服务（app/business）
  → MySQL（十张业务表）
知识工具
  → documents.support_open 开放开关（型号由 Agent 澄清）
  → 已发布 generation / embedding 配置
  → Elasticsearch
```

| 位置 | 作用 |
| --- | --- |
| `app/business/models.py` | 十张业务表模型，独立 `BusinessBase`，启动不 `create_all` |
| `migrations/20261026_after_sales_business.sql` | 唯一业务 DDL 来源，只新增表 |
| `app/db/business_schema.py` | 只读结构校验；缺表/缺列/缺唯一键时启动失败 |
| `app/auth/` | Argon2、服务端会话、CSRF、共享数据库限速 |
| `app/business/*.py` | 订单、设备、工单、操作键、开放资料服务 |
| `app/tools/` | 8 个真实工具与统一目录 |
| `app/api/routes/` | `/api/v1/*` 页面接口与工具调试接口 |
| `app/web/auth.js`、`business.js`、`tool-lab.js`、`business.css` | 新页面模块 |
| `app/cli/` | 迁移检查、管理员、数据初始化、资料导入、密码重置、过期清理 |
| `samples/after-sales-business-v1/` | 合成数据、清单、说明 |
| `tests/test_business_mysql_integration.py` | 真实 MySQL 约束/锁/回滚/幂等验收 |

## 3. 启动

### 3.1 依赖

- Python 3.11+（当前 3.12）
- Docker Desktop，项目根目录的 `docker-compose.yml`
- 可用的 MySQL、MinIO、Elasticsearch、RabbitMQ
- 已配置 embedding 服务凭据时，知识资料才能完成建库

```powershell
python -m pip install -e ".[dev]"
docker compose up -d
Copy-Item .env.example .env   # 仅首次；已有 .env 时不要覆盖
```

不要读取或提交 `.env` 中的密钥。`business_schema` 和导入命令只打印脱敏后的
`host:port/database`，不打印账号密码。

### 3.2 检查并应用迁移（已在本机执行）

```powershell
python -X utf8 -m app.cli.business_schema check
python -X utf8 -m app.cli.business_schema apply --yes
```

- `check` 只读；缺表时返回非零并提示迁移文件。
- `apply` 顺序执行 10 条 `CREATE TABLE IF NOT EXISTS`；不 `DROP`、不 `ALTER`
  既有表。MySQL DDL 非事务，失败可修复后重跑；脚本会跳过已存在的表并再次校验。
- 目标库由 `.env` 的 `MYSQL_HOST/PORT/DATABASE` 决定。当前本机为
  `127.0.0.1:13306/rag_demo`。执行前必须确认目标库；生产环境不要复用演示库。

### 3.3 启动 API

```powershell
python -X utf8 -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

打开 <http://127.0.0.1:8001/>。

本机开发会同时启动 Outbox Publisher、RabbitMQ Worker 和 A1 Maintenance。
如果 MinIO/ES/RabbitMQ 不可用，业务登录、订单、设备、工单仍可用；知识检索
明确返回不可用，后台 RAG 服务不会启动。MySQL/业务表不可用时启动失败或
`/health/ready` 返回 503，不伪装健康。

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health
Invoke-RestMethod http://127.0.0.1:8001/health/ready
```

`/health` 是进程存活；`/health/ready` 检查业务数据库，并单独报告 RAG 组件。

## 4. 账号准备

### 4.1 管理员

公开注册只能创建 customer。管理员必须用专用命令创建，密码交互输入：

```powershell
python -X utf8 -m app.cli.create_admin admin
```

密码 12～128 字符，两次输入；不会写入命令行参数、源码、文档或日志。
自动化/本地密钥工具可把密码从标准输入传入：

```powershell
Get-Content .\local-admin-secret.txt | python -X utf8 -m app.cli.create_admin admin --password-stdin
```

`local-admin-secret.txt` 不应提交到 Git。

### 4.2 客户

- 正常流程：在登录页“注册客户账号”，只能得到 `customer` 角色；用户名会转小写。
- 合成样例数据需要固定用户 ID，因此由 `seed_business --create-users` 创建
  `alice`、`bob`、`carol`。密码逐账号交互输入，或通过 `--password-stdin`
  按账号顺序从标准输入提供。
- 当前本机初始化时密码由操作者本地生成，没有写入仓库或本说明。需要调整时使用：

```powershell
python -X utf8 -m app.cli.set_password admin
python -X utf8 -m app.cli.set_password alice
```

重置密码会撤销该账号现有登录会话。

### 4.3 合成数据

```powershell
python -X utf8 -m app.cli.seed_business --dry-run --as-of 2026-10-26
python -X utf8 -m app.cli.seed_business --apply --yes --create-users
```

- 1 管理员（`admin`）、3 客户（`alice`/`bob`/`carol`）、12 订单、12 明细、
  18 设备、12 工单、35 条处理记录。
- 数据文件：`samples/after-sales-business-v1/`；全部为合成练习数据。
- 基准日期固定为 `2026-10-26`；`--as-of` 必须与 manifest 一致，设备购买/保修
  日期按基准日相对生成。
- 以固定 ID 和自然唯一键防重：重复执行只跳过，不重复插入、不覆盖已修改内容。
  同名/同编号但 ID 不同会停止并报告。
- 密码不在样例 JSON 中；初始化时交互输入或从标准输入读取。

## 5. 开放售后资料

清单：`samples/after-sales-business-v1/knowledge-manifest.json`

- 只允许 corpus 的 `01`～`06`，共 6 份文档。
- `07` 旧校准文档、`08`～`10` 历史工单和 `eval/` 评测题明确排除。
- 导入复用现有 Markdown 上传、parse run、Outbox/Worker、ES 建库链路，不另写
  一套灌库流程。
- 开放状态是 `documents.support_open` 一个字段，不再有型号关联表：
  只有 active generation 完整发布（`SUCCESS` + `artifacts_available` +
  `published_at`）后才会被置为开放；未发布文档不会进入检索范围。
- 型号维度不入库。Agent 应先用 `query_device` 拿到设备型号来限定问题，
  信息不足时先向用户澄清；检索层只保证“未开放文档不会被召回”。

```powershell
# 只校验清单/哈希，不连接数据库
python -X utf8 -m app.cli.import_support_knowledge --dry-run

# 上传缺失文档并触发建库
python -X utf8 -m app.cli.import_support_knowledge --apply --yes

# 建库完成后再次执行 apply 以按清单重建开放标记
python -X utf8 -m app.cli.import_support_knowledge --apply --yes
python -X utf8 -m app.cli.import_support_knowledge --status
```

说明：`--apply` 首次执行时建库是异步的，因此第一次通常只报告“未发布”；
Worker 完成后再次执行会把已发布文档置为开放，并关闭目标知识库以外的
残留开放标记（含历史错误关联）。当前本机 6 份资料均已发布 v1 并开放。
若 embedding 凭据缺失，建库会失败或停在未发布，命令会如实报告，不会把文档
标记为可检索。

数据库变更入口（已在本机执行）：`migrations/20261026b_documents_support_open.sql`
新增 `documents.support_open`、从旧关联表回填、删除 `support_knowledge_documents`；
脚本用 `information_schema` 守卫，可重复执行。

## 6. 页面操作

登录后身份栏显示服务端会话中的显示名、用户名和角色；客户看不到管理入口。

### 客户

1. **工作台**：订单/设备/工单计数。
2. **订单**：只看自己的订单；点“详情”进入独立详情页查看明细和总额，可返回列表。
3. **设备**：设备编号、型号、固件、保修状态（按 Asia/Shanghai 请求当日计算，
   截止日含当天）；过保设备仍可提交工单。点“详情”进入独立详情页。
4. **工单**：
   - 选设备、填主题和故障描述，点击“提交真实工单”。
   - 页面会先调用 `/api/v1/operations` 预留操作键，再用该键
     `POST /api/v1/tickets`。同一键重复提交返回同一工单；页面把操作键存在
     `sessionStorage`，成功后清除，失败时保留。
   - 结果未知时显示“查询操作结果”，先查询原键，再决定用原键重试；不会自动换键。
   - 工单详情是独立页面，包含处理记录和管理员处理表单，可返回列表。
5. **售后资料**：客户页面入口已按 2026-10-26 调整移除，改由工具/后续聊天承载。
   `search_knowledge` 只返回 `support_open` 且已发布的文档片段；型号不作为工具
   参数，Agent 需要先用 `query_device` 确认型号，或在信息不足时向用户澄清。
   检索依赖不可用时明确报告错误，不影响订单/工单。
6. **工具调试**：见第 7 节。

### 管理员

- **订单**：录入订单前先按用户名/显示名查找客户；添加 1～50 条明细；金额由
  后端用 Decimal 计算。可标记完成；有设备的订单不能取消。
- **设备**：先按订单号找明细，再从明细登记设备；型号必须匹配，累计登记数量
  不能超过购买数量，编号不能重复。
- **工单**：查看所有客户工单；管理员处理表单必须填写说明，可选合法状态变更；
  使用 `expected_version` 做乐观并发检查，冲突会刷新详情。
- **客户**：分页查询客户最少字段。
- **资料开放开关**：在既有知识库详情页的文档列表里，每份文档有一列“售后开放”，
  用“开放售后/停止开放”按钮切换（`PATCH /api/v1/documents/{id}/support`）。
  只有已发布文档能开放；不新增独立页面。
- 既有**知识库**、**检索实验**、**模型配置**、**聊天界面壳子**仍保留；前三个
  已统一要求管理员登录，既有流程加回归测试。聊天仍只是界面，不伪造回答。

## 7. 工具与调试页

工具目录：`GET /api/v1/tool-lab/tools`。执行：`POST /api/v1/tool-lab/execute`。

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `list_orders` | page、page_size | 当前身份可访问的订单摘要 |
| `query_order` | order_no | 订单及明细 |
| `list_devices` | page、page_size | 可访问设备摘要 |
| `query_device` | serial_no | 型号、固件、保修、关联订单 |
| `list_tickets` | status、page、page_size | 可访问工单摘要 |
| `query_ticket` | ticket_no、events_page | 工单及分页处理记录 |
| `create_ticket` | serial_no、subject、description | 真实创建工单，操作键来自信封 |
| `search_knowledge` | query、top_k | 已开放文档的片段及来源；型号由 Agent 澄清 |

调试页：

- 显示当前真实登录身份；没有输入其他用户/角色的控件。
- 参数表单由服务端 JSON Schema 生成。
- 展示 Observation、耗时、实际尝试次数、`request_id`、`next_action`。
- 创建工单会先自动预留操作键；同一参数再次执行复用原键；可“清除操作键”
  后主动新建。结果 `unknown` 时必须点击“查询最近操作结果”，不能换键重试。
- 关键边界：不做任意 SQL/代码执行；工具从可信注册表解析；身份与权限不来自
  页面或模型参数；写工具缺少操作键时在执行前拒绝。

## 8. 测试与验收命令

### 离线测试

```powershell
python -m pytest -q -p no:cacheprovider
python -m ruff check app tests --no-cache
```

MySQL 集成测试未提供专用库时会 skip，不会用 SQLite 冒充 MySQL 验收。

### 隔离 MySQL 集成

只允许专用 `_test` 库，禁止使用应用库，禁止 `drop_all`：

```powershell
$env:BUSINESS_TEST_MYSQL_HOST = "127.0.0.1"
$env:BUSINESS_TEST_MYSQL_PORT = "13306"
$env:BUSINESS_TEST_MYSQL_USER = "rag_user"
$env:BUSINESS_TEST_MYSQL_PASSWORD = "***"
$env:BUSINESS_TEST_MYSQL_DATABASE = "rag_demo_business_test"
python -m pytest tests/test_business_mysql_integration.py -q -p no:cacheprovider
```

当前本机已创建 `rag_demo_business_test` 并实际运行 6 项测试。测试只创建/清理
自己的 `itest-` 记录，不接触 `rag_demo`。

### 真实 HTTP 验收

```powershell
$env:AFTER_SALES_ADMIN_PASSWORD = "***"
$env:AFTER_SALES_ALICE_PASSWORD = "***"
$env:AFTER_SALES_BOB_PASSWORD = "***"
python -X utf8 tools/verify_after_sales_api.py
```

覆盖登录、身份隔离、越权 404、开放开关（关闭后不召回、客户无权调整）、
`search_knowledge` 参数契约、操作键幂等、参数冲突、处理记录版本冲突、工具执行、
退出后会话重放 401。

### 真实浏览器验收

```powershell
$env:AFTER_SALES_ADMIN_PASSWORD = "***"
$env:AFTER_SALES_ALICE_PASSWORD = "***"
$env:AFTER_SALES_BOB_PASSWORD = "***"
python -X utf8 tools/verify_after_sales_browser.py
```

使用本机 Microsoft Edge 无头模式和 Chrome DevTools Protocol，实际点击页面、
填写表单、切换账号、创建工单、执行工具、处理工单、刷新会话并检查 401 清理。
截图输出到 `docs/images/after-sales/`。

## 9. 故障排查

| 现象 | 检查 |
| --- | --- |
| 启动报“业务表尚未迁移” | 先 `business_schema check`，确认目标库后 `apply --yes` |
| 启动失败、`/health/ready` 503 | MySQL/业务表不可用；这不是 RAG 问题 |
| 登录 401 | 用户名/密码错误或会话过期；重新登录。统一提示不区分账号是否存在 |
| 写请求 403 | 缺少 CSRF 头、CSRF Cookie 与会话不匹配、Origin 不同源，或不是管理员 |
| 写请求 429 | 登录/注册限速；等窗口过期或运行 `python -m app.cli.cleanup_auth --apply --yes` |
| 知识检索 `KNOWLEDGE_UNAVAILABLE` | 查 `/health/ready` 的 RAG、embedding 配置、ES 索引、Worker 日志 |
| 知识检索 404 | 当前没有任何 `support_open=1` 的已发布文档；在知识库文档列表开放 |
| 知识检索 409/未发布 | 文档仍在建库或建库失败；用 `import_support_knowledge --status` 查看 |
| 启动报 documents 缺 support_open | 执行 `python -m app.cli.business_schema apply --yes --file migrations/20261026b_documents_support_open.sql` |
| 创建工单结果未知 | 保留操作键，`GET /api/v1/operations/{id}` 查询；成功返回原工单；reserved 可用原键重试 |
| 处理工单 409 | 版本已变化；页面刷新后按最新版本提交 |
| 数据初始化冲突 | 同名/同编号但 ID 不同，或文件哈希变化；人工核对，不覆盖 |
| 页面登录后空白 | 打开浏览器控制台；确认 `/auth/me`、静态 JS 和 Cookie 未被拦截 |

## 10. 安全与边界

- 会话令牌 32 字节随机值，数据库只存 SHA-256 摘要；固定 8 小时；退出撤销。
- Cookie `HttpOnly`、`SameSite=Lax`、`Path=/`；HTTPS 环境使用 `Secure`。
  CSRF Cookie 可被同源脚本读取，但服务端同时校验 Cookie、请求头和会话摘要。
- 所有认证写请求校验 Origin；缺少可信来源拒绝。登录/注册只接受 JSON。
- 登录来源按摘要计数；账号 15 分钟 10 次失败、来源 15 分钟 60 次尝试；
  注册来源每小时 10 次。计数在数据库，不使用进程内存字典。
- 客户资源查询在 SQL 中限定 `user_id`；不存在和他人资源都返回 404。
  管理接口统一要求管理员；工具执行从数据库重新解析角色/权限。
- 响应不缓存身份/业务数据；错误只返回安全 code/message/request_id。
- 不在源码、文档、日志或 Git 写入密码、完整 API Key、会话令牌、CSRF 值。

后续 Agent 工作可以直接基于：
`GET /api/v1/tool-lab/tools` 的 JSON Schema、`ToolRuntime`、业务服务和
`business_operations` 的操作键协议，实现对话循环、工具选择、`unknown` 核对和
持久化恢复；本轮没有实现这些编排能力。
