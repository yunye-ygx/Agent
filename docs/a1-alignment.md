# A1 对齐记录（2026-10-21）

依据：`C:/javapractice/Job/output/study/04_A1离线知识处理模块实现规格.md`。
用户已确认保留 Python/FastAPI，允许模型及多文件改造、编写迁移脚本；不执行现有 MySQL/ES 迁移，不修改 `.env`、部署、CI，不删除文件。

## 工程核实与差异

| 项目 | 现状与证据 | 目标 |
|---|---|---|
| 技术栈 | `pyproject.toml`：Python、SQLAlchemy async、aio-pika、ES 8、MinIO | 保留 Python；Generation 对应 ParseRun |
| 上传分离 | `documents_v2.upload_document` 已只保存文件与 Document；README 过时 | 保留并修正文档；补齐 TXT/MD |
| 批次指针 | Document 的 current_generation_id 是有效版本，building_generation_id 结束后清空 | 明确 active_run_id/current_run_id，保留最近批次 |
| 配置 | Generation 存快照，但 Worker 使用进程启动时的 parser/chunker/embedding | 按快照创建处理器，固定文件版本及路径 |
| PDF 范围 | 每 Task 重复解析整 PDF 后筛页；使用 1 基闭区间 | 0 基半开区间，先提取子 PDF、映射原页码 |
| 执行资格 | heartbeat 时间；无 attempt_id，完成/续约只按 task_id | 原子领取、次数、租约、执行截止、旧执行者隔离 |
| 消息 | Outbox；attempt 与投递轮次混用；无旧确认隔离 | dispatch_seq；confirm + mandatory 路由失败；提交后发送 |
| 恢复 | reclaim_expired_running_tasks 有定义，无后台调用；RUNNING 重复消息立即重投 | 独立恢复扫描，退避、有限尝试、取消收尾 |
| 复用 | API 内直接标新 Task SUCCESS；共享 ES generation_ids、TaskChunkRef | 调度 COPY；目标独立 ID；完整分页、核对数量、有限重试 |
| 写入/发布 | 单条 ES wait_for；所有 Task 成功立即切指针；无可恢复发布扫描 | 分批逐项检查；发布前核验全部归属及搜索可见；恢复收尾 |
| 检索 | KB 查询只取第一个 Document | KB 所有有效批次过滤；排除已删除文档 |
| 取消/失败 | 缺少统一 Document 锁边界；失败等所有任务终止；取消依赖 Worker 回应 | 撤销资格，停止剩余工作，保留旧有效结果 |
| 清理 | 仅按 TaskChunkRef 清理失败批次；没有活动 COPY 源保护 | 归属清理、保护有效批次/COPY 源、保留窗口、反复清扫迟到记录 |

## 实施顺序与文件映射

1. `mysql_models.py` / `mysql_repository.py`：模型、事务状态转换、快照、dispatch_seq、租约、幂等统计。
2. `documents_v2.py` / `task_processor_service.py`：批次创建、PARSE/COPY、阶段边界、超时、资格校验。
3. `outbox_publisher.py` / `worker.py` / 后台维护入口：投递补偿、手动 ACK、执行恢复、发布扫描。
4. `elasticsearch_chunks.py` / `retrieval.py` / `generation_cleanup.py`：独立记录、分页复制、有效批次检索、安全清理。
5. 数据迁移草案、README 与验收记录；不连接现有服务执行变更。

## 验证与边界

- 改造前 `python -m pytest -q`：原有测试全部通过（108 个参数化测试用例）；它们有些断言旧的共享复用行为，需随契约更新。
- 验证命令：`python -m pytest -q`、`python -m ruff check app tests`。
- 新增隔离 SQLite SQLAlchemy 事务测试（同步引擎的异步测试包装，无新增安装依赖），以及模拟 MQ、ES、MinerU、embedding 故障注入。
- SQLite 不验证 MySQL InnoDB 的实际并发锁行为；真实 MySQL/RabbitMQ/ES 联调另行记录，不把适配器测试等同部署验证。
- 未提供远端 MinerU 实例、接口版本或凭据。先保留明确标识的本地适配器，增加可注入远端适配器契约；不假定远端取消能力。
- 真实 MinerU/OCR 和 embedding 的输出稳定性、跨页表格质量、资源参数均需真实样例验证。

## 组件依据

- aio-pika confirm/mandatory：<https://docs.aio-pika.com/apidoc.html>
- ES 8 bulk 逐项结果及刷新：<https://www.elastic.co/guide/en/elasticsearch/reference/8.19/docs-bulk.html>
- 本记录区分规格要求、工程选择与实测结果；后续验收结果见本文末尾。

## 本次实现结果

- 上传与解析保持分离；限制输入大小，PDF 预先验证页数，TXT/MD 不再走 PDF 页数读取。
- Generation 作为 ParseRun；快照固定原文件 hash、存储地址、解析器配置/版本、切块策略、embedding 模型/维度/地址。Worker 按快照构造处理组件，密钥仍在服务端。
- Document 通过 ORM 同义属性提供 `current_run_id` 与 `active_run_id`；沿用物理列而非破坏性改名。API 的旧 building 展示字段仅在有效执行状态有值，最近批次永不因结束自动清空。
- PARSE/COPY 都先落为 WAITING_PUBLISH。领取同时生成 attempt_id、次数加一、设置租约/截止；续约、成功和失败都检查当前资格。失败/过期有限重试、退避、dispatch_seq 递增，不创建新批次。
- Task 成功与 run 统计共用事务；发布独立验证搜索可见性、全部目标数量/归属/ID/文本/向量，再在统一控制锁下切换 active_run_id。
- Outbox 持久化补投、确认轮次校验、路由失败处理；Worker 手动 ACK；独立维护服务执行恢复/取消收尾/发布/清理，已接入 FastAPI lifespan。
- 取消/失败撤销全部有效资格并停止剩余任务。新批次构建和失败期间不改变旧有效批次。
- COPY 分页读取源产物、核对完整性，写新的 task/run ID；异常与宕机同样有限重试。活动 COPY 引用保护源；清理先持久化不可复用标记，网络删除在事务外执行，重复扫描可清迟到产物。
- KB 固定 embedding 空间；检索查询全部未删除文档的有效批次，拒绝未发布结果及不兼容模型。
- 新增确定性产物摘要 `output_fingerprint`：固定文本、chunk_key、来源位置及元数据的集合，重试发现漂移就在 ES 写入前拒绝混写。这是本次为稳定集合前提增加的工程保护，不是业务文本摘要，也不证明真实 OCR/模型输出已稳定。

独立只读审查提出并修复：MySQL RR 下复用/源保护读取旧快照、批次版本号读取旧快照、等锁期间租约到期、跨扫描/取消/清理锁顺序，以及 KB/Document 锁次序与旧计数覆盖。关键决策采用锁定读取并刷新 ORM 实例；控制转换按 Document→Run→Task，创建/删除需要计数或模型固定时先锁 KB。清理每个候选提交，恢复与取消扫描分事务。

## 验收场景对应的隔离测试

以下是逻辑和故障注入覆盖，不是实际进程宕机、网络分区或外部集群联调。具体断言核对状态、次数、统计、ID、数量和有效指针。

| 规格编号 | 对应证据（tests/ 下） | 验证内容与边界 |
|---|---|---|
| T01 | test_a1_lifecycle：rollback_removes_entire_new_run_and_outbox | SQLite 真实事务回滚，无孤立批次/任务 |
| T02/T03 | test_a1_recovery：dispatch_reservation_throttles_uncertain_publish_without_execution_attempt | 已提交等待任务可再次安排，投递节流不增加执行次数；broker 未实测 |
| T04/T05 | test_a1_recovery：consumer_can_finish_before_confirm、old_confirm_cannot_mark_new_dispatch_pending | 先消费后确认不倒退，旧 seq 不覆盖新轮次 |
| T06 | test_a1_lifecycle：claim_is_once_per_lease_and_old_dispatch_is_ignored | 重复领取仅一次成功；真实并发消费者/行锁尚未实测 |
| T07/T13 | test_a1_recovery：repeated_crashes_eventually_fail_without_infinite_reactivation | 模拟每次领取后失联，过期接管计次并最终失败 |
| T08 | test_a1_lifecycle：expired_attempt_cannot_renew_or_complete_and_takeover_counts、lease_is_checked_against_time_after_waiting_for_document_lock | 旧标识不能提交或续约；含等待锁跨过到期时间的注入 |
| T09 | test_offline_ingestion_acceptance：partial_es_parse_write_converges_without_duplicate_records；test_a1_storage：bulk_write_checks_every_result | 部分写失败重试收敛，逐项失败不误报成功 |
| T10 | test_offline_ingestion_acceptance：es_complete_but_db_commit_failed_is_safe_to_retry | ES 已写后 DB 完成失败，稳定 ID 重试，无统计提前成功 |
| T11 | test_offline_ingestion_acceptance：task_success_and_statistics_rollback_together | SQL 更新后、提交前注入故障，Task 和统计一起回滚 |
| T12 | test_offline_ingestion_acceptance：redelivery_after_success_before_ack_does_not_repeat_business | 成功后重投不重复解析、embedding、写入或统计 |
| T14 | test_a1_lifecycle：cancel_revokes_attempt；test_offline_ingestion_acceptance：cancel_during_embedding；test_a1_api：cancel_endpoint_only_finishes_its_own_run | 取消撤销资格、不写正常结果、限定单批次收尾 |
| T15 | test_a1_pipeline：copy_missing_source_exhausts_attempts_and_preserves_active_run；test_a1_api：kb_search_queries_all_active_documents | 构建失败保留旧指针，ES 查询只带 active run 集合 |
| T16/T17 | test_a1_api：reparse_schedules_copy_without_premarking_success；test_a1_pipeline：copy_partial_failure_retries_entire_task_without_embedding | COPY 正常调度，部分失败自动重试，目标独立 ID，不重新 embedding |
| T18 | test_a1_pipeline：copy_missing_source_exhausts_attempts_and_preserves_active_run | COPY 次数耗尽失败、不发布，不修改源成功状态 |
| T19 | test_generation_reuse：incompatible_run_does_not_select_copy；test_offline_ingestion_acceptance：kb_rejects_model_switch_without_migration | 配置不兼容不选 COPY，KB 模型切换受限 |
| T20 | test_a1_recovery：finalizer_recovers_after_last_success、cancel_between_es_verification_and_publish | 独立扫描发布；取消后拒绝最终切换 |
| T21 | test_a1_storage：source_task_read_returns_all_pages；test_a1_pipeline：copy_missing_source | 读完分页、保留向量、数量缺失不成功 |
| T22 | test_a1_recovery：cleanup_protects_active_run_and_live_copy_source、publication_preserves_old_run_for_inflight_search_window；test_generation_cleanup：cleanup_can_remove_late_results | 保护有效和引用源、旧结果窗口、重复清理；没有真实并发 ES 检索 |

附加测试覆盖远端 multipart 契约、旧结构启动拒绝自动迁移、旧共享产物转换、确定性漂移保护、手动 ACK/DB 故障 NACK、后台服务启动关闭。

## 最终实测记录

2026-10-21，在 `C:/javapractice/new` 执行：

```text
python -X utf8 -m pytest -o addopts='' -q --junitxml=docs/a1-test-results.xml
130 passed in 3.69s

python -X utf8 -m ruff check app tests tools/migrate_a1_artifacts.py
All checks passed!

python -X utf8 -m tools.migrate_a1_artifacts
Offline plan only; no connections.

SQLAlchemy CreateTable.compile(MySQL dialect)
7 张表 DDL 离线编译成功（不是执行 SQL 迁移）
```

迁移文件及后续隔离演练步骤：[a1-migration.md](a1-migration.md)。本次没有生产库、真实服务端到端或真实并发锁测试证据，没有执行 MySQL/ES 迁移；不将这些尚待完成的验证标记为已通过。
