# RAG 文本切块实验台设计

## 目标

把当前首页改造成一个用于观察 PDF 解析与文本切块的学习工具。用户上传准备好的 PDF 后，点击“解析”应真实调用后端和本机 MinerU；解析完成后，页面并排展示原始文件、解析 block 和切块结果。用户可以反复修改 `chunk_size` 与 `overlap`，只重新执行切块，不重复解析 PDF。

第一版只实现最基础的固定 token 窗口切块，不启用 delimiter 规则，不接入向量库、Embedding、问答或持久化数据库。

## 交互与页面结构

页面从上到下分为三部分：

1. 操作区：选择一个 PDF，输入 `chunk_size` 和 `overlap`，点击解析或重新切块。
2. 三栏观察区：
   - 左栏“文件原文”：显示浏览器可预览的原始 PDF；文件不可预览时显示文件名和下载链接。
   - 中栏“解析内容”：显示 MinerU 返回的 block，按原始顺序排列；每个 block 显示序号、类型、页码和文本内容。表格保留 Markdown 或 HTML 文本，图片显示占位信息和资源路径。
   - 右栏“切块内容”：显示 chunk 序号、token 数、字符区间、覆盖到的 block 类型及文本。点击 chunk 后更新底部详情。
3. 参数详情区：显示当前 chunk 的 `chunk_size`、`overlap`、`delimiter`、当前 chunk token 数和 `block 类型`。没有选中 chunk 时显示整体统计或“请选择一个 chunk”。

界面以可读性为主，三栏在宽屏横向排列，小屏改为纵向排列；长文本区域独立滚动，不能因为内容长度改变页面布局。

## 后端接口

### `POST /api/v1/chunking/parse`

使用 `multipart/form-data` 接收字段 `file`，第一版只接受 `.pdf`。后端校验文件名、大小和内容后，调用 MinerU 本机能力解析文件，并将 MinerU 输出转换成项目自己的标准化结构。

响应结构：

```json
{
  "document_id": "temporary-id",
  "filename": "manual.pdf",
  "file_type": "pdf",
  "original_url": "/api/v1/chunking/files/temporary-id",
  "blocks": [
    {
      "block_id": "block-0000",
      "block_index": 0,
      "block_type": "text",
      "page_number": 1,
      "text": "...",
      "metadata": {}
    }
  ],
  "parsed_text": "...",
  "warnings": []
}
```

`document_id` 只用于本次页面实验，第一版不写数据库。原始文件由后端临时保存或以内存缓存方式提供预览，生命周期至少覆盖当前实验页面的解析和切块请求；超过生命周期后返回明确的文件不可用错误。

### `GET /api/v1/chunking/files/{document_id}`

返回当前实验的原始 PDF 字节流，用于左栏 `<iframe>` 或浏览器内置 PDF 预览。文件不存在或已过期时返回 404。该接口只允许访问服务端为本次解析创建的临时文件，不接受任意路径。

### `POST /api/v1/chunking/preview`

接收解析接口返回的标准化 blocks 以及切块配置：

```json
{
  "blocks": [],
  "chunk_size": 500,
  "overlap": 80,
  "delimiter": null
}
```

后端使用 `tiktoken` 的固定 `cl100k_base` 编码将 block 文本编码成 token 序列，在统一 token 流上按窗口 `[start, start + chunk_size)` 切块，下一块从 `end - overlap` 开始。每个 chunk 返回：

```json
{
  "chunk_index": 0,
  "text": "...",
  "token_count": 500,
  "start_token": 0,
  "end_token": 500,
  "start_char": 0,
  "end_char": 1960,
  "block_types": ["text", "table"],
  "block_indexes": [0, 1],
  "metadata": {}
}
```

第一版规定：`delimiter` 只能为 `null` 或空字符串，服务端拒绝未知 delimiter；`overlap` 必须满足 `0 <= overlap < chunk_size`；`chunk_size` 使用正整数，并设置合理上限避免一次请求占用过多内存。空 block 不参与切块，但保留在解析结果中供观察。

## 解析适配层

新增 MinerU 适配器，业务层只依赖一个稳定接口，例如 `parse_pdf(filename, content) -> ParsedPdf`。适配器负责：

- 将上传内容写入受控临时目录；
- 调用本机安装的 MinerU 命令或 Python API；
- 查找 MinerU 产出的结构化结果；
- 映射为 `text`、`title`、`table`、`image` 等稳定的 `block_type`；
- 合并可参与切块的文本，并保留页码、原始索引和 MinerU 元数据；
- 在 MinerU 未安装、命令失败、输出缺失或结果无法解析时返回可读的 HTTP 错误。

具体 MinerU 调用方式放在适配器内部，不泄漏到路由和前端。实现时优先使用当前本机安装版本支持的官方入口；如果该版本只提供 CLI，则通过受控子进程调用，命令和超时从配置读取，不能拼接用户输入形成 shell 命令。

## 数据流

```text
选择 PDF
  -> POST /chunking/parse
  -> MinerU
  -> 标准化 blocks + 临时 document_id
  -> 页面保存解析结果
  -> POST /chunking/preview(chunk_size, overlap)
  -> tiktoken token 窗口切块
  -> 页面刷新右栏和底部详情
```

切块请求不重新读取 PDF，也不重新调用 MinerU。前端参数变化时采用显式“重新切块”按钮，避免用户输入未完成时频繁请求；请求期间禁用相关按钮并展示状态。

## 错误与边界

- 非 PDF、空文件、超过大小限制：返回 400。
- MinerU 未安装或未配置：返回 503，并提示本机安装/配置问题，不伪装成解析成功。
- MinerU 运行超时或输出损坏：返回 502，保留后端日志中的原始原因，前端只显示可理解的摘要。
- `overlap >= chunk_size`、负数或超出上限：返回 422。
- 解析成功但没有可切块文本：返回 422，并仍允许页面查看原始 block。
- 前端在没有解析结果时不能调用 preview；解析结果过期时提示重新解析。

## 测试与验收

后端测试覆盖：

- MinerU 适配器可以将 fixture 解析结果映射为标准化 blocks；
- parse 路由拒绝非 PDF，并正确传播 MinerU 缺失/失败错误；
- token chunker 验证窗口长度、overlap、最后一个短 chunk 和 block 类型映射；
- preview 路由拒绝非法参数，并能用固定 tokenizer 生成可重复结果。

前端验收：

- 选择 PDF 后点击解析确实发起请求；
- 左、中、右三栏分别出现文件预览、解析 blocks 和 chunks；
- 修改 `chunk_size`/`overlap` 后重新切块不会再次触发 MinerU；
- 点击不同 chunk，底部五项信息同步变化；
- API 失败时显示错误且不会清空上一次有效结果。

## 不在本次范围内

- delimiter 切块策略；
- 多种 tokenizer 对比；
- 数据库存储、向量化、检索和问答；
- MinerU 图片文件的视觉识别；
- 多文件批量上传和异步任务队列。
