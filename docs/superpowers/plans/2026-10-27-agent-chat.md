# 真实会话与 LangChain 流式聊天实施计划

> 使用 superpowers:executing-plans 在当前工作区逐项执行。用户已确认需求并要求先提交现有改动；基线为 bdb23e8。不创建 worktree、不改环境配置、不推送远程。

**Goal:** 登录用户在聊天页创建真实会话，通过已有业务工具查询真实订单，刷新后可查看消息并继续对话。

**Architecture:** FastAPI 验证 Cookie、CSRF、会话归属，MySQL 保存会话和有序消息；LangChain 1.x create_agent 负责模型/工具循环，既有 ToolRuntime 继续负责工具参数、权限、超时和重试。POST fetch 消费 SSE；不启用任务队列、checkpointer 或中断续跑。

**Tech Stack:** Python、FastAPI、SQLAlchemy/MySQL、LangChain 1.x、原生 JavaScript。

## 1. 基线与依赖
- [x] 检查未提交文件、敏感信息模式及 diff，提交现有工作。
- [ ] 固定兼容 LangChain 1.x 依赖；旧演示使用 langchain-classic 保留旧 API；在项目虚拟环境验证，不升级全局 Python。

## 2. 数据与归属
- [ ] 新增 app/chat/models.py、store.py 和 migrations/20261027_chat.sql。
- [ ] chat_conversations 保存 UUID、用户外键、标题、时间、当前请求占用标记；chat_messages 保存 UUID、会话外键、顺序、轮次、role、文本、tool_calls/tool_call_id、轮次完成状态。
- [ ] 所有访问限定 user_id；数据库条件更新拒绝同会话同时发送；消息排序由数据库唯一约束保障。
- [ ] 每条完整消息立即入库；整轮成功后才进入后续模型历史。失败轮保留展示但不把不完整工具协议传给下一轮。
- [ ] 编写 tests/test_chat_store.py：跨用户不可读、消息次序、并发占用、完整轮与失败轮历史筛选。

## 3. 框架及 HTTP
- [ ] app/chat/agent.py 从默认模型配置创建 ChatOpenAI；模型超时、禁止 SDK 隐式重试、循环次数上限；只读工具复用现有 Runtime（首版不开放创建工单）。
- [ ] app/chat/service.py 保存 user / assistant tool-call / tool result / assistant answer；SSE delta、message、done、error。done 只在持久化成功后发送；异常信息不泄漏密钥或底层响应。
- [ ] app/api/routes/chat.py：POST/GET conversations，GET messages（分页），POST conversations/{id}/messages（流式）；请求体禁止 user_id 和客户端历史。
- [ ] tests/test_chat_api.py、tests/test_chat_agent.py 用可控模型传输测试真实框架和 Runtime 往返，检查权限、工具错误和消息记录；运行时无假模型后备。

## 4. 页面
- [ ] app/web/chat.js、chat.css，替换 index.html 壳子和 app.js 占位处理；客户/管理员均可进入。
- [ ] 会话列表、新会话、历史分页、输入、流式文本、工具结果折叠展示；失败明确显示，断流不标为完成。
- [ ] 身份/会话切换使旧请求响应失效，退出清空 DOM；模型输出仅 textContent，避免 HTML 注入。
- [ ] tests/chat_frontend.test.cjs：SSE 拆包、跨账号迟到响应、错误与 done 语义。

## 5. 验证与交付
- [ ] python -m pytest tests/test_chat_store.py tests/test_chat_agent.py tests/test_chat_api.py；node --test tests/chat_frontend.test.cjs。
- [ ] 全量非外部写入测试、Ruff、前端语法检查。
- [ ] 迁移只新增已获需求授权的两张表，校验外键/索引；不修改现有数据。真实模型冒烟仅使用专用新会话，保留验证结果。
- [ ] docs/agent-chat-guide.md 记录启动、接口、代码阅读入口、验证证据和明确限制。

## 边界
聊天历史不是任务恢复。进程硬中断后的占用释放/断点恢复后续再做；本版未完成轮不自动续跑，必要时新建会话。没有模型配置或依赖时返回明确错误，不生成演示答案。新增 SQL 文件可审阅；不擅自修改已有表。创建工单仍通过现有业务页/工具实验室，聊天首版接入只读工具。
