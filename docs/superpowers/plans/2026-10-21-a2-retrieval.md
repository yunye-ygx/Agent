# A2 在线检索实现计划

执行方式：executing-plans，在当前 checkout 的 codex/a2-retrieval-lab 分支实现；不提交、不推送、不改环境或数据库。

依据：C:/javapractice/Job/output/study/05_A2在线检索实验模块实现规格.md。

## 工程映射与适配

- FastAPI / SQLAlchemy / ES 8 / MinIO / 原生 HTML、JS，保持现有 A1 与旧 GET search 兼容。
- 用户确认：先用本机单用户模式。A2 检查 loopback、本地环境和同源，实验归属使用服务端签发的 cookie；这不是多租户授权。
- 现有 content 与 metadata.section_path 可搜索，不做 mapping 迁移；文档名称在数据库中，对标题兜底匹配映射到文档 ID。
- 分词先用确定性的中文单字、英数标识符规则，明确版本，不假装拥有 RAGFlow 词典。query 和候选使用同一规则；ES 字段沿用现有 analyzer。
- Rerank 使用已存模型配置的 provider_key=rerank、capabilities=[Rerank]，模型名复用现有 chat_model 存储列但 UI 标成 Rerank 模型；不修改表结构。
- MinIO 现有 artifacts bucket 保存候选、失败与派生结果，30 分钟 TTL。会话归属和知识库／文档状态再次校验。

## 工作清单

- [x] 1. 评分与参数：tests/test_retrieval_scoring.py 先红，再实现 app/retrieval/schemas.py、scoring.py；验证字段权重、空词、cosine、稳定排序和阈值。
- [x] 2. ES 混合召回：tests/test_hybrid_retrieval.py 验证一次请求、两路过滤、失败分片和候选上限；实现 app/retrieval/search.py。
- [x] 3. Rerank：tests/test_rerank.py 验证响应对齐、缺分、超时与输入边界；实现 app/retrieval/rerank.py。
- [x] 4. 实验服务与存储：tests/test_retrieval_experiments.py 验证成功、异常保留、手动降级、快照过期和幂等；实现 service.py、snapshots.py。
- [x] 5. HTTP 接入：tests/test_retrieval_api.py 验证本机访问、有效版本范围、配置兼容、快照归属；新增 retrieval_experiments.py 路由和 access.py、scope.py。
- [x] 6. 最小 UI：独立 retrieval.js/css 和检索入口；使用 API 实际数据显示诊断、导出、降级。新增 Rerank 配置入口，保持已有模型功能。
- [x] 7. 验证：新测试→全量 pytest→修改文件 lint→JS 语法与浏览器可用检查；如外部服务不可用，明确模拟与实际验证边界。记录 docs/a2-retrieval.md。

每个步骤先写行为测试并观察失败，再实现。必要的编译、数据库 fixture 和协议模拟在本地隔离测试中执行，不操作现有业务数据。

## 验证结果

- 全量 pytest：165 passed（2026-10-21）；修改文件 Ruff、JS 语法检查及 git diff --check 通过。
- 隔离浏览器：Rerank 失败保留、复用候选降级、阈值过滤空结果、窄屏和配置表单通过。
- 本机 A1 schema 与 ES 数据已在 staging clone 演练后升级；备份保留旧库、MinIO 对象、原 ES 索引与旧 `.env`。A2 FastAPI 已在 `127.0.0.1:8001` 启动，页面与会话接口通过。本轮没有代用户发起 query；真实 Embedding 响应、MinIO A2 快照和人工召回效果由用户继续验证。
