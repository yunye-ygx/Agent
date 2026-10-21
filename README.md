# RAG Demo

这是一个用于学习知识库解析、版本切换、异步 Task 和 Chunk 检索的 Demo。当前链路已经从 PostgreSQL/pgvector 切换为：

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

已有 MySQL/共享 Chunk 索引必须先审阅 [迁移说明](docs/a1-migration.md)，不能把 `create_all` 当作字段迁移。启动会检查旧结构并报错，不自动 ALTER 或转换现有索引。迁移脚本本次**仅编写，未执行**；原始 ES 索引保留，配置切换需要单独操作。

## API 概览

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

打开页面顶部的“模型配置”，可以查看智谱和 GPT-compatible 提供商的配置状态、Chat/Embedding 模型、向量维度和连接测试结果。完整 API Key 只从服务端 `.env` 读取，前端只接收掩码；GPT 没有配置 Key 时会显示未配置，不会发起外部请求。

当前解析链路使用智谱 `embedding-3`，Embedding 维度为 2048，与 Elasticsearch `rag_demo_chunks_v1` 的 Mapping 一致。GPT 的 `text-embedding-3-small` 默认返回 1536 维，当前仅作为配置展示和后续接入入口，不能直接写入现有 2048 维 Chunk 索引。

## 测试

离线测试不依赖 Docker、MinerU 或外部模型服务：

```powershell
python -m pytest -q
```

测试覆盖配置、数据模型、稳定 Chunk ID、ES Mapping、RabbitMQ 消息契约、Task 幂等判断、MinerU 标准化结果、Token 切块和前端契约。

## 设计文档

本次对照结论、实现映射和验收证据见：

[docs/a1-alignment.md](docs/a1-alignment.md)
