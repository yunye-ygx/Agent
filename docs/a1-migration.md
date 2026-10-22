# A1 升级说明（本机已于 2026-10-22 执行）

本文件先前描述的是迁移前计划。用户授权后，已按下文执行本机数据升级；具体备份、校验和保留项见文末执行记录。未执行文档外的远程操作。

## 新建隔离环境

空数据库由现有 bootstrap 的 `create_all` 创建新模型；空 ES 索引由 `ensure_index` 创建明确的 kb_id/doc_id/run_id/task_id/chunk_key mapping。首次启动仍需正常配置已有的服务连接。已有旧表/旧共享索引会报出需迁移的错误，启动不执行自动 ALTER 或旧数据转换。

## 现有数据升级的审核顺序

1. **停止所有旧 API、Worker、Publisher、维护程序，备份 MySQL、MinIO 和旧 ES 索引。** 旧 Worker 没有 attempt 隔离，不能在升级期间继续写入。这是后续实际升级的步骤，本次没有执行。
2. 在隔离副本上审核并执行 `tools/migrations/20261021_a1.sql`。SQL 是一次性草案，含自动提交 DDL，不能宣称可整段事务回滚，也不能重复盲跑。按实际已存在字段调整后再演练。
3. SQL 新增 KB embedding 固定配置、批次文件快照/统计/发布标记/可用性、Task 执行资格和调度字段。旧物理 Document 指针列不改名，使用 ORM 同义属性表达新语义；最近批次指针不再清空。
4. 所有未完成旧任务业务取消，旧 Outbox 退休。旧成功任务及 TaskChunkRef 保留供产物转换；历史 attempt_count 仅是基于旧 retry_count 的近似值，不是新机制的验证证据。旧解析版本无法追溯，快照标记为 `legacy-unverified`，首轮升级后不将它假称为严格兼容的新解析器结果。
5. 先运行本地说明模式（没有连接）：

   ```powershell
   python -m tools.migrate_a1_artifacts
   ```

6. 审核通过后，在已停止业务的隔离副本上尝试转换，例如：

   ```powershell
   python -m tools.migrate_a1_artifacts --apply --target-index rag_demo_chunks_a1_v2
   ```

   这是有外部写入的命令。2026-10-22 已在隔离副本演练并在正式库执行。源索引从现有配置读取，目标必须另行指定且不同于源。转换按每个成功 Task 的完整旧引用读取源文本/向量，创建新的稳定 ID 与独立归属。数量、维度或来源不完整时终止；不删除源索引或源产物。失败可在排查后重试稳定覆盖目标，但成功完成后的重复执行会被可用性预检拒绝，避免对在线系统误操作。

7. 转换刷新后从目标索引按批次读回核对任务数量，最后一个 DB 事务才标可用并固定各 KB 的有效 embedding 配置。目标索引仍使用单一向量维度；不兼容的非生效历史批次不转换、不标可用，生效批次不兼容或同 KB 混模型则直接终止。不能用此脚本充当在线模型迁移工具。
8. 人工核对 active_run_id 指针、Task 数量、源/目标 ID、向量维度和检索结果，再单独审批修改实际环境中的 `ELASTICSEARCH_INDEX` 指向新索引并启动新程序。本次没有自动修改配置。失败时保留旧服务备份，恢复流程须恢复旧 DB 与旧程序配套使用，不能用旧程序直接运行已改状态语义的新库。

## 外部适配器边界

- 已有 embedding HTTP 客户端继续使用，模型/维度/地址随批次固定，密钥不写快照。KB 首次启动解析固定 embedding 空间，后续切换返回 409。
- 远端 MinerU 没有提供实际地址、部署版本或响应样本。`RemoteMinerUParser` 仅实现明确的 `file_parse-v2` 契约：`POST /file_parse` multipart `files`、`backend`、`parse_method`、`return_content_list=true`，读取 `results.<name>.content_list`。测试使用 `httpx.MockTransport`，不声称已与真实 MinerU 实例联通。
- 未配置远端地址时保留已有本地 CLI 适配器。PDF 在调用前用 pypdf 提取 `[start,end)` 子文件，因此不依赖未经核实的远端页范围参数。跨边界表格、上下文质量仍需样例验证。
- 新增参数在 `app/core/config.py`。原配置 `task_max_retry=3` 对应最多 4 次领取；租约默认沿用 15 分钟、续约 30 秒、执行上限 30 分钟；ES 每批 100、投递/维护间隔 5 秒、Worker 并发/预取分别默认 1。这些是学习环境的保守默认值，未做生产压测。

## 本次验证范围

迁移 SQL 和 Chunk 转换已在本机 MySQL / ES 副本演练并正式执行，目标索引刷新和按 Run 读回核对通过。仍未实测 MySQL InnoDB 并发锁竞争、RabbitMQ broker 崩溃恢复或真实 OCR/embedding 稳定性。

## 本机执行记录（2026-10-22）

- 执行前备份目录：`C:\javapractice\backups\rag-demo-a1-20261022-082454`。包含 MySQL gzip 转储及校验值、ES mapping/settings 和 123 条源记录、MinIO 两个 bucket 的 17 个对象、迁移前 `.env` 副本。转储和对象大小均已验证。
- 在独立克隆库 `rag_demo_a1_stage_20261022` 上执行 SQL，A1 schema 校验通过；另将 196 条 Task Chunk 产物转换到 `rag_demo_chunks_a1_stage_20261022` 并验证 mapping。演练完成后已删除克隆库并撤销其授权，也已删除临时索引。
- 正式执行 `tools/migrations/20261021_a1.sql`。MySQL 保留原 7 个知识库、7 个文档、9 个 Generation、9 个 Task 和 196 条 TaskChunkRef。旧未完成 Task 标记取消、旧 Outbox 标为 DEAD；旧 Generation 的产物标记为待迁移。
- 正式转换 196 条 Task Chunk 产物到 `rag_demo_chunks_a1_v2`，刷新后按 Run 核对成功。旧索引 `rag_demo_chunks_v1` 仍保留，计 123 条。
- 7 个文档中有 5 个当前 active_run 为 SUCCESS 且产物可用；另 2 个没有可检索的已发布批次，因此在线实验会排除它们。
- `.env` 中只把 `ELASTICSEARCH_INDEX` 从 `rag_demo_chunks_v1` 改为 `rag_demo_chunks_a1_v2`。恢复旧程序时应同时恢复 MySQL 备份与 `.env.pre-migration`；不要只切回旧索引而继续用新数据状态。
- 当前部署的真实模型调用、在线检索质量和 A2 MinIO 快照写入尚未由用户执行验证。Docker 服务与应用本机接口已确认启动成功。
