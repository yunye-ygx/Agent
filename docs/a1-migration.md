# A1 升级说明（尚未对现有服务执行）

用户仅授权编写模型/迁移文件，本次没有连接现有 MySQL 或 ES 执行迁移，没有修改 `.env`、Docker、CI，没有删除文件。

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

6. 审核通过后，才在已停止业务的隔离副本上尝试转换，例如：

   ```powershell
   python -m tools.migrate_a1_artifacts --apply --target-index rag_demo_chunks_a1_v2
   ```

   这是有外部写入的命令，**本次没有运行**。源索引从现有配置读取，目标必须另行指定且不同于源。转换按每个成功 Task 的完整旧引用读取源文本/向量，创建新的稳定 ID 与独立归属。数量、维度或来源不完整时终止；不删除源索引或源产物。失败可在排查后重试稳定覆盖目标，但成功完成后的重复执行会被可用性预检拒绝，避免对在线系统误操作。

7. 转换刷新后从目标索引按批次读回核对任务数量，最后一个 DB 事务才标可用并固定各 KB 的有效 embedding 配置。目标索引仍使用单一向量维度；不兼容的非生效历史批次不转换、不标可用，生效批次不兼容或同 KB 混模型则直接终止。不能用此脚本充当在线模型迁移工具。
8. 人工核对 active_run_id 指针、Task 数量、源/目标 ID、向量维度和检索结果，再单独审批修改实际环境中的 `ELASTICSEARCH_INDEX` 指向新索引并启动新程序。本次没有自动修改配置。失败时保留旧服务备份，恢复流程须恢复旧 DB 与旧程序配套使用，不能用旧程序直接运行已改状态语义的新库。

## 外部适配器边界

- 已有 embedding HTTP 客户端继续使用，模型/维度/地址随批次固定，密钥不写快照。KB 首次启动解析固定 embedding 空间，后续切换返回 409。
- 远端 MinerU 没有提供实际地址、部署版本或响应样本。`RemoteMinerUParser` 仅实现明确的 `file_parse-v2` 契约：`POST /file_parse` multipart `files`、`backend`、`parse_method`、`return_content_list=true`，读取 `results.<name>.content_list`。测试使用 `httpx.MockTransport`，不声称已与真实 MinerU 实例联通。
- 未配置远端地址时保留已有本地 CLI 适配器。PDF 在调用前用 pypdf 提取 `[start,end)` 子文件，因此不依赖未经核实的远端页范围参数。跨边界表格、上下文质量仍需样例验证。
- 新增参数在 `app/core/config.py`。原配置 `task_max_retry=3` 对应最多 4 次领取；租约默认沿用 15 分钟、续约 30 秒、执行上限 30 分钟；ES 每批 100、投递/维护间隔 5 秒、Worker 并发/预取分别默认 1。这些是学习环境的保守默认值，未做生产压测。

## 本次验证范围

隔离 SQLite 验证事务结果，模拟适配器验证故障路径；没有实测 MySQL InnoDB 行锁、RabbitMQ broker 崩溃恢复、ES 集群刷新或真实 OCR/embedding 稳定性。SQL 和批量转换脚本均未在真实服务上执行。实际升级前应在隔离副本上补充这些记录。
