# 澄印打印机售后业务底座（含 RAG Demo）

这是一个可运行的打印机售后业务底座：FastAPI + MySQL 提供用户、订单、设备、工单、开放资料和真实工具，原生 HTML/JavaScript 提供客户与管理员页面；底层保留原有的 RAG 知识库、离线建库、检索实验和模型配置能力。

全部账号、订单、设备、工单和品牌参数均为**合成练习数据**。本轮没有实现完整 Agent 对话循环、任务持久化或恢复，聊天页仍是界面壳子。

- 启动、账号、迁移、资料导入和操作说明：[docs/after-sales-business-guide.md](docs/after-sales-business-guide.md)
- 实际测试结果与未验证项：[docs/after-sales-business-verification.md](docs/after-sales-business-verification.md)
- 业务设计与计划：[docs/superpowers/specs/2026-10-26-users-orders-design.md](docs/superpowers/specs/2026-10-26-users-orders-design.md)、[docs/superpowers/plans/2026-10-26-after-sales-business-foundation.md](docs/superpowers/plans/2026-10-26-after-sales-business-foundation.md)

## 售后业务快速启动

```powershell
python -m pip install -e ".[dev]"
docker compose up -d

# 首次或迁移后：检查/应用十张业务表（不会自动建表）
python -X utf8 -m app.cli.business_schema check
python -X utf8 -m app.cli.business_schema apply --yes

# 创建管理员并导入合成数据（密码交互输入，不写入仓库）
python -X utf8 -m app.cli.create_admin admin
python -X utf8 -m app.cli.seed_business --dry-run --as-of 2026-10-26
python -X utf8 -m app.cli.seed_business --apply --yes --create-users

# 开放售后资料；建库是异步的，发布后再执行一次以建立映射
python -X utf8 -m app.cli.import_support_knowledge --apply --yes
python -X utf8 -m app.cli.import_support_knowledge --status

# 启动
python -X utf8 -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

打开 <http://127.0.0.1:8001/>。客户只能读取自己的订单、设备和工单；管理员可录入订单、登记设备、处理工单、维护资料开放清单。8 个工具可在“工具调试”页实际执行，创建工单会先预留操作键并写入数据库。

验证命令：

```powershell
python -m pytest -q -p no:cacheprovider
python -m ruff check app tests tools --no-cache
python -X utf8 tools/verify_after_sales_api.py        # 需运行中的 API 与账号密码环境变量
python -X utf8 tools/verify_after_sales_browser.py    # 需本机 Edge，输出截图
```

下面的内容是原有 RAG Demo 的详细说明，仍然有效。

## RAG Demo

```text
MySQL：知识库、Document、Generation/ParseRun、Task 与 Outbox
MinIO：原始文件、MinerU 解析产物和图片
RabbitMQ：Task 异步投递、重试和死信
Elasticsearch：只保存 Chunk，同时承担文本和向量检索
```

## 运行依赖

- Python 3.11+
- Docker Desktop
- 本机可用的 MinerU 命令，或已核验接口契约的远端 MinerU 服务
- 可调用的 Embedding 服务

安装依赖：

```powershell
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

启动基础设施：

```powershell
docker compose up -d
```

本地服务端口：

| 服务 | 端口 |
|---|---:|
| MySQL | 13306 |
| Elasticsearch | 9200 |
| MinIO API | 9000 |
| MinIO Console | 9001 |
| RabbitMQ AMQP | 5672 |
| RabbitMQ Management | 15672 |

启动 API：

```powershell
python -m uvicorn app.main:app --reload --port 8001
```

本地开发模式下，FastAPI 启动时会自动在同一个进程内启动 Outbox Publisher、RabbitMQ Worker 和 A1 Maintenance，因此只需要启动这一个命令。配置由 `RUN_BACKGROUND_SERVICES=true` 控制。

如果需要单独调试 Worker，仍然可以使用独立入口：

```powershell
python -m app.worker
```

如果需要单独调试 Outbox Publisher，仍然可以使用独立入口：

```powershell
python -m app.messaging.outbox_publisher
```

单进程本地开发链路：

```text
uvicorn
  ├── FastAPI API
  ├── Outbox Publisher 后台任务
  ├── RabbitMQ Worker 后台任务
  └── A1 Maintenance 后台任务
```

不要在单进程开发模式下再额外启动上面两个独立入口，否则会同时运行重复的 Publisher/Worker。生产环境再将它们拆成独立服务。

打开 `http://127.0.0.1:8001/`，可以看到知识库管理界面；聊天页面目前只有 UI 壳子，不会伪造检索回答。

## A1 解析链路

```text
上传 PDF/TXT/MD → MinIO 原文件 + Document（此时不解析）
点击解析 → MySQL 固定文件版本/配置快照，创建 Generation（ParseRun）和 PARSE/COPY Task + Outbox
提交事务 → Publisher 按 dispatch_seq 投递 RabbitMQ
Worker 原子领取 attempt_id/租约/次数 → PARSE 或 COPY → 分批 embedding / ES 写入
有效执行资格下提交 Task SUCCESS + 批次统计
Maintenance 核对完整产物与搜索可见性 → 原子发布 active_run_id
```

`current_run_id` 保留最近一次解析批次，`active_run_id` 指向完整发布且供检索使用的批次。新批次构建、失败或取消期间继续检索旧有效批次。为兼容旧表，物理列 `building_generation_id` 对应 current_run_id，`current_generation_id` 对应 active_run_id。接口保留旧展示字段，并增加明确的两个 run 字段。

PDF Task 使用 0 基半开页范围，Worker 先提取子 PDF，解析后将页码映射回原文件。TXT/MD 用 `[0,0)` 表示整文档，一个 Task 可以生成多个 chunk。异步切块仍使用 `StructuredChunker`。

完整成功任务可作为新 COPY Task 的源；新 Task 先等待调度，复制全部文本、向量和来源信息，改写独立的 run_id/task_id/_id 后才提交成功。ES 不再共享 generation_ids，TaskChunkRef 仅保留用于旧数据迁移。COPY 与 PARSE 使用相同的有限尝试、退避及租约恢复机制；`task_max_retry=3` 表示最多 4 次执行机会。

Worker 在阶段边界核对资格；取消撤销业务提交资格，不承诺停止已经发往远端的 MinerU 计算。相同任务的产物文本/集合摘要在首次写 ES 前固定，重试发现变化会拒绝混写。此保护不等于已验证真实 OCR/模型输出稳定。

本地进程除 Publisher、Worker 外还自动启动 **Maintenance**，负责过期执行恢复、取消收尾、批次发布与安全清理。独立部署时必须额外启动：

```powershell
python -m app.rag.maintenance
```

清理保护有效批次和正在执行的 COPY 源，并在切换后保留旧结果一段时间。清理保留控制记录，允许再次清除迟到的非生效 ES 记录。

远端解析适配器支持明确标记的 `file_parse-v2` multipart 契约，通过代码配置项 `mineru_base_url` 和 `mineru_api_version` 选择。未配置 URL 时仍使用现有本地 MinerU 适配器。本次没有改 `.env` 或调用真实远端服务，实际部署接口版本必须先核验。

## 旧数据升级

已有 MySQL/共享 Chunk 索引必须先审阅 [迁移说明](docs/a1-migration.md)，不能把 `create_all` 当作字段迁移。启动会检查旧结构并报错，不自动 ALTER 或转换现有索引。本机已于 2026-10-22 完成 A1 表与 Chunk 转换；新环境仍需按文档单独审核和执行。

## API 概览

售后业务（全部以 `/api/v1` 开头；未登录 401，越权/不存在 404，管理员接口 403）：

```text
POST /auth/register  POST /auth/login  POST /auth/logout  GET /auth/me
GET  /orders  GET /orders/{id}  POST /orders  PATCH /orders/{id}
GET  /devices GET /devices/{id} POST /devices
GET  /tickets GET /tickets/{id} POST /tickets  POST /tickets/{id}/events
POST /operations      GET /operations/{id}
POST /support/knowledge/search
GET/POST /admin/support-documents   PATCH /admin/support-documents/{id}
GET  /admin/customers
GET  /tool-lab/tools  POST /tool-lab/execute
GET  /admin/system/status
```

知识库与检索实验（管理员 + CSRF；检索实验仍限本机直连）：

```text
POST /api/v1/knowledge-bases
GET  /api/v1/knowledge-bases
POST /api/v1/knowledge-bases/{kb_id}/documents
GET  /api/v1/knowledge-bases/{kb_id}/documents
POST /api/v1/documents/{document_id}/parse
POST /api/v1/documents/{document_id}/reparse
POST /api/v1/documents/{document_id}/cancel
GET  /api/v1/documents/{document_id}/status
GET  /api/v1/documents/{document_id}/chunks
GET  /api/v1/search?q=问题&knowledge_base_id=知识库ID
GET  /api/v1/model-providers
POST /api/v1/model-providers/{provider}/test
```

## 模型配置

打开页面顶部的“模型配置”，可以查看已保存的提供商标记、Chat/Embedding 模型、向量维度和连接测试结果。完整 API Key 只保存在服务端，前端只接收掩码。

当前本机 `.env` 使用百炼兼容地址、`qwen-plus` 与 `text-embedding-v4`（2048 维），对应索引 `rag_demo_chunks_qwen_v1`；以实际 `.env` 为准。切换向量模型不能只改配置，需要重建知识库或按迁移说明整体迁移。

## 测试

离线测试不依赖 Docker、MinerU 或外部模型服务：

```powershell
python -m pytest -q -p no:cacheprovider
```

当前共 310 项通过（含 6 项仅在提供隔离 MySQL 测试库时运行的真实集成测试；不提供隔离库时为 304 passed / 6 skipped）。另有 HTTP 与 Edge 浏览器验收脚本，说明见 [售后业务验证](docs/after-sales-business-verification.md)。

## 设计文档

- 售后业务底座计划与设计：[plan](docs/superpowers/plans/2026-10-26-after-sales-business-foundation.md)、[spec](docs/superpowers/specs/2026-10-26-users-orders-design.md)
- 工具执行器与真实工具：[docs/tool-runtime.md](docs/tool-runtime.md)
- A1 对照结论与实现映射：[docs/a1-alignment.md](docs/a1-alignment.md)
