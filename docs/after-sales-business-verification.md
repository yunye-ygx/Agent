# 打印机售后业务底座：验证记录（2026-10-26）

本文件只记录实际执行过的验证。未执行的项目在第 6 节明确列出，不能用离线测试或静态检查代替真实验收。

## 1. 环境

- Windows，Python 3.12.5，项目目录 `C:\javapractice\new`
- Docker Desktop；MySQL 8.4（`127.0.0.1:13306`）、Elasticsearch 8.11、MinIO、RabbitMQ
- 应用库：`rag_demo`（本机）；隔离测试库：`rag_demo_business_test`
- 当前 ES 索引：`.env` 指向的 `rag_demo_chunks_qwen_v1`（2048 维，已有 A1 字段）
- 应用：`uvicorn app.main:app --host 127.0.0.1 --port 8001`
- 知识资料：新建知识库「售后开放资料」，6 份 Markdown 均发布 v1，并在 `documents.support_open` 上开放（无型号关联表）

## 2. 数据库与数据初始化

### 迁移

```powershell
python -X utf8 -m app.cli.business_schema check
python -X utf8 -m app.cli.business_schema apply --yes
python -X utf8 -m app.cli.business_schema apply --yes --file migrations/20261026b_documents_support_open.sql
```

结果：

- 迁移前检查明确报告业务表缺失，且没有自动建表。
- 依次应用 9 条 `CREATE TABLE IF NOT EXISTS`，结构校验通过。
- 只新增业务表；既有 7 张离线建库表和数据保留，原迁移未执行 `DROP`/`ALTER`。
- 应用库现有既有知识库 `e2e-retrieval-20261022` 的数据未被修改。
- 2026-10-26 调整：`migrations/20261026b_documents_support_open.sql` 在隔离库先演练（两次执行均成功，验证守卫幂等），再对 `rag_demo` 执行：新增 `documents.support_open`、从 7 条旧关联回填、`DROP TABLE support_knowledge_documents`。
- 回填后执行 `import_support_knowledge --apply --yes` 按清单重建标记：目标知识库 6 份文档开放，旧库中被错误关联的 `01_nx100_manual.md` 自动关闭（该行由早期导入留下，属于本次修复的脏数据）。

### 合成数据

```powershell
python -X utf8 -m app.cli.seed_business --dry-run --as-of 2026-10-26
python -X utf8 -m app.cli.seed_business --apply --yes --create-users --password-stdin
python -X utf8 -m app.cli.seed_business --apply --yes
```

结果：

- 第一次导入：用户 4、订单 12、明细 12、设备 18、工单 12、处理记录 35。
- 第二次导入：用户新增 0/跳过 4；订单新增 0/跳过 12；设备新增 0/跳过 18；工单新增 0/跳过 12。
- 用户修改后的显示名在重复初始化后保持不变；未覆盖已存在记录。
- 管理员 `admin`、客户 `alice`/`bob`/`carol`。密码由初始化时本地生成，未写入仓库、文档或日志。

可公开复现的业务编号：

- 订单：`SO-2026-1001`～`SO-2026-1012`；客户归属 alice 1001–1004、bob 1005–1008、carol 1009–1012。
- 设备：`NX100-2024-001`、`NX200-2024-002` … 共 18 台，完整清单位于 `docs/samples/after-sales-business-v1/devices.json`；其中 `NX200-2025-008` 为已退役设备。
- 工单：`T2026-0001`～`T2026-0012`；状态覆盖 open/in_progress/resolved/closed。
- 资料：知识库「售后开放资料」中的 `01`～`06`，6 份文档的 `support_open` 均为 1；旧库文档保持未开放。

### 其他 CLI

- `create_admin` 在隔离测试库实际创建管理员；`set_password` 实际重置并撤销会话；用哈希校验确认可登录。
- `cleanup_auth` dry-run 与 `--apply --yes` 均执行，当前过期会话/限速桶为 0。
- 已验证创建管理员不接受公开注册；公开注册强制 customer。

## 3. 真实 MySQL 集成测试

```powershell
$env:BUSINESS_TEST_MYSQL_HOST = "127.0.0.1"
$env:BUSINESS_TEST_MYSQL_PORT = "13306"
$env:BUSINESS_TEST_MYSQL_USER = "rag_user"
$env:BUSINESS_TEST_MYSQL_PASSWORD = "***"
$env:BUSINESS_TEST_MYSQL_DATABASE = "rag_demo_business_test"
python -m pytest tests/test_business_mysql_integration.py -q -p no:cacheprovider
```

结果：**6 passed**。

覆盖：

1. 设备编号唯一约束、设备→用户外键（失败后回滚）。
2. 订单→用户外键失败后无半份记录。
3. 同一操作键两个并发事务创建工单：最终 1 张工单、1 条创建事件、操作状态 succeeded。
4. 同一操作键改参数：冲突 `OPERATION_PARAMETER_MISMATCH`，不创建工单。
5. 同一订单明细两个并发设备登记：1 成功、1 `DEVICE_QUANTITY_EXCEEDED`，总数不超过购买数量。
6. 同一工单版本两个并发处理：1 成功、1 `VERSION_CONFLICT`，版本只增加一次。

测试只创建/清理 `itest-`/`ITEST-` 前缀记录，不执行 `drop_all`，不访问 `rag_demo`。

## 4. HTTP 真实验收

```powershell
python -X utf8 tools/verify_after_sales_api.py
```

结果：**32 passed, 0 failed**。

覆盖：

- admin/alice/bob 真实登录；管理员身份来自后端会话。
- alice 只看到自己的 4 笔订单、4 台设备、自己的工单。
- alice 访问 bob 的订单返回 404；查询参数 `user_id=bob` 不改变后端身份。
- `search_knowledge` 工具返回片段与文档来源，不泄露对象路径；携带 `model_code` 被拒为 `INVALID_ARGUMENTS`。
- 管理员可关闭单份文档的售后开放，关闭后该文档不再被召回；客户调用同一接口返回 403；随后恢复开放。
- 预留操作键 → 创建工单 → 同键重复提交返回同一工单（201）；同键改参数 409。
- `GET /operations/{id}` 返回 succeeded 和原工单 ID；管理员追加处理记录后版本递增；旧版本 409。
- 工具目录 8 个；`list_orders` 真实查询 4 笔；`create_ticket` 复用操作键返回原工单。
- 退出后旧会话 Cookie 重放 `/auth/me` 返回 401。
- bob 只看到自己的 4 笔订单。

## 5. 真实浏览器验收

```powershell
python -X utf8 tools/verify_after_sales_browser.py
```

结果：**29 passed, 0 failed**。使用本机 Microsoft Edge 无头模式 + Chrome DevTools Protocol，实际执行页面 JS、点击、填表和等待接口返回。

覆盖：

- 未登录只显示登录页；登录与注册是**两个独立页面**，可互相切换，表单 Tab 键顺序可用。
- `/#/register` 与 `/#/login` 可独立访问，浏览器前进后退可切换。
- alice 登录后身份栏来自后端；订单 4 行、设备 4 行；浏览器内 fetch 他人订单 404。
- 订单、设备、工单的“详情”都进入**独立详情页面**，列表页不再内联展开，且可返回列表。
- 工具调试页中 `search_knowledge` 的参数表单只有 `query` 与 `top_k`，没有型号字段。
- 工单页真实创建工单、显示初始处理记录、成功后清除 sessionStorage 操作键。
- 工具调试页加载 8 个工具，实际执行 `list_orders`；显示后端身份、耗时、尝试次数。
- 390×844 窄屏无横向溢出。
- 退出切 bob：身份栏为 bob，访问 alice 订单 404。
- 管理员：订单 12 行、客户 3 行；知识库文档列表显示 6 份已开放资料，可关闭一份再恢复。
- 打开待处理工单、提交处理说明和状态，版本递增。
- 刷新后管理员会话保持；清除 Cookie 后回到登录页，业务 DOM 清空。
- 浏览器网络日志中的 401/404 是上述“未登录检查/越权检查”的预期结果。

截图（`docs/images/after-sales/`）：

| 文件 | 内容 |
| --- | --- |
| `01-login.png` | 登录页（独立页面，深色品牌区 + 登录卡片） |
| `01b-register.png` | 注册页（独立页面，含确认密码与返回登录） |
| `02-alice-workbench.png` | 客户工作台 |
| `03-alice-orders.png` | 客户订单列表 |
| `03b-order-detail.png` | 订单独立详情页 |
| `04-alice-devices.png` | 客户设备列表 |
| `04b-device-detail.png` | 设备独立详情页 |
| `05-alice-support.png` | 工具调试中的 `search_knowledge`（只有 query/top_k） |
| `06-alice-ticket.png` | 工单独立详情页（真实创建工单） |
| `07-alice-tool-lab.png` | 工具执行 |
| `08-alice-narrow.png` | 窄屏 |
| `09-bob-orders.png` | 账号切换后的客户订单 |
| `10-admin-orders.png` | 管理员订单 |
| `11-admin-support.png` | 知识库文档列表的“售后开放”开关 |
| `12-admin-ticket-processing.png` | 管理员处理工单 |
| `13-session-cleared.png` | 清除会话后的登录页 |

验收脚本额外写入了若干“验收工单/浏览器验收”合成工单，用于验证真实写入和幂等；这些记录未删除，因此页面工单计数会高于 seed 初始 12。它们不是 seed 数据，也不影响原始 12 条样例。

## 6. 完整测试与静态检查

```powershell
$env:BUSINESS_TEST_MYSQL_* = ...   # 指向隔离库
python -X utf8 -m pytest -p no:cacheprovider
python -m ruff check app tests tools --no-cache
```

结果：

- `310 passed`（含 6 项真实 MySQL 集成测试；不提供隔离库时这 6 项 skip，此时为 304 passed / 6 skipped）。
- `ruff check app tests tools`：通过。
- 既有离线建库、检索实验、工具 runtime、前端契约测试继续通过。

## 7. 未验证项与剩余问题

未执行/不能声称已验证：

1. **物理停用 ES/RabbitMQ 的场景未实测**。已通过 `test_app_lifespan` 的 RAG 不可用分支和 `test_support_knowledge` 的 unavailable 路径验证启动/错误逻辑；但没有在真实运行中停掉容器再验收订单/工单/知识页，因此计划阶段 5 的该项复选框保持未勾选。
2. **生产 HTTPS/反向代理/可信代理配置未验证**。代码按请求 scheme 设置 Secure Cookie，非本机非 HTTPS 拒绝认证访问；实际 TLS、代理头、域名和部署参数需要单独确认。
3. **真实移动设备、读屏器和完整键盘路径未测试**。只做了 390px 视口和登录表单 Tab 检查。
4. **知识检索质量未评估**。已确认真实召回和来源，但没有标注集、Recall/答案质量或 Rerank；当前未配置 Rerank。
5. **真实 LLM Agent 对话循环未实现**：无模型自动选工具、多轮规划、任务检查点、重启恢复和 `unknown` 自动核对。按范围留给后续。
6. **多进程/多实例并发未压测**。限速与幂等使用数据库共享状态，MySQL 集成测试验证了单进程内两个独立事务的锁行为，但没有启动多个 uvicorn worker 做压力/故障注入。
7. **远端 MinerU 适配器未联通**；本机 Markdown 资料走本地文本解析。
8. **管理员处理页只展示合法状态选项**，非法跳转仍由后端拒绝；未做所有组合的浏览器矩阵。
9. 应用启动时 MySQL 警告 `Integer display width is deprecated`（来自 `TINYINT(1)`），不影响当前运行；后续可改为 `BOOLEAN` 并配合新的迁移审核。
10. 知识导入首次 `--apply` 时构建是异步的：若尚未发布，需要再次执行 `--apply`/`--status` 才会把文档置为开放；当前 6 份已发布并开放。
11. **型号澄清策略未实现到 Agent 层**：按 2026-10-26 决定，文档不再记录型号，`search_knowledge` 也不接受 `model_code`；先用 `query_device` 限定型号、信息不足时追问用户，属于后续 Agent 对话循环的工作，本轮只验证“未开放文档不会被召回”。

## 8. 结论

本轮交付的业务底座、真实工具和页面已完成实际数据库、HTTP 与浏览器验收；未验证项均是外部部署条件或明确留给后续 Agent 编排的能力，不以 mock 结果冒充真实验收。
