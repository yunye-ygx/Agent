> 历史设计记录：本文中的共享 generation_ids / TaskChunkRef 复用、旧状态和发布流程已被 2026-10-21 A1 改造替代。当前实现及验收依据见 [A1 对齐记录](a1-alignment.md)，升级步骤见 [迁移说明](a1-migration.md)。下文仅用于了解旧实现，不能作为新链路说明。

# RAG Demo 离线建库与基础设施设计

> 版本：v1
>
> 目标：为新的知识库解析链路提供一份可以交给其他窗口执行的离线初始化设计。
>
> 本方案不再使用 PostgreSQL、pgvector 或旧版同步入库流程。

## 1. 最终技术边界

```text
MySQL：
    保存知识库、Document、Generation、Task 以及 Task 与 Chunk 的关系

MinIO：
    保存用户上传的原始文件、MinerU 解析产物和图片对象

RabbitMQ：
    投递异步 Task 消息、承载消息重试和死信

Elasticsearch：
    只保存 Chunk
    同时承担 Chunk 正文、Embedding、关键词检索和向量检索
```

核心原则：

1. MySQL 是业务状态的唯一事实来源。
2. RabbitMQ 的消息只代表“请执行某个 Task”，不代表 Task 最终成功。
3. MinIO 保存文件，不保存业务状态。
4. Elasticsearch 不承担 Generation 或 Task 状态机。
5. ES 中一条物理文档就是一个 Chunk，同时包含文本和向量。
6. 每次主动重新解析都创建新的 Generation 和新的 Task。
7. 同一 Document 同时最多存在一个正在构建的 Generation。
8. 旧的 `current_generation` 在新 Generation 构建期间继续提供检索。

## 2. 业务对象关系

```text
KnowledgeBase 1 ───── N Document
Document      1 ───── N Generation
Generation    1 ───── N Task
Task          1 ───── N TaskChunkRef
TaskChunkRef  N ───── 1 ES Chunk
```

完整链路：

```text
用户上传文件
    ↓
MinIO 原始文件
    ↓
MySQL Document
    ↓
MySQL Generation
    ↓
MySQL Task
    ↓
RabbitMQ Task 消息
    ↓
Worker 读取 MinIO 并完成解析、结构化切块、Embedding
    ↓
ES 写入 Chunk
    ↓
MySQL 写入 TaskChunkRef，并更新 Task 状态
    ↓
Generation 汇总所有 Task
    ↓
Generation SUCCESS 后切换 Document.current_generation_id
```

### 2.1 当前结构化切块策略

异步 Worker 当前使用 `StructuredChunker`，策略版本为 `structured-v1`。Block 进入切块器后先进行结构分析：保留 `title` 的上下文路径，识别 MinerU 未标注的有序/无序列表并维护层级，表格按完整行切分且重复表头，图片保留 `relative` 与 `nearby` Block 关系。普通正文按照段落、句子、标点、空格、Token 的顺序递归降级；`title/table/list/image` 是结构切换点，不会默认跨结构产生 Token overlap。纯图片且没有 OCR、caption 或 relative 文本时只保留解析元数据，不生成空文本 Chunk。

结构化策略版本、Chunk size 和 overlap 会写入 Generation 的 `config_snapshot` 并参与 `parse_fingerprint`，切块规则变化时不会复用旧策略生成的 Chunk。

## 3. MySQL 初始化约定

### 3.1 数据库

```sql
CREATE DATABASE rag_demo
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
```

建议使用 MySQL 8.0 或更高版本。

建议配置：

```text
数据库名：rag_demo
字符集：utf8mb4
排序规则：utf8mb4_unicode_ci
时区：UTC
主键：CHAR(36) UUID
时间字段：DATETIME(6)
```

所有服务在 API、Worker、定时清理程序中统一使用 UTC，前端展示时再转换为本地时间。

### 3.2 ID 约定

MySQL 主键统一使用 UUID 字符串，例如：

```text
550e8400-e29b-41d4-a716-446655440000
```

这样可以让 API、RabbitMQ 消息、ES `_id` 和日志中的对象标识保持一致，也不会依赖不同服务之间的自增 ID 协调。

## 4. MySQL 表设计

## 4.1 `knowledge_bases`：知识库表

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `CHAR(36)` | PK | 知识库 ID |
| `name` | `VARCHAR(200)` | NOT NULL | 知识库名称 |
| `description` | `VARCHAR(1000)` | NULL | 知识库描述 |
| `status` | `VARCHAR(32)` | NOT NULL | `ACTIVE` / `DELETED` |
| `document_count` | `INT UNSIGNED` | NOT NULL DEFAULT 0 | 文档数量缓存 |
| `created_at` | `DATETIME(6)` | NOT NULL | 创建时间 |
| `updated_at` | `DATETIME(6)` | NOT NULL | 最后更新时间 |

索引：

```text
PRIMARY KEY (id)
INDEX idx_kb_status_updated (status, updated_at)
```

`document_count` 是展示缓存，不是关系事实。真实文档数量仍可以通过 `documents` 查询校验。

## 4.2 `documents`：逻辑文档表

一个 Document 表示用户在知识库中看到的一个逻辑文件，不等同于某次解析任务。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `CHAR(36)` | PK | Document ID |
| `knowledge_base_id` | `CHAR(36)` | NOT NULL | 所属知识库 |
| `filename` | `VARCHAR(512)` | NOT NULL | 用户看到的文件名 |
| `object_key` | `VARCHAR(1024)` | NOT NULL | MinIO 原始文件 Key |
| `file_hash` | `CHAR(64)` | NOT NULL | 原始文件 SHA-256 |
| `file_size` | `BIGINT UNSIGNED` | NOT NULL | 文件字节数 |
| `mime_type` | `VARCHAR(128)` | NOT NULL | MIME 类型 |
| `page_count` | `INT UNSIGNED` | NULL | 解析后页数 |
| `status` | `VARCHAR(32)` | NOT NULL | `ACTIVE` / `DELETED` |
| `current_generation_id` | `CHAR(36)` | NULL | 当前对外检索版本 |
| `building_generation_id` | `CHAR(36)` | NULL | 正在构建的版本 |
| `created_at` | `DATETIME(6)` | NOT NULL | 上传时间 |
| `updated_at` | `DATETIME(6)` | NOT NULL | 最后更新时间 |

索引和约束：

```text
PRIMARY KEY (id)
FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id)
UNIQUE KEY uk_document_kb_hash (knowledge_base_id, file_hash)
INDEX idx_document_kb_status (knowledge_base_id, status)
INDEX idx_document_building (building_generation_id)
INDEX idx_document_current (current_generation_id)
```

注意：`current_generation_id` 和 `building_generation_id` 是文档版本指针，不在 Document 删除时自动级联删除 Generation；Generation 的清理由业务流程负责。

同一 Document 的并发控制：

```text
SELECT ... FROM documents WHERE id = ? FOR UPDATE
```

在事务中检查 `building_generation_id IS NULL`，然后创建新的 Generation 并回写该字段，防止同一文档同时创建两个构建版本。

## 4.3 `generations`：解析版本表

一个 Generation 表示某个 Document 在某份配置快照下的一次完整解析结果。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `CHAR(36)` | PK | Generation ID |
| `document_id` | `CHAR(36)` | NOT NULL | 所属 Document |
| `version_no` | `INT UNSIGNED` | NOT NULL | 文档内部递增版本号 |
| `status` | `VARCHAR(32)` | NOT NULL | 见状态机 |
| `parse_fingerprint` | `CHAR(64)` | NOT NULL | 文件与全量配置指纹 |
| `config_snapshot` | `JSON` | NOT NULL | 创建时使用的完整配置快照 |
| `expected_task_count` | `INT UNSIGNED` | NOT NULL DEFAULT 0 | 预期 Task 数 |
| `completed_task_count` | `INT UNSIGNED` | NOT NULL DEFAULT 0 | 已成功 Task 数 |
| `failed_task_count` | `INT UNSIGNED` | NOT NULL DEFAULT 0 | 失败 Task 数 |
| `canceled_task_count` | `INT UNSIGNED` | NOT NULL DEFAULT 0 | 取消 Task 数 |
| `reuse_expires_at` | `DATETIME(6)` | NULL | 失败/取消版本的临时保留截止时间 |
| `failure_reason` | `VARCHAR(2000)` | NULL | Generation 失败原因 |
| `created_at` | `DATETIME(6)` | NOT NULL | 创建时间 |
| `started_at` | `DATETIME(6)` | NULL | 开始执行时间 |
| `finished_at` | `DATETIME(6)` | NULL | 结束时间 |
| `updated_at` | `DATETIME(6)` | NOT NULL | 最后更新时间 |

约束和索引：

```text
PRIMARY KEY (id)
FOREIGN KEY (document_id) REFERENCES documents(id)
UNIQUE KEY uk_generation_document_version (document_id, version_no)
INDEX idx_generation_document_status (document_id, status)
INDEX idx_generation_fingerprint (document_id, parse_fingerprint)
INDEX idx_generation_cleanup (status, reuse_expires_at)
```

Generation 状态：

```text
BUILDING
CANCELING
SUCCESS
FAILED
CANCELED
```

规则：

1. 只有所有 Task 都是最终成功状态时，Generation 才能变成 `SUCCESS`。
2. 只有 `SUCCESS` 的 Generation 可以被设置为 `Document.current_generation_id`。
3. `FAILED`、`CANCELED` 的 Generation 不能参与检索。
4. `CANCELING` 期间不允许新建同一 Document 的下一次 Generation。
5. `reuse_expires_at` 只用于失败或取消版本的临时资源保留，成功版本为空。

## 4.4 `tasks`：解析执行任务表

一个 Task 是某个 Generation 内按页范围拆分的执行单元。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `CHAR(36)` | PK | Task ID |
| `generation_id` | `CHAR(36)` | NOT NULL | 所属 Generation |
| `document_id` | `CHAR(36)` | NOT NULL | 冗余 Document ID，便于查询 |
| `task_no` | `INT UNSIGNED` | NOT NULL | Generation 内顺序 |
| `core_page_start` | `INT UNSIGNED` | NOT NULL | 核心页起始页，1-based |
| `core_page_end` | `INT UNSIGNED` | NOT NULL | 核心页结束页，1-based |
| `context_page_start` | `INT UNSIGNED` | NULL | 可读取的上下文起始页 |
| `context_page_end` | `INT UNSIGNED` | NULL | 可读取的上下文结束页 |
| `status` | `VARCHAR(32)` | NOT NULL | Task 状态 |
| `completion_type` | `VARCHAR(32)` | NULL | `EXECUTED` / `REUSED` |
| `parse_fingerprint` | `CHAR(64)` | NOT NULL | Task 实际使用的指纹 |
| `chunk_count` | `INT UNSIGNED` | NOT NULL DEFAULT 0 | 关联 Chunk 数 |
| `retry_count` | `INT UNSIGNED` | NOT NULL DEFAULT 0 | 业务重试次数 |
| `max_retry` | `INT UNSIGNED` | NOT NULL DEFAULT 3 | 最大业务重试次数 |
| `last_error_code` | `VARCHAR(128)` | NULL | 最后一次错误分类 |
| `last_error_message` | `VARCHAR(2000)` | NULL | 最后一次错误信息 |
| `cancel_requested_at` | `DATETIME(6)` | NULL | 收到取消请求时间 |
| `heartbeat_at` | `DATETIME(6)` | NULL | Worker 最近心跳 |
| `created_at` | `DATETIME(6)` | NOT NULL | 创建时间 |
| `started_at` | `DATETIME(6)` | NULL | 开始执行时间 |
| `finished_at` | `DATETIME(6)` | NULL | 结束时间 |
| `updated_at` | `DATETIME(6)` | NOT NULL | 最后更新时间 |

约束和索引：

```text
PRIMARY KEY (id)
FOREIGN KEY (generation_id) REFERENCES generations(id)
FOREIGN KEY (document_id) REFERENCES documents(id)
UNIQUE KEY uk_task_generation_no (generation_id, task_no)
INDEX idx_task_generation_status (generation_id, status)
INDEX idx_task_status_updated (status, updated_at)
INDEX idx_task_heartbeat (status, heartbeat_at)
```

Task 状态：

```text
PENDING
QUEUED
RUNNING
RETRY_WAIT
SUCCESS
FAILED
CANCEL_REQUESTED
CANCELED
```

`REUSED` 不作为 Task 状态，而写入 `completion_type`；被复用的 Task 最终仍然是 `SUCCESS`。

## 4.5 `task_chunk_refs`：Task 与 ES Chunk 关联表

这个表只保存关系和顺序，不保存 Chunk 正文或向量。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `BIGINT UNSIGNED` | PK AUTO_INCREMENT | 关联记录 ID |
| `task_id` | `CHAR(36)` | NOT NULL | Task ID |
| `generation_id` | `CHAR(36)` | NOT NULL | 冗余 Generation ID |
| `chunk_id` | `VARCHAR(128)` | NOT NULL | ES `_id` |
| `chunk_index` | `INT UNSIGNED` | NOT NULL | 文档内顺序 |
| `relation_type` | `VARCHAR(32)` | NOT NULL | `PRODUCED` / `REUSED` |
| `created_at` | `DATETIME(6)` | NOT NULL | 创建时间 |

约束和索引：

```text
PRIMARY KEY (id)
FOREIGN KEY (task_id) REFERENCES tasks(id)
FOREIGN KEY (generation_id) REFERENCES generations(id)
UNIQUE KEY uk_task_chunk (task_id, chunk_id)
INDEX idx_ref_generation_index (generation_id, chunk_index)
INDEX idx_ref_chunk (chunk_id)
```

复用规则：

```text
旧 Task 成功且指纹完全匹配
    → 新 Task 新增 relation_type=REUSED 的关联记录
    → ES Chunk 不复制
    → ES Chunk.generation_ids 增加新 Generation ID
```

## 4.6 `task_dispatch_outbox`：Task 投递可靠性表

为了避免 MySQL 事务已经创建 Task，但 RabbitMQ 发布失败导致 Task 永久停留在 `PENDING`，建议增加 Outbox 表。它不是业务结果表，而是 MySQL 到 RabbitMQ 的可靠投递中间层。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `CHAR(36)` | PK | Outbox 消息 ID |
| `task_id` | `CHAR(36)` | NOT NULL | 对应 Task |
| `message_type` | `VARCHAR(64)` | NOT NULL | `TASK_EXECUTE` |
| `payload` | `JSON` | NOT NULL | RabbitMQ 消息体 |
| `status` | `VARCHAR(32)` | NOT NULL | `PENDING` / `PUBLISHED` / `DEAD` |
| `attempt_count` | `INT UNSIGNED` | NOT NULL DEFAULT 0 | 发布尝试次数 |
| `next_attempt_at` | `DATETIME(6)` | NOT NULL | 下次尝试时间 |
| `published_at` | `DATETIME(6)` | NULL | 成功发布时间 |
| `last_error` | `VARCHAR(2000)` | NULL | 最后错误 |
| `created_at` | `DATETIME(6)` | NOT NULL | 创建时间 |
| `updated_at` | `DATETIME(6)` | NOT NULL | 更新时间 |

约束和索引：

```text
PRIMARY KEY (id)
FOREIGN KEY (task_id) REFERENCES tasks(id)
UNIQUE KEY uk_outbox_task_type (task_id, message_type)
INDEX idx_outbox_dispatch (status, next_attempt_at)
```

第一版如果不实现独立 Outbox Worker，也必须保留“数据库事务提交成功后再发布”的约束，并提供扫描 `PENDING/QUEUED` Task 的补偿接口。推荐直接实现 Outbox，避免双写不一致。

## 5. Elasticsearch Chunk 索引设计

### 5.1 索引名称

```text
rag_demo_chunks_v1
```

后续 Mapping 发生不兼容变化时，新建 `rag_demo_chunks_v2`，通过 alias 切换，不直接修改线上字段类型。

### 5.2 Chunk 文档字段

| 字段 | ES 类型 | 说明 |
|---|---|---|
| `_id` | string | Chunk ID，由应用生成 |
| `document_id` | `keyword` | Document ID |
| `content` | `text` | Chunk 正文 |
| `embedding` | `dense_vector` | 向量，维度由 Embedding 配置决定 |
| `chunk_index` | `integer` | 文档内顺序 |
| `page_start` | `integer` | 起始页 |
| `page_end` | `integer` | 结束页 |
| `source_task_id` | `keyword` | 最初生产该 Chunk 的 Task |
| `generation_ids` | `keyword[]` | 当前使用该 Chunk 的 Generation 集合 |
| `metadata` | `object` | 标题、块类型、图片引用等来源信息 |
| `content_hash` | `keyword` | Chunk 正文指纹 |
| `chunk_fingerprint` | `keyword` | Chunk 配置和内容指纹 |
| `created_at` | `date` | 创建时间 |
| `updated_at` | `date` | generation_ids 最后更新时间 |

### 5.3 Mapping 示意

```json
{
  "settings": {
    "number_of_shards": 1,
    "number_of_replicas": 0
  },
  "mappings": {
    "dynamic": "strict",
    "properties": {
      "document_id": { "type": "keyword" },
      "content": { "type": "text" },
      "embedding": {
        "type": "dense_vector",
        "dims": 2048,
        "index": true,
        "similarity": "cosine"
      },
      "chunk_index": { "type": "integer" },
      "page_start": { "type": "integer" },
      "page_end": { "type": "integer" },
      "source_task_id": { "type": "keyword" },
      "generation_ids": { "type": "keyword" },
      "metadata": {
        "type": "object",
        "dynamic": true
      },
      "content_hash": { "type": "keyword" },
      "chunk_fingerprint": { "type": "keyword" },
      "created_at": { "type": "date" },
      "updated_at": { "type": "date" }
    }
  }
}
```

`embedding.dims` 必须与实际 Embedding 模型返回维度一致。若模型维度变化，不能直接修改 Mapping，必须创建新索引版本并重新构建 Chunk。顶层 Mapping 仍保持 `dynamic: strict`，但 `metadata` 保持动态，是因为 MinerU 不同 block 类型和版本会增加 OCR、图片关系、表格等来源字段；这样可以约束 Chunk 顶层结构，同时保留解析元数据的扩展性。

### 5.4 Chunk ID 和复用

推荐使用稳定的应用侧 Chunk ID：

```text
chunk_id = sha256(
    document_id
    + parse_fingerprint
    + task_page_range
    + chunk_index
    + content_hash
)
```

同一 Document、同一解析指纹、同一页范围和同一 Chunk 内容会生成相同的 Chunk ID，因此重新解析时可以复用 ES 文档。

配置变化后 `parse_fingerprint` 变化，会产生新的 Chunk ID，不覆盖旧 Chunk。

### 5.5 ES 写入和清理规则

写入：

1. Worker 先用稳定 ID执行 ES `index` 或 `create`。
2. 写入成功后，在 MySQL 插入 `task_chunk_refs`。
3. 批量写入完成后，再把 Task 更新为 `SUCCESS`。
4. 更新 `generation_ids` 必须使用 ES 乐观并发或脚本更新，不能直接用旧数组覆盖并发更新。

复用：

```text
ES Chunk 已存在
    → 不复制 content 和 embedding
    → 原子地把新 generation_id 加入 generation_ids
    → MySQL 新增 task_chunk_refs
```

清理：

```text
删除某个 Generation
    → 查询该 Generation 的 task_chunk_refs
    → 从 ES generation_ids 中移除该 Generation
    → generation_ids 为空时才删除 ES Chunk
```

禁止按照 `task_id` 直接删除 ES Chunk，因为同一物理 Chunk 可能被多个 Generation 共享。

## 6. MinIO 对象存储设计

### 6.1 Bucket

建议创建：

```text
rag-demo-files       原始上传文件
rag-demo-artifacts   MinerU 解析结果、图片和中间文件
```

### 6.2 Object Key

原始文件：

```text
knowledge-bases/{knowledge_base_id}/documents/{document_id}/source/{safe_filename}
```

解析产物：

```text
knowledge-bases/{knowledge_base_id}/documents/{document_id}/generations/{generation_id}/mineru/{task_id}/...
```

图片对象：

```text
knowledge-bases/{knowledge_base_id}/documents/{document_id}/generations/{generation_id}/images/{image_id}.{ext}
```

### 6.3 MinIO 生命周期

原始文件：

```text
只要 Document 未被删除，就保留
```

成功 Generation 的解析产物：

```text
保留，供文档详情页、图片展示和问题排查使用
```

失败或取消 Generation 的解析产物：

```text
在 reuse_expires_at 到期后清理
```

MinIO 删除必须由后台清理任务执行，不能在用户取消请求中同步等待所有对象删除完成。

## 7. RabbitMQ 离线初始化设计

### 7.1 交换机和队列

```text
Exchange：rag_demo.tasks
类型：topic
Durable：true

Queue：rag_demo.task.execute
Durable：true

Routing Key：task.execute

Dead Letter Exchange：rag_demo.dlx
Dead Letter Queue：rag_demo.task.dead
```

### 7.2 Task 消息

```json
{
  "message_id": "uuid",
  "message_type": "TASK_EXECUTE",
  "task_id": "uuid",
  "generation_id": "uuid",
  "document_id": "uuid",
  "attempt": 1,
  "created_at": "2026-09-18T00:00:00Z"
}
```

消息中不携带完整 PDF、完整解析结果或 Chunk 正文，只携带定位 Task 所需的 ID。Worker 通过 Task 和 Document 从 MySQL 获取配置，再从 MinIO 下载原文件。

### 7.3 ACK 规则

```text
Task 状态已经可靠写入 MySQL：
    SUCCESS / FAILED / CANCELED / RETRY_WAIT
    → ACK

Task 最终状态没有可靠写入 MySQL：
    → 不 ACK，让 RabbitMQ 重新投递
```

重复消息到达时：

```text
Task 已是 SUCCESS / FAILED / CANCELED
    → 不重复执行，直接 ACK

Task 是 PENDING / QUEUED / RETRY_WAIT
    → 根据状态和 retry_count 判断是否继续执行
```

业务重试和消息重试必须分开：

```text
Embedding 超时、MinerU 暂时失败、ES 临时不可用
    → 业务重试，更新 retry_count 和 RETRY_WAIT

Worker 崩溃、数据库状态回写失败、进程断电
    → 不 ACK，依赖 RabbitMQ 重新投递
```

## 8. Generation 和 Task 的关键事务

### 8.1 创建首次解析

MySQL 事务内完成：

1. 创建 Document 或确认 Document 存在。
2. 检查 `building_generation_id IS NULL`。
3. 创建 Generation，状态为 `BUILDING`。
4. 按页范围创建 Task，状态为 `PENDING`。
5. 创建对应 Outbox 记录。
6. 回写 `documents.building_generation_id`。
7. 提交事务。

事务提交后由 Outbox 发布器发送 RabbitMQ 消息，并将 Task 更新为 `QUEUED`。

### 8.2 创建重新解析

MySQL 事务内：

1. 锁定 Document 行。
2. 如果存在 `building_generation_id`，拒绝创建新的解析版本。
3. 计算新的 `parse_fingerprint`。
4. 创建新的 Generation。
5. 为每个页范围创建新的 Task。
6. 只匹配旧的成功 Task，不匹配失败 Task。
7. 对匹配成功的 Task 创建 `REUSED` 关系或待复用标记。
8. 对不匹配的 Task 创建 Outbox 消息。
9. 设置新的 `building_generation_id`。
10. 提交事务。

复用判断至少包括：

```text
原始文件 hash
解析器类型和版本
解析配置
切块器版本和配置
Embedding 模型和版本
Embedding 维度
核心页范围
上下文页规则
```

### 8.3 Generation 成功切换

当最后一个 Task 成功后，在 MySQL 事务中：

1. 锁定 Generation 和 Document。
2. 再次统计该 Generation 的 Task 状态。
3. 只有全部 Task 为 `SUCCESS` 才允许继续。
4. Generation 更新为 `SUCCESS`。
5. `documents.current_generation_id` 更新为当前 Generation。
6. `documents.building_generation_id` 清空。
7. 提交事务。

切换完成后，检索接口读取新的 `current_generation_id`。

## 9. 检索规则

检索请求首先从 MySQL 获取：

```text
knowledge_base_id
document_id（如果指定）
document.current_generation_id
```

然后对 ES 执行向量检索，并强制加入过滤条件：

```text
generation_ids contains current_generation_id
```

典型逻辑：

```text
查询向量
    ↓
ES knn 搜索
    ↓
过滤 document_id
    ↓
过滤 generation_ids 包含当前生效 Generation
    ↓
返回 content、chunk_id、页码和 metadata
```

禁止直接按照 `document_id` 检索而不过滤 Generation，否则重新解析期间可能把旧版本和新版本混在一起。

## 10. 离线初始化顺序

```text
1. 启动 MySQL
2. 创建 rag_demo 数据库和用户
3. 执行 MySQL DDL
4. 启动 Elasticsearch
5. 创建 rag_demo_chunks_v1 Mapping
6. 启动 MinIO
7. 创建 rag-demo-files 和 rag-demo-artifacts Bucket
8. 启动 RabbitMQ
9. 创建 Exchange、Queue、Routing Key 和 DLQ
10. 启动 API 服务
11. 启动 Task Worker
12. 启动 Outbox 发布器和过期资源清理器
```

本地端口建议：

| 服务 | 端口 | 用途 |
|---|---:|---|
| MySQL | 3306 | 业务元数据 |
| Elasticsearch | 9200 | Chunk 和向量检索 |
| MinIO API | 9000 | 文件 API |
| MinIO Console | 9001 | 文件管理控制台 |
| RabbitMQ AMQP | 5672 | 消息投递 |
| RabbitMQ Management | 15672 | 消息管理台 |

## 11. 第一版不做的内容

以下内容暂时不加入第一版，避免偏离当前学习目标：

1. 多租户和复杂权限模型。
2. 多副本 Worker 的高级调度策略。
3. 分布式锁服务；第一版使用 MySQL 行锁和唯一约束。
4. 多种向量模型同时共存。
5. Chunk 内容的跨文档全局去重。
6. 复杂的增量解析算法。
7. 图片向量、多模态 Embedding 和图片相似度检索。
8. LLM 对话生成链路；当前先完成知识库、解析、切块和 Chunk 查看。
9. 旧 PostgreSQL 代码兼容迁移。

## 12. 验证标准

### 基础链路

```text
创建知识库
    → 上传 PDF 到 MinIO
    → MySQL 创建 Document、Generation、Task
    → RabbitMQ 收到 Task 消息
    → Worker 解析并写入 ES Chunk
    → Task SUCCESS
    → Generation SUCCESS
    → Document.current_generation_id 更新
```

### 重新解析

验证以下情况：

1. 新 Generation 构建期间，旧 Generation 仍能检索。
2. 配置不变时，成功 Task 可以复用旧 Chunk。
3. 配置变化时，只有匹配的 Task 复用，其他 Task 重新执行。
4. 新 Generation 全部成功后，Document 切换到新版本。
5. 新 Generation 失败时，旧版本仍然可以检索。

### 取消解析

验证以下情况：

1. 取消请求先将 Generation 设置为 `CANCELING`。
2. Worker 观察到取消标记后，不再继续产生新的 Chunk。
3. 正在运行的 Task 最终变为 `CANCELED` 或完成当前可安全提交的步骤后变为 `CANCELED`。
4. 所有 Task 进入终态后，Generation 才变成 `CANCELED`。
5. 取消完成前不能重新解析同一 Document。
6. 取消后旧 `current_generation` 不受影响。

### 失败和重试

验证以下情况：

1. Embedding 超时进入业务重试，不立即丢失消息。
2. Task 状态回写失败时消息不 ACK。
3. 重复消息不会重复生成一组 Chunk。
4. 部分 Chunk 已写入时，Task 重新执行具有幂等性。
5. 共享 Chunk 清理时不会删除仍被其他 Generation 使用的 Chunk。

## 13. 交给实现窗口的执行边界

实现窗口应按本文件执行，不再引入 PostgreSQL，也不要把 Document、Generation、Task 状态迁移到 ES。

实现顺序建议：

```text
第一阶段：Docker Compose 和 MySQL/MinIO/RabbitMQ/ES 健康检查
第二阶段：MySQL DDL、ES Mapping、MinIO Bucket、RabbitMQ 拓扑
第三阶段：文件上传和知识库/Document API
第四阶段：Generation/Task 创建、取消和状态查询
第五阶段：RabbitMQ Producer、Outbox 和 Worker
第六阶段：MinerU、切块、Embedding、ES Chunk 写入
第七阶段：Chunk 复用、Generation 切换和清理
第八阶段：知识库管理界面和文档 Chunk 对比界面
第九阶段：聊天界面壳子和端到端验证
```

这份设计只定义离线基础设施、数据表、状态边界和验证标准，不包含具体业务代码实现。
