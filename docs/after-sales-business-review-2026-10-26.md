# 售后业务实现独立审查（2026-10-26）

> 后续变更说明：本报告之后，用户于同日决定取消「售后资料」「资料开放」两个页面，并把开放状态改为 `documents.support_open` 单字段、删除 `support_knowledge_documents` 表与 `model_code` 参数。因此报告中关于 enabled 映射的 R6 及“7 条开放映射”等描述已成为历史状态，不再适用于当前实现。

## 结论

主要业务、数据库、真实工具和页面已经落地，可以用于正常流程体验。不能据此认定计划已经完整、可靠地完成：账号切换、未知写入恢复、分页、退出和输入边界存在缺陷；真实 HTTP 事务路径还有需要补测的并发问题。建议先修复下列 P1 项，再继续接入 Agent。

这次只审查，没有修复业务代码、重置密码、运行迁移、导入数据或执行真实业务写请求。新增本报告；隔离测试使用临时文件和内存数据库。应用数据库只执行元数据及业务数量查询，没有读取密码或会话令牌。

## 1. 优先修复的问题

### R1 — P1：切换账号后，旧请求会重新显示前一账号的数据

位置：[business.js:147](/C:/javapractice/new/app/web/business.js:147)、[auth.js:34](/C:/javapractice/new/app/web/auth.js:34)。

A 发出订单请求但尚未返回，退出并登录 B，A 的请求随后完成。页面没有校验响应对应的登录身份/会话代次，会直接把 A 的结果写入 B 当前页面。退出时清空 DOM 不能阻止后续迟到响应再次渲染。详情、工具及管理页面也有同类模式。

**证据：** 使用真实 business.js、模拟 DOM 和可控 Promise 的 Node VM 复现，确认 Bob 身份下能渲染延迟返回的 Alice 订单。这不是后端放开了 B 的查询权限，而是浏览器泄露了之前合法取得的 A 数据。

**修复：** 登录变化递增会话代次、取消旧请求；所有渲染前验证代次。正常退出与 401 都统一清理业务、工具、模型配置和检索实验状态。新增延迟响应跨账号测试。

### R2 — P1：待确认工单的操作键会丢失，可能重复创建

位置：[business.js:535](/C:/javapractice/new/app/web/business.js:535)、[business.js:577](/C:/javapractice/new/app/web/business.js:577)、[tool-lab.js:115](/C:/javapractice/new/app/web/tool-lab.js:115)。

业务页面在 DOMContentLoaded 恢复 sessionStorage 的操作键，随后 /auth/me 成功发出 business:ready，又调用 resetBusinessState 删除它。若工单已提交但响应丢失，刷新后就无法通过页面继续核对原操作，再次提交会创建新键。

工具调试页的 pending 仅保存在内存，而且点击任意工具（包括再次点击 create_ticket）就直接清空；unknown 后换工具再回来也会重新预留键。

**证据：** Node VM 复现业务页“恢复成功=true；business:ready 后仍保留=false”；调试页由上述直接赋值及 reserveForCreateTicket 的新键路径确认。未向应用数据库制造重复工单。

**修复：** 按 user_id 保存未解决的操作，区分“同账号初始化”与“退出/换账号”；切换工具和刷新不能丢失未确认操作。结果 unknown 时继续查询或重试原键，不能隐式变成新操作。

### R3 — P2：分页接口存在，但页面只能访问第一页

位置：[business.js:147](/C:/javapractice/new/app/web/business.js:147)、[business.js:264](/C:/javapractice/new/app/web/business.js:264)、[business.js:384](/C:/javapractice/new/app/web/business.js:384)。

订单、设备、工单、客户列表固定请求第 1 页 50 条，没有翻页入口；创建工单只能选前 100 台设备。工单处理记录固定取第 1 页 50 条，而后端按最早记录优先排列：达到第 51 条后，新处理记录不会显示，即使刷新也看不到。

**修复：** 使用接口 total/page/page_size 增加分页或加载更多；设备选择支持搜索与分页；处理记录展示最新页或完整分页。当前少量初始化数据掩盖了这个问题。

### R4 — P2：退出状态处理不完整

后端位置：[auth.py:169](/C:/javapractice/new/app/api/routes/auth.py:169)。清除 Cookie 修改的是注入 Response，随后返回了一个新 Response，清 Cookie 响应头丢失。

**证据：** SQLite/ASGI 复现 logout=204、Set-Cookie 为空、客户端仍保留两个 Cookie；再次 /me 为 401，说明服务端撤销确实有效。不能将此描述成“成功退出后还能使用旧会话”。

前端位置：[auth.js:166](/C:/javapractice/new/app/web/auth.js:166)。网络或服务端错误也最终显示“已退出登录”，覆盖错误提示。若退出请求未抵达服务器，实际会话未撤销，刷新可重新恢复登录。

**修复：** 返回真正携带删除 Cookie 的 Response；前端区分本地清屏和服务端确认退出，失败时如实提示。

### R5 — P2：单价校验超出数据库范围，特殊输入会产生 500

位置：[schemas.py:19](/C:/javapractice/new/app/business/schemas.py:19)。

使用 Decimal 系数位数计算范围，未正确计算指数、整数位与两位小数。模型复现：单价 `10000000000` 和 `1E+12` 被接受，前者已超过 DECIMAL(12,2)；`1E+100` 在 quantize 时抛 decimal.InvalidOperation，不是参数校验错误。

**修复：** 按 DECIMAL(12,2) 数值上限 9999999999.99 校验，捕获量化异常并统一返回 422，增加科学计数法和边界测试。

### R6 — P2：重复资料导入会重新开放被管理员停用的文档

位置：[import_support_knowledge.py:159](/C:/javapractice/new/app/cli/import_support_knowledge.py:159)。

管理员停用资料后，再执行一次普通 `--apply` 导入，代码无条件把 existing.enabled 改回 True。原本用于可重入导入的命令因此覆盖了管理员的访问策略。

**证据：** 在独立内存数据库用真实 _report_document 复现 enabled=False → enabled=True，未修改应用的资料开放清单。

**修复：** 重复导入保留现有启停状态。重新开放应由显式的单独动作控制，而非导入的隐含副作用。

### R7 — P2：认证参数校验错误回显提交的密码

位置：[api/errors.py:19](/C:/javapractice/new/app/api/errors.py:19)、[auth/schemas.py](/C:/javapractice/new/app/auth/schemas.py)。

只安装了 BusinessError 处理器，FastAPI 默认 422 会在 detail[].input 中返回密码原值。使用短密码的内存 ASGI 请求已复现。这是向请求方回显其输入，不等同于可以读取其他用户密码，但违反计划中的密码不返回约束，也会扩大错误日志采集时的暴露范围。

**修复：** 对 RequestValidationError 输出做统一脱敏，去掉敏感 input/ctx，不记录认证请求正文。

## 2. 并发问题：代码与当前数据库语义明确，尚未做写库复现

当前数据库只读查询 `@@transaction_isolation` 返回 REPEATABLE-READ。下面必须用完整认证路径补测试，不能把已有“直接调用 service”的并发测试当成覆盖。

1. **设备数量检查可能读取旧快照。** [dependencies.py:49](/C:/javapractice/new/app/auth/dependencies.py:49) 先用共享 session 认证；[devices.py:123](/C:/javapractice/new/app/business/devices.py:123) 随后锁明细，但 [devices.py:141](/C:/javapractice/new/app/business/devices.py:141) 的 COUNT 是普通快照读。两个请求都在首个事务提交前完成认证时，后获锁者仍可能看到旧设备数量，突破购买数量限制。
2. **取消订单与登记设备锁的对象不同。** [orders.py:211](/C:/javapractice/new/app/business/orders.py:211) 锁订单；登记只锁明细并普通读取订单状态。可交错为登记读到 recorded → 另一事务取消订单 → 登记插入设备。应使用共同父订单锁及一致的锁顺序，同时解决快照问题。
3. **同键成功重放可能错误返回 404。** [tickets.py:245](/C:/javapractice/new/app/business/tickets.py:245) 用锁定当前读发现 succeeded，随后普通读取工单详情；若认证已建立旧快照，新创建工单可能仍不可见。去重本身可成立，但“返回原成功结果”不可靠。

修复应从认证与业务事务边界、当前读和一致锁顺序入手，不能只机械增加 FOR UPDATE。完成后在隔离 MySQL 用两个独立 HTTP 请求/事务和可控并发屏障验证。此次没有在应用库新增并发测试数据。

## 3. 功能覆盖情况

| 范围 | 审查结论 |
| --- | --- |
| 注册、登录、会话、CSRF、权限 | 已有真实实现，存在 R1/R4/R7 等问题 |
| 用户所属订单、设备、工单 | 真实服务和数据库已接入，权限过滤存在；并发和分页仍需补齐 |
| 工单状态、处理记录、写入幂等 | 核心实现存在，页面恢复及并发重放有缺陷 |
| 8 个工具与调试入口 | 已注册真实适配器，不是返回固定字典的演示工具；未知写入恢复不足 |
| 开放资料检索 | 真实 Embedding/ES 链路、文档 allowlist 和来源输出存在；导入启停策略有 R6 |
| 管理页面、业务页面 | 已有页面，分页与跨账号状态清理不完整 |
| 初始化与操作说明 | 已有数据文件、CLI、文档与截图；实际样例已入库 |
| 管理端依赖状态展示 | 后端 `/admin/system/status` 已实现，前端没有调用或展示，计划未完整落地 |
| 工具关联审计日志 | 响应带 request_id，但缺少计划要求的完整成功调用日志（用户、操作键、结果、耗时等） |
| 重试故障分类 | `app/tools/base.py:26` 将全部 SQLAlchemyError 转为可重试错误，没有只识别临时故障 |
| Agent 自动选工具、完整对话、任务恢复 | 未实现，符合本次明确排除范围，不算漏做 |

知识检索的新服务与旧管理检索路由存在重复逻辑，未完全落实原计划的共享服务提取；这属于维护差异，未据此判定功能失败。

## 4. 本次验证结果与证据边界

- 独立复跑：**296 passed in 13.37s**。命令为 `python -X utf8 -m pytest --basetemp <本次唯一临时目录> -q -o addopts='' -p no:cacheprovider --ignore=tests/test_business_mysql_integration.py --tb=short`。
- 首次沙箱内运行的 5 项 setup 因 Windows 临时目录权限出错；在获准的沙箱外隔离重跑后全部通过，不归因于业务代码。
- `python -m ruff check app tests tools --no-cache`：通过。
- 未重跑会建表/写入/清理测试数据的 6 项 MySQL 集成测试，也没有执行真实 API/浏览器写入验收脚本或索取/重置现有账号密码。
- 真实数据库只读核对：17 张表，其中十张业务表均存在；4 用户、12 订单、12 明细、18 设备、22 工单、53 处理记录、10 业务操作、7 条资料映射。
- 知识库查询：原知识库 `e2e-retrieval-20261022` 仍存在并有 2 文档；新增「售后开放资料」有 6 文档。只说明当前状态，不能证明原有每条数据从未被修改。
- R1/R2 使用真实 JS 的可控异步/事件执行复现；R4/R7 使用内存 ASGI 复现；R5 使用真实 Pydantic 模型；R6 使用独立内存数据库。

原 Agent 的“302 passed、27 API、22 浏览器”来自其验证记录，本次仅独立确认上述 296 项及专项复现，不能把历史数字改写成本次全部复验通过。

浏览器脚本主要覆盖正常路径：创建一次工单、执行 list_orders、切换账号、刷新；没有模拟提交后丢响应、刷新保留键或未完成请求跨账号返回。所谓“DOM 清空”检查是在清 Cookie 并整页刷新后进行，不能证明同页退出清理可靠。前端契约单测主要检查源码字符串，也不能验证上述行为。因此计划中“重复提交及错误展示”等已勾选项需要重新用针对性行为测试核实。

## 5. 除代码外，还做了哪些操作

### 当前状态与记录能够支持的事项

1. **实际应用了十张新业务表。** 迁移文件只有 CREATE TABLE IF NOT EXISTS；当前库表存在，验证文档也记录了执行。属于计划内工作，不是仅编写了 SQL。
2. **实际创建了账号和关联数据。** admin、alice、bob、carol 均为 active。密码未读取；没有在本次审查重置。
3. **新增知识库并建库。** 实际有「售后开放资料」、6 文档、7 条开放映射；这会涉及对象存储、ES、RabbitMQ 和 Embedding 调用，属于计划内知识链路。
4. **留下了额外验收业务记录。** 对照固定 seed ID，12 张原始样例工单全部存在，另有 10 张工单；原始 12 张的当前 status 与 seed 一致。验证文档明确承认保留验收工单。没有证据把这些记录当作用户真实业务单。
5. **增加了辅助命令和验收脚本。** 密码重置、过期认证数据清理、样例构建、API/浏览器验收是合理配套，但真实验收脚本默认对本地应用写数据。浏览器脚本还会选择第一个 open 工单处理，不强制限定为本次创建的记录，重复验收前应调整目标隔离。
6. **修改了必要的既有链路。** 管理路由增加认证、启动增加业务表校验/RAG 降级、ES 召回增加 document_ids 过滤、页面增加登录门禁。没有扩展支付、Java 服务、完整 Agent 对话或新任务队列。

### 没有足够证据证明的事项

- HEAD 仍是 `fa17c0c`，本轮功能改动未提交；当前 Git 状态未显示 CI、Docker Compose、核心配置文件的跟踪变更。
- `.env` 的当前修改时间仍为 2026-10-22，本次未读取其内容；仅凭 Git 与时间戳不能绝对证明隐藏文件、机器环境、包安装、数据库权限或远端操作完全没有变化。
- 未取得实施 Agent 的完整执行记录和用户批准记录，因此不能断言数据库操作未经授权，也不能宣称所有操作都已获得批准。验证文档是线索，当前数据库是状态证据，两者不是完整操作审计日志。

建议将 R1/R2 作为下一轮修复入口，同时补充完整 HTTP 并发测试。正常路径已经可用于体验；当前不适合作为“已全部验收、可以直接上线”的完成状态。
