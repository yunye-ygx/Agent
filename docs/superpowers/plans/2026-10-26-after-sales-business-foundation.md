# 打印机售后业务底座 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 superpowers:executing-plans 在当前会话按阶段执行。遵循用户本地覆写，不默认使用子代理、worktree 或提交 Git。复选框代表真实完成情况，未执行不得勾选。

> **2026-10-26 变更（用户决定，已实施）：知识开放改为单字段。** 删除第 4/6/7 节的「售后资料」客户检索页与「资料开放」管理页；`support_knowledge_documents` 表由 `documents.support_open` 一个字段取代（迁移 `migrations/20261026b_documents_support_open.sql`，含回填与 `DROP TABLE`，已在隔离库演练后对 `rag_demo` 执行）。型号维度不再入库：`search_knowledge(query, top_k)` 去掉 `model_code`，型号由 Agent 先用 `query_device` 限定或向用户澄清；`POST /support/knowledge/search` 与 `/admin/support-documents` 路由删除，开放开关改在既有知识库文档列表操作。下文相关条目保留为历史设计记录，实际以本变更为准。

**Goal:** 将用户、订单、打印机设备、工单、知识资料、真实工具和页面补齐，让用户把后续精力集中在 Agent 编排与恢复机制上。

**Architecture:** 一个 FastAPI 应用；HTTP 页面接口与工具适配器复用业务服务，SQLAlchemy async 访问 MySQL。认证身份从后端注入，业务服务统一检查资源归属。检索复用现有建库、Embedding 与 Elasticsearch；现有 RabbitMQ 只承担离线建库。

**Tech Stack:** 现有 Python/FastAPI、Pydantic、SQLAlchemy/asyncmy、MySQL、原生 HTML/CSS/JavaScript；密码哈希使用 Argon2；测试沿用 pytest、httpx 和 Ruff。新增依赖版本在实施时核对兼容性，不改现有依赖大版本。

---

## 1. 交付边界与当前状态

用户已选择：Python；订单＋设备＋工单；打印机售后；包含工具调试页；完整 Agent 对话留作后续学习。已授权本轮编写文档。本轮没有执行以下计划、安装依赖、改数据库或准备账号。

本计划替代旧草案中“只做用户订单”“只增加四张表”“设备工单后续做”的范围。原草案已同步更新。

本次完成实际 CRUD、关联数据、真实工具、页面、使用说明及验证。不会实现支付、购物车、库存、物流、通知发送、企业多租户、完整 Agent 对话循环或通用任务恢复系统。订单通过管理员录入，工单由客户提交、管理员处理。

现有代码核对结果：

- `app/main.py` 为入口；`app/web/` 是原生页面，聊天仅界面壳子。
- `app/db/mysql_models.py` 包含离线建库模型，尚无用户、订单、设备和业务工单。
- `app/db/mysql_schema.py` 启动时调用 `Base.metadata.create_all`；必须防止导入新模型后悄悄创建业务表。
- `app/tool_runtime.py` 已有严格参数校验、权限检查、超时、受控重试及 Observation；现有演示不是实际业务。
- `app/retrieval/access.py` 仅有本机实验会话，不能作为正式登录系统。
- `app/api/routes/retrieval.py` 含实际检索逻辑；需要小范围提取为可复用服务，不从工具直接调用 FastAPI 路由函数。
- `samples/rag-eval-after-sales-v1/` 已有澄印 NX100/NX200 手册和故障资料；保留其型号差异、固件版本差异和文档替代关系。
- 已有 `tests/conftest.py` 的 SQLite 包装 fixture 不证明 MySQL 行锁正确；并发验收单独使用隔离 MySQL。

## 2. 用户能实际完成什么

### 普通用户

注册、登录、退出；查看自己的订单、订单明细、设备、保修状态；根据设备编号检索开放的售后资料；创建工单、查看工单处理记录；使用工具调试页执行相同业务。

### 管理员

分页查找客户，录入客户订单及明细，按订单明细登记设备；查询工单、添加处理说明和更新状态；维护知识库，并明确选择哪些文档向售后用户开放。使用现有模型配置页。首位管理员通过专用命令创建，公开注册没有管理员入口。

### 主要操作链

```text
管理员录入订单 → 登记设备 → 客户查看订单与设备
客户选设备 → 输入故障 → 创建工单 → 管理员处理 → 客户查看记录
客户选工具 → 输入参数 → Runtime → 业务服务 → 数据库 → Observation
```

页面提交与工具提交都经过业务权限、幂等和事务约束。隐藏按钮只是界面行为，不承担权限保证。

## 3. 数据模型与一致性规则

新增十张表，不修改离线建库 tasks 的用途。ID 为 UUID 字符串；时间戳为 UTC；字符串长度明确；使用外键、唯一约束和必要索引。业务状态使用受约束的字符串枚举，避免无校验的自由文本状态。

| 表 | 核心字段 | 约束与查询索引 |
| --- | --- | --- |
| business_users | id、username、display_name、password_hash、role、status、created_at、updated_at | 规范化 username 唯一；role=customer/admin；status=active/disabled |
| auth_sessions | id、user_id、token_hash、csrf_token_hash、expires_at、revoked_at、created_at | token_hash 唯一；用户与有效期索引；不存明文会话令牌 |
| auth_rate_limits | bucket_key、window_started_at、attempt_count、expires_at | bucket_key 主键；过期索引；计数原子更新；来源标识使用摘要 |
| orders | id、order_no、user_id、created_by、status、total_amount、currency、created_at、updated_at | order_no 唯一；用户＋创建时间索引；金额 DECIMAL(14,2)，CNY |
| order_items | id、order_id、product_name、product_model、quantity、unit_price、line_amount | order_id 索引；单价 DECIMAL(12,2)；明细金额 DECIMAL(14,2) |
| devices | id、serial_no、user_id、order_item_id、model_code、firmware_version、purchased_on、warranty_until、status、created_at | serial_no 唯一；用户、订单明细索引；status=active/retired |
| tickets | id、ticket_no、user_id、device_id、subject、description、status、version、created_at、updated_at | ticket_no 唯一；用户＋创建时间、状态＋创建时间索引 |
| ticket_events | id、ticket_id、actor_id、event_type、from_status、to_status、message、created_at | 工单＋创建时间索引；追加写入，所有处理说明对所属客户可见 |
| business_operations | id、user_id、action、request_hash、status、result_resource_id、result_snapshot、expires_at、created_at、completed_at | 主键即后端生成操作键；用户＋动作索引；status=reserved/succeeded |
| support_knowledge_documents | id、model_code、document_id、enabled、created_by、created_at | 型号＋document_id 唯一；document_id 外键指向现有 documents |

金额和资源约束：

- 订单 1～50 项，数量每项 1～1000；单价非负且最多两位小数，拒绝浮点误差和超限值。总金额由 Decimal 计算，最多 999999999999.99。
- 订单号人工录入、规范化后唯一；重复录入返回 409。订单及明细一次事务提交，默认 recorded；管理员可标记 completed。cancelled 只允许没有关联设备的订单，避免已有售后关系失效。第一版不编辑已保存的金额和归属。
- 型号先限 NX100/NX200；设备型号必须匹配订单明细；设备所属用户从订单推导。登记时锁订单明细，已登记设备总数不能超过购买数量。编号不能重复。
- 设备保修按 Asia/Shanghai 业务日期计算，截止日包含当天；日期范围合法。展示根据请求当日计算，不持久化容易过期的布尔值。过保设备仍可提交工单，页面说明过保状态。
- 工单 subject 为 1～120 字，description 为 1～4000 字，处理说明为 1～2000 字，去除首尾空白后校验。设备必须有效且可访问，user_id 从设备推导。
- 工单状态：open → in_progress → resolved → closed；in_progress → open 可退回并填写原因；resolved → in_progress 可重新处理；closed 为终态。只有管理员改状态。采用 version 乐观并发检查，同一旧版本只能更新一次，冲突 409；状态和事件同事务。
- 列表默认 20 条、上限 100，按 created_at 和 id 稳定排序；前端与工具不提供无限量导出。

## 4. 身份、安全与写入幂等

### 认证

用户名采用 3～32 位小写字母、数字、下划线，输入先规范化；显示名允许中文。密码 12～128 字符，使用成熟 Argon2 库；哈希工作移出事件循环。不在日志或接口返回密码、哈希、Cookie 或 CSRF 值。

服务端会话固定 8 小时；32 字节加密随机令牌，数据库保存 SHA-256 摘要；登录刷新令牌，退出撤销当前会话；每次认证检查用户 active。Cookie HttpOnly、SameSite=Lax、Path=/，非本机 HTTP 环境拒绝不安全认证访问，生产 HTTPS 使用 Secure。

CSRF 随会话生成；数据库存摘要，浏览器使用可读取的独立 CSRF Cookie，并在写请求头回传，服务端验证 Cookie、请求头、会话摘要。认证 Cookie 不可读。登录注册仅接收 JSON 并验证同源 Origin；登录后所有写请求还验证 CSRF。缺少可信来源的浏览器写请求拒绝；不从未经配置的 Forwarded 头推断信任。退出清理两类 Cookie。

使用数据库限速桶：登录同账号每 15 分钟 10 次失败、同来源每 15 分钟 60 次尝试；注册同来源每小时 10 次。原子更新保证多进程一致，过期桶由显式清理命令删除。账户不存在也经历等价密码验证与限速路径，失败统一 401，限速 429。来源来自直接连接；部署反向代理时需单独确认可信代理配置，不能信任客户端任意传入的 IP。

### 资源权限

服务层接收后端构建的 Principal，包含 user_id、role 和权限。每次查询在 SQL 中限定 customer 的所属关系，admin 可管理所有业务记录。列表、详情、工具、处理记录和操作查询均执行检查。用户输入的 owner_id、role、permissions 一律不能成为执行身份。不存在及不属于自己的资源都返回 404。

现有管理接口统一加 admin 依赖；新增 `/support/knowledge/search` 仅搜索开放文档。检索实验仍保留本机访问限制，快照归属绑定 user_id 与 session_id，切换账号不能继承旧实验。

### 创建工单防重复

1. 页面调用 `POST /api/v1/operations`，传 action=create_ticket 和最终参数。后端先验证资源、参数，生成 operation_id、规范化参数摘要并存 reserved，默认 30 分钟内可执行。
2. `POST /api/v1/tickets` 或调试接口携带该操作键；它是执行信封字段，不进入模型工具参数。服务端确认所属用户、action、参数摘要和有效期。
3. 创建服务事务锁定操作行。若 succeeded，校验当前访问权后返回原结果；若 reserved，写入工单、初始事件和 succeeded 结果后一起提交。相同键但不同参数返回 409。
4. 超时后结果可能已提交。页面保留原键，通过 `GET /api/v1/operations/{id}` 查询；succeeded 返回原工单，reserved 表示未记录成功或仍在事务中，不等同失败。可以用原键安全重试，由行锁与唯一键串行化。
5. 不因超时换新键自动提交；工具写操作最多执行一次，未知结果返回 reconcile。页面的再次尝试必须复用原键。新建不同操作只能由用户主动发起。
6. 未完成且过期的键拒绝执行；成功键第一版不自动删除，保留去重凭据。业务结果返回 JSON 快照不含私人凭据，权限与资源状态在返回前重新校验。

这张表解决业务写入去重，不保存 Agent 对话、计划或工具检查点，不复用离线任务表。

## 5. HTTP 与工具契约

所有新增路径以 `/api/v1` 开头；错误统一为 detail 中的 code/message/request_id，不暴露异常栈、SQL、内部 URL 或密钥。时间为带时区 ISO 格式，金额为十进制字符串。

| 路径 | 操作与权限 |
| --- | --- |
| `/auth/register`、`/auth/login` | POST，公开、限速、来源校验；注册强制 customer |
| `/auth/me`、`/auth/logout` | GET 当前用户；POST 撤销当前会话 |
| `/admin/customers` | GET，admin 分页查找，最少字段 |
| `/orders`、`/orders/{order_id}` | GET 所属列表/详情；POST 管理员录入；PATCH 详情路径仅管理员修改允许的状态 |
| `/devices`、`/devices/{device_id}` | GET 所属列表/详情；POST 管理员登记 |
| `/tickets`、`/tickets/{ticket_id}` | GET 所属列表/详情；POST 使用操作键创建 |
| `/tickets/{ticket_id}/events` | POST 管理员处理，必须提供 expected_version 和说明；详情包含分页处理记录 |
| `/operations`、`/operations/{operation_id}` | POST 预留工单操作；GET 查询自己的操作结果 |
| `/support/knowledge/search` | POST，登录用户查询开放资料，query/model_code/top_k，禁止自选任意知识库 ID |
| `/admin/support-documents`、`/admin/support-documents/{id}` | GET/POST 列出或关联开放文档；PATCH 启停关联，均为管理员 |
| `/tool-lab/tools`、`/tool-lab/execute` | GET 授权工具目录及 JSON Schema；POST 执行一条工具调用 |

工具目录：

| 名称 | 参数 | 行为 |
| --- | --- | --- |
| list_orders | page、page_size | 当前身份可访问的订单摘要 |
| query_order | order_no | 单笔订单及明细 |
| list_devices | page、page_size | 可访问设备摘要 |
| query_device | serial_no | 型号、固件、保修及关联订单 |
| list_tickets | status 可选、page、page_size | 可访问工单摘要 |
| query_ticket | ticket_no、events_page | 工单及分页处理记录 |
| create_ticket | serial_no、subject、description | 当前身份有权提交的设备工单，需信封中的 operation_id |
| search_knowledge | query、model_code、top_k | 开放资料片段及来源，top_k 1～10 |

工具 schema 均继承 ToolArguments，禁止额外字段和隐式类型转换。schema、目录说明和适配器注册使用同一来源，后续 Agent 可读取相同目录。真实生产工具不会包含故意 sleep、制造失败等演示开关。

订单/设备/工单读取默认每次 5 秒、最多两次尝试；仅明确识别的临时连接故障或超时可重试，权限/参数/业务拒绝不重试。创建工单默认 10 秒、一次尝试。知识检索默认 20 秒、一次尝试，具体 HTTP 客户端连接/读取也设置界限；这些是起始配置，不作为切换持久化任务链路的条件。

调试接口不接收任意 SQL、Python、导入路径、权限、超时或重试次数。后端从可信注册表解析工具；用户无权执行的工具不展示也不允许调用。返回 Observation 加耗时，不回显未经脱敏的服务器异常。关联日志记录 request_id、user_id、工具名、操作键、结果码、耗时和实际尝试次数，不默认记录完整问题内容。

## 6. 资料、初始化数据与运行依赖

### 一套自洽的数据

使用现有澄印 NX100/NX200 型号，不虚构第三套与文档不一致的故障码。新增 `samples/after-sales-business-v1/`：

- `manifest.json`：版本、固定逻辑 ID、基准日期、行数、来源映射，不含密码。
- `users.json`：1 管理员、3 客户（仅账号和显示信息）。
- `orders.json`：12 笔订单，覆盖 recorded/completed；明细合计支持 18 台设备。
- `devices.json`：18 台设备，归属和型号匹配明细，包含保修内外、不同固件与退役设备。
- `tickets.json`：12 张工单及事件，覆盖四种状态和多个用户。
- `knowledge-manifest.json`：只允许选定公开资料、型号映射及源文件哈希。
- `README.md`：明确合成练习数据、业务基准日期与测试场景，不宣称真实客户或真实厂商参数。

初始数据按显式 `--as-of YYYY-MM-DD` 生成相对购买与保修日期，manifest 固化基准日期；重复初始化不重算、不覆盖业务数据。测试使用固定日期；手工验收使用初始化基准日期和当前日期核对保修状态。

账号密码由交互输入，不写到源码、命令行参数、日志或 Git。使用者可选择为各测试账号设置密码；导入报告只列账号、业务数量、冲突和日期。公开生产环境不自动导入这些账户。

初始化以固定 ID/自然唯一键防重复，先做 dry-run，再在单个事务导入业务记录。已有同名账号但 ID 不同、已修改数据或自然键冲突时停止并报告，不覆盖、不清空。工单初始历史从合成数据建立，不伪造真实工具执行日志。

### 知识资料

沿用 `samples/rag-eval-after-sales-v1/corpus/01` 至 `06` 的公开手册、故障说明、服务规则及固件公告；具体文件路径写入 allowlist。`07_archived_calibration.md` 和 `08`～`10` 历史工单默认不开放；`eval/` 绝不导入。原 fixture 不是实际设备数据源。

先确认现有解析器支持的格式；支持 Markdown 就走已有上传流程，否则生成保留文档标题/章节/版本的 PDF 后走既有 PDF 建库，不另写一套索引灌库流程。PDF 生成时使用相应技能检查中文字体与渲染。只有已有 run 完整发布后才标记资料可检索。

知识搜索先限定 enabled 且文档 active、当前 generation 已发布的 allowlist，再执行检索。过滤发生在召回范围内，并在返回前复核，避免先全库召回再只在 UI 隐藏。返回片段、文档标题、document_id、chunk_id、页码/章节及已有版本信息；不返回原始对象存储路径和内部历史工单。

业务登录、订单、设备、工单应只依赖 MySQL。现有启动会初始化所有 RAG 服务，需小范围调整：业务表校验为必需；RAG 初始化失败时业务仍可用，知识功能明确返回 unavailable，管理页展示依赖状态；数据库失败不能伪装健康。保留后台任务关闭清理和错误日志，增加依赖状态接口的管理员访问控制，不返回连接凭据。

## 7. 文件组织与实施阶段

用户希望计划聚焦工作内容与验收，不把完整实现源码提前塞进文档。以下每阶段先写行为测试、观察预期失败，再实现、验证和勾选；实际代码实现留到执行阶段。

### 阶段 1：数据层与增量迁移

新增：`app/business/__init__.py`、`models.py`、`schemas.py`、`errors.py`、`principal.py`；`app/db/business_schema.py`；`migrations/20261026_after_sales_business.sql`；`app/cli/__init__.py`、`business_schema.py`；`tests/test_business_schema.py`。

修改：`app/db/mysql_schema.py`，启动对旧表仍按原流程处理，但明确排除新业务表；新业务表通过只读 schema 检查，不自动 create_all。新模型导入与表清单只有一个来源。

- [x] 写表约束、索引、关系及“启动不创建业务表”测试，先观察失败。
- [x] 编写十表模型、增量 SQL 和只读校验，检查列/类型/索引/外键兼容性。
- [x] CLI 提供 `check` 与显式 `apply`；打印脱敏目标库名称和迁移摘要。DDL 不承诺事务回滚；部分成功能识别并继续兼容的步骤，结构冲突停止。
- [x] 运行 `python -m pytest tests/test_business_schema.py -q -p no:cacheprovider`，预期全通过。
- [x] 准备迁移说明，确认具体目标数据库后才执行迁移；不自动删除任何既有表。（目标 `127.0.0.1:13306/rag_demo`，用户确认后已执行，十张表校验通过；未改既有表。）

### 阶段 2：登录身份与管理接口收口

新增：`app/auth/__init__.py`、`passwords.py`、`sessions.py`、`rate_limits.py`、`dependencies.py`、`schemas.py`；`app/api/routes/auth.py`、`customers.py`；`app/cli/create_admin.py`；`tests/test_auth.py`、`tests/test_management_access.py`。

修改：`pyproject.toml` 密码哈希依赖；`app/main.py` 路由注册；五个既有路由文件 `knowledge_bases.py`、`documents_v2.py`、`model_providers.py`、`retrieval.py`、`retrieval_experiments.py`；`app/retrieval/access.py` 绑定正式身份；对应既有 API 测试显式注入测试身份，不能在生产代码加 test 绕过。

- [x] 写注册越权字段拒绝、并发重名、错误密码、过期/撤销/停用、CSRF、来源和多进程限速测试。
- [x] 实现 Argon2、会话、Principal、原子限速桶、auth 路由和交互式管理员创建。
- [x] 为所有管理路由加权限依赖，测试直接 include_router 时仍受保护。
- [x] 运行 `python -m pytest tests/test_auth.py tests/test_management_access.py -q -p no:cacheprovider`。
- [x] 验收管理员来自专用命令、普通注册不能自选角色、退出后旧 Cookie 失效。（`create_admin`/`set_password` 在隔离测试库实测；公开注册只产生 customer。）

### 阶段 3：订单与设备

新增：`app/business/orders.py`、`devices.py`；`app/api/routes/orders.py`、`devices.py`；`tests/test_orders.py`、`tests/test_devices.py`。

- [x] 写 Decimal 金额、事务回滚、订单重复、列表隔离、详情越权、设备跨用户关联、数量超额和保修边界测试。
- [x] 实现订单录入/查询/有限状态变更和设备登记/查询，关联身份从数据库推导。
- [x] API 与服务分离，服务不依赖 Request 或 HTTPException；路由映射业务错误码。
- [x] 运行 `python -m pytest tests/test_orders.py tests/test_devices.py -q -p no:cacheprovider`。
- [x] 确认一次订单录入失败不会留下半份明细，并发设备登记不能超过购买数量。（SQLite 事务测试 + 真实 MySQL 并发设备登记测试。）

### 阶段 4：工单、事件与幂等

新增：`app/business/tickets.py`、`operations.py`；`app/api/routes/tickets.py`、`operations.py`；`tests/test_tickets.py`、`tests/test_business_operations.py`。

- [x] 写跨用户设备拒绝、同键重复、同键参数冲突、过期键、状态跳转、乐观锁冲突、事件追加和提交后丢响应测试。
- [x] 实现操作预留/查询、事务行锁去重、工单创建与管理员处理。
- [x] 同一幂等事务覆盖工单、初始事件、结果凭据；数据库异常先 rollback 再映射错误，不把提交结果不明当作明确未执行。
- [x] 运行 `python -m pytest tests/test_tickets.py tests/test_business_operations.py -q -p no:cacheprovider`。
- [x] 在 MySQL 并发验收中重复提交同一键，最终仅一个工单和一个创建事件。

### 阶段 5：面向客户的知识检索

新增：`app/business/knowledge.py`；`app/retrieval/published_search.py`；`app/api/routes/support_knowledge.py`；`tests/test_support_knowledge.py`。

修改：`app/api/routes/retrieval.py` 提取既有查询流程为可复用服务；必要时在 `app/storage/elasticsearch_chunks.py` 增加文档 allowlist 条件。`app/bootstrap.py`、`app/main.py` 增加业务与 RAG 依赖状态分离，保留原后台任务清理行为；更新 `tests/test_app_lifespan.py`、`tests/test_health.py`。

- [x] 写未开放文档、禁用映射、删除文档、未发布 generation、型号错误以及外部服务不可用测试。
- [x] 实现管理员资料关联和客户检索入口，复用发布版本与 embedding 配置一致性校验。
- [x] 关闭实验室权限不能意外关闭合法客户知识入口；客户入口不能访问实验原始向量与内部文档。
- [x] 运行 `python -m pytest tests/test_support_knowledge.py tests/test_retrieval_api.py tests/test_app_lifespan.py tests/test_health.py -q -p no:cacheprovider`。
- [ ] 停用 ES/RabbitMQ 时登录、订单、工单仍可使用，知识页显示服务不可用；MySQL 不可用时不得返回业务就绪。（启动分支和 unavailable 逻辑已单测；未实际停容器验收，见验证记录“未验证项”。）

### 阶段 6：真实工具与调试 API

新增：`app/tools/__init__.py`、`catalog.py`、`order_tools.py`、`device_tools.py`、`ticket_tools.py`、`knowledge_tools.py`；`app/api/routes/tool_lab.py`；`tests/test_business_tools.py`、`tests/test_tool_lab_api.py`。

修改：`app/main.py` 注册；仅在必要时扩展 `app/tool_runtime.py` 的目录元数据兼容接口，不改变已有超时/权限语义；更新 `docs/tool-runtime.md` 区分演示命令与真实业务工具。

- [x] 为八个工具编写 schema、权限、实际服务调用、输出脱敏及错误分类测试。
- [x] 从可信会话建立 ExecutionContext，工具 authorize 做快速功能校验，数据库服务做资源检查。
- [x] 注册八个适配器，JSON Schema 从参数模型导出；创建工单信封必须包含合法操作键。
- [x] 编写 tool-lab 目录和执行路由，禁止身份/策略覆盖和任意代码执行。
- [x] 运行 `python -m pytest tests/test_business_tools.py tests/test_tool_lab_api.py tests/test_tool_runtime.py -q -p no:cacheprovider`。
- [x] 验证真实订单查询、越权拒绝、工单创建和重复操作返回同一个工单；模型 call_id 不能代替业务幂等键。

### 阶段 7：业务页面与工具调试页

新增：`app/web/auth.js`、`business.js`、`tool-lab.js`、`business.css`；`tests/test_business_frontend_contract.py`。

修改：`app/web/index.html`、`app.js`、必要的 `retrieval.js`；提取共享 fetch 包装或最小扩展现有 request，所有写请求统一带 CSRF；新模块不依赖现有大脚本的内部业务状态。

- [x] 登录/注册页和身份栏；401 时停止轮询、清空敏感 DOM 与调试结果、回登录。
- [x] 订单列表/详情、设备列表/详情、工单列表/创建/详情；管理员订单明细录入、设备登记、工单处理和资料开放控制。
- [x] 工具目录、参数编辑、执行、耗时/次数/Observation 展示；显示当前真实登录身份，不能在调试页手输另一个执行用户。
- [x] 创建工单显示“会真实创建工单”；先预留操作再提交，双击复用原操作；unknown 展示查询状态/用原键重试，不自动换键。
- [x] 表单支持键盘、关联 label、空态/加载/错误态；窄屏可用，用户内容使用 textContent 或统一转义，不直接拼接 HTML。
- [x] 运行 `python -m pytest tests/test_business_frontend_contract.py tests/test_frontend_contract.py -q -p no:cacheprovider`。
- [x] 使用真实浏览器走客户和管理员流程、切换账号、刷新、窄屏、重复提交及错误展示；静态字符串测试不能替代浏览器验收。

### 阶段 8：关联数据与知识资料初始化

新增：`samples/after-sales-business-v1/` 中第 6 节列出的七个文件；`app/cli/seed_business.py`、`app/cli/import_support_knowledge.py`、`app/cli/cleanup_auth.py`；`tests/test_business_seed.py`。

- [x] 编制 1 管理员、3 客户、12 订单、18 设备、12 工单的数据，校验引用闭合、数量、金额、事件状态和时间顺序。
- [x] 实现 dry-run、显式 apply、密码交互和重复执行冲突保护；数据库目标脱敏展示，不输出连接密码。
- [x] 运行 `python -m app.cli.seed_business --dry-run --as-of 2026-10-26`，预期仅展示校验结果和计划数量，不写库。
- [x] 编写资料 allowlist、可重入上传清单与建库状态检查；上传前按源文件哈希识别已导入项，版本变化报告而不静默覆盖。复用已发布文档，未完成建库不假装可检索。
- [x] 运行 `python -m pytest tests/test_business_seed.py -q -p no:cacheprovider`；两次初始化后数量不增加，后续用户修改不被覆盖。
- [x] 经实际目标库和账号密码确认后导入，记录公开业务编号清单，供用户在页面和调试页复现。

### 阶段 9：端到端验收与交付

新增：`tests/test_business_mysql_integration.py`、`docs/after-sales-business-guide.md`、`docs/after-sales-business-verification.md`；修改 `README.md` 入口和实际能力说明。

- [x] 为隔离 MySQL 建立明确的测试连接输入；只允许显式指定的专用测试库，禁止沿用应用库并执行 drop_all。测试只清理自身固定前缀的记录，执行前核对目标。
- [x] 校验真实 MySQL 唯一键、外键、行锁、事务回滚、幂等并发和状态更新冲突；数据库驱动不支持的语法不能靠 SQLite 通过掩盖。
- [x] 运行 `python -m pytest tests/test_business_mysql_integration.py -q -p no:cacheprovider`；未提供隔离数据库则明确记录未运行，不声称并发验证通过。
- [x] 执行完整 `python -m pytest -q -p no:cacheprovider` 和 `python -m ruff check app tests --no-cache`，记录新增失败与已有失败的边界，不修改无关问题凑通过。
- [x] 浏览器与真实外部依赖完成下一节场景，报告通过/失败/未验证与原因。
- [x] 使用说明列出迁移、创建管理员、初始化数据、资料建库、启动、账号切换、工具调用和故障排查；不要把密码写进文档。
- [x] 文档明确接口和工具已交付，Agent 自动决策、消息循环、持久化恢复尚未实现。

## 8. 最终验收清单

| 场景 | 必须观察到的结果 |
| --- | --- |
| A 登录，查看订单与设备 | 页面、API 和工具返回同一数据库记录 |
| B 查询 A 的订单/设备/工单 | 列表没有该记录，详情与工具均拒绝访问 |
| A 伪造 user_id/role | 请求被拒绝，不改变后端当前身份 |
| A 查询 NX100/NX200 同名故障码 | 检索范围匹配型号，结果保留文档来源 |
| 客户检索内部工单或评测题 | 不在开放检索范围，无泄露 |
| A 创建工单 | 数据库写入工单与初始事件，页面和工具可查 |
| 提交后响应丢失，再用原操作键重试 | 只产生一个工单，能定位原结果 |
| 同一操作键改参数 | 冲突拒绝，原工单不被改写 |
| 两管理员同时处理同一版本 | 一方成功、一方冲突，无丢失更新 |
| 服务重启 | 用户、订单、设备、工单和幂等凭据仍在；不意味着 Agent 任务已可恢复 |
| 重复初始化数据 | 不重复插入、不覆盖用户维护内容 |
| RAG 依赖不可用 | 订单工单业务继续可用，知识检索明确失败 |
| 退出后重放会话 | 401；旧页面结果已清理 |
| 既有建库和检索实验 | 授权管理员可使用，原有流程通过回归 |

## 9. 执行与授权边界

本文件是实施计划，不是完成报告。当前只授权撰写计划；进入实施时沿用已确认业务范围，常规代码细节自主处理。用户 AGENTS.md 要求数据库变更、删除文件、改环境/CI、推送和历史修改必须确认；具体建表 SQL、目标库与数据导入清单准备好之后再就实际执行请求确认，不以笼统范围讨论代替数据库操作许可。

不修改 `.env`、不读取或输出密钥、不默认清空现有库，不把端到端依赖缺失掩盖成成功。用已有模型配置完成知识资料建库需要有效服务凭据；缺失时先完成可独立验证的业务功能，明确报告知识链路所缺条件。

每阶段完成后更新本计划复选框并报告实际结果。阶段 1～4 建立可运行业务，阶段 5～6 接通知识和工具，阶段 7～9 完成可操作页面、数据和整体验收。计划完成后由用户启动实施；无需再次逐个讨论字段或一般实现方案。

## 10. 执行记录（2026-10-26）

- 已在当前工作区按阶段完成实施；未创建 worktree、未派子代理、未提交或推送 Git。
- 用户确认目标库后执行迁移：`127.0.0.1:13306/rag_demo` 新增九张业务表，既有表未改。
- 已导入 1 管理员、3 客户、12 订单、12 明细、18 设备、12 工单、35 条处理记录；重复执行不增加数量。
- 已建隔离测试库 `rag_demo_business_test` 并完成 6 项真实 MySQL 并发/约束测试。
- 2026-10-26 变更：`documents.support_open` 单字段取代开放关联表，旧表已按用户确认删除；旧库中被错误关联的 NX100 手册已自动关闭。
- 已发布 6 份售后资料并全部开放，`search_knowledge` 工具命中真实片段。
- 完整测试：310 passed（不提供隔离库时 304 passed / 6 skipped）；工具 ruff 通过；HTTP 验收 32/32；Edge 浏览器验收 29/29。
- 详细证据、截图与未验证项：[售后业务验证](../../after-sales-business-verification.md)。
- 阶段 5 最后一项（物理停用 ES/RabbitMQ）未实测，保持未勾选；其余已完成项均有对应测试或真实操作记录，未用静态检查替代。
