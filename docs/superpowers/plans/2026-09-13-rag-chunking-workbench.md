# RAG 文本切块实验台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有 RAG Lab 首页改造成一个可真实解析 PDF、展示 MinerU blocks，并能反复调整 token `chunk_size`/`overlap` 观察切块结果的实验台。

**Architecture:** 新增独立的 `app.rag.chunking` 领域模块：MinerU 适配器负责 PDF 到标准化 blocks，token chunker 负责标准化 blocks 到 chunks，路由只编排上传、临时文件生命周期和参数校验。前端保存解析响应，在参数变化时仅调用 preview 接口；不写数据库、不调用 Embedding、不进入 `ragflow`。

**Tech Stack:** FastAPI、Pydantic、`tiktoken` (`cl100k_base`)、本机 MinerU CLI/Python 安装、原生 HTML/CSS/JavaScript、pytest。

---

## 文件结构与职责

- Create: `app/rag/chunking_schemas.py`，标准化 PDF block、解析结果、token chunk 和请求模型。
- Create: `app/rag/token_chunker.py`，使用 `tiktoken` 的 `cl100k_base` 编码执行固定 token 窗口切块，并计算 token/字符/block 元数据。
- Create: `app/rag/mineru_parser.py`，隔离 MinerU 本机调用、输出文件发现和 MinerU 结果映射。
- Create: `app/rag/chunking_service.py`，校验 PDF、临时文件、解析调用和 preview 编排。
- Create: `app/api/routes/chunking.py`，提供 parse、原始 PDF 预览和 preview HTTP 接口。
- Modify: `app/main.py`，注册 chunking router。
- Modify: `app/core/config.py`，增加 MinerU 命令、超时、临时文件生命周期和 PDF 大小配置。
- Modify: `app/web/index.html`，替换为三栏切块实验台页面和请求逻辑。
- Modify: `pyproject.toml`，增加 `tiktoken` 运行依赖；MinerU 作为本机安装依赖记录在 README，避免把重量级模型包强制塞进基础环境。
- Modify: `README.md`，增加 MinerU 本机安装、启动和实验台接口说明。
- Create: `tests/test_token_chunker.py`，覆盖 token 窗口、overlap、短尾块和 block 类型。
- Create: `tests/test_chunking_api.py`，覆盖路由校验、临时 PDF、preview 和 MinerU mock。
- Create: `tests/fixtures/mineru/content_list.json`，保存最小可控的 MinerU content-list fixture。

不读取或修改 `ragflow` 文件夹及其内容。

### Task 1: Add token chunking contracts and failing unit tests

**Files:**
- Create: `app/rag/chunking_schemas.py`
- Create: `app/rag/token_chunker.py`
- Test: `tests/test_token_chunker.py`

- [ ] **Step 1: Write the failing unit tests**

Add tests that use short ASCII text and a mocked/real deterministic `cl100k_base` encoder to assert:

```python
def test_split_respects_token_window_and_overlap():
    result = TokenChunker(chunk_size=4, overlap=1).split(
        [PdfBlock(block_id="block-0000", block_index=0, block_type="text", page_number=1, text="one two three four five six")]
    )
    assert all(item.token_count <= 4 for item in result)
    assert result[0].end_token - result[1].start_token == 1

def test_split_keeps_tail_and_block_types():
    result = TokenChunker(chunk_size=100, overlap=0).split(
        [
            PdfBlock(block_id="block-0000", block_index=0, block_type="title", page_number=1, text="Title"),
            PdfBlock(block_id="block-0001", block_index=1, block_type="table", page_number=1, text="A | B"),
        ]
    )
    assert result[-1].token_count > 0
    assert result[0].block_types == ["title", "table"]

def test_invalid_chunk_configuration_is_rejected():
    with pytest.raises(ValueError):
        TokenChunker(chunk_size=4, overlap=4)
```

The tests must assert `start_char`, `end_char`, `start_token`, `end_token`, `token_count`, and the ordered unique `block_types` are present in the returned dataclass.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_token_chunker.py -q
```

Expected: collection or import failures because the new schemas and chunker do not exist yet.

- [ ] **Step 3: Implement the schemas and deterministic token chunker**

Define frozen dataclasses with these fields:

```python
@dataclass(frozen=True)
class PdfBlock:
    block_id: str
    block_index: int
    block_type: str
    page_number: int | None
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class ParsedPdf:
    filename: str
    blocks: list[PdfBlock]
    parsed_text: str
    warnings: list[str] = field(default_factory=list)

@dataclass(frozen=True)
class TokenChunk:
    chunk_index: int
    text: str
    token_count: int
    start_token: int
    end_token: int
    start_char: int
    end_char: int
    block_types: list[str]
    block_indexes: list[int]
    metadata: dict[str, Any] = field(default_factory=dict)
```

`TokenChunker` must instantiate `tiktoken.get_encoding("cl100k_base")`, reject `chunk_size <= 0` and `overlap < 0 or overlap >= chunk_size`, concatenate non-empty block text with one newline between blocks, encode once, and decode each token window. Build block metadata from token span intersections, preserve first-seen type order, and stop after the final token window. Do not split by characters or delimiter in this class.

- [ ] **Step 4: Add the dependency and run the focused tests**

Add `tiktoken>=0.8,<1.0` to `pyproject.toml`, install the project environment if needed, then run:

```powershell
python -m pytest tests/test_token_chunker.py -q
```

Expected: all token chunker tests pass.

### Task 2: Implement MinerU result normalization

**Files:**
- Create: `app/rag/mineru_parser.py`
- Create: `tests/fixtures/mineru/content_list.json`
- Test: `tests/test_mineru_parser.py`

- [ ] **Step 1: Create a minimal MinerU fixture and failing adapter tests**

The fixture must contain ordered entries representing at least a title, text, table, and image. Tests must assert that `normalize_content_list()` maps them to `PdfBlock` values with stable `block_id`, `block_index`, `block_type`, page number, text, and copied metadata; an image with no text remains visible as an `image` block but contributes empty text.

- [ ] **Step 2: Run the adapter tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_mineru_parser.py -q
```

Expected: import failure for the new adapter functions.

- [ ] **Step 3: Implement normalization independently from process execution**

Implement `normalize_content_list(payload: list[dict[str, Any]]) -> list[PdfBlock]` with an explicit mapping for common MinerU types (`text`, `title`, `table`, `image`, `equation`, `list`) and an `unknown` fallback. Read text from the known content fields in priority order (`text`, `md`, `content`), convert table content to its Markdown/HTML representation, preserve image path/description in metadata, and never use raw user-provided values as filesystem paths.

- [ ] **Step 4: Implement the local MinerU runner**

Create `MinerUParser.parse_pdf(filename, content) -> ParsedPdf` that:

1. Creates a unique temporary directory under the configured temp root.
2. Writes the PDF using a generated safe filename.
3. Invokes the configured executable with an argument list, not `shell=True`; default command is `mineru` and default arguments are `-p <input> -o <output>`.
4. Applies the configured timeout and captures stderr for logs.
5. Searches only inside the generated output directory for `*_content_list.json`, then parses JSON and normalizes it.
6. Raises typed errors for missing executable, timeout, non-zero exit, missing output, malformed JSON, and no usable blocks.
7. Returns `ParsedPdf(filename, blocks, parsed_text, warnings)` while cleanup is performed by the service after the result is built.

Support a configured command path through `MINERU_COMMAND`; never interpolate the uploaded filename into a shell string. Keep the output discovery logic in this adapter so routes do not depend on a particular MinerU version layout.

- [ ] **Step 5: Run the adapter tests with process execution mocked**

Mock `subprocess.run` to create the fixture output in the temporary directory and verify the adapter returns blocks. Add tests for `FileNotFoundError`, timeout, non-zero exit, and missing content-list output. Run:

```powershell
python -m pytest tests/test_mineru_parser.py -q
```

Expected: all normalization and error mapping tests pass without requiring MinerU to be installed.

### Task 3: Add service configuration and chunking API

**Files:**
- Create: `app/rag/chunking_service.py`
- Create: `app/api/routes/chunking.py`
- Modify: `app/core/config.py`
- Modify: `app/main.py`
- Test: `tests/test_chunking_api.py`

- [ ] **Step 1: Write failing API tests**

Add tests using `TestClient` and a dependency-injected fake parser. Cover:

```python
def test_parse_rejects_non_pdf():
    response = client.post("/api/v1/chunking/parse", files={"file": ("note.txt", b"x", "text/plain")})
    assert response.status_code == 400

def test_parse_returns_standardized_blocks_and_preview_url():
    response = client.post("/api/v1/chunking/parse", files={"file": ("manual.pdf", b"%PDF", "application/pdf")})
    assert response.status_code == 200
    body = response.json()
    assert body["file_type"] == "pdf"
    assert body["blocks"][0]["block_type"] == "title"
    assert body["original_url"].startswith("/api/v1/chunking/files/")

def test_preview_rejects_invalid_overlap():
    response = client.post("/api/v1/chunking/preview", json={"blocks": [], "chunk_size": 4, "overlap": 4})
    assert response.status_code == 422
```

Also test that the file GET endpoint returns `application/pdf` for a parsed document and 404 for an unknown id.

- [ ] **Step 2: Run API tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_chunking_api.py -q
```

Expected: 404 for the new routes or import errors before implementation.

- [ ] **Step 3: Add explicit settings**

Extend `Settings` with `mineru_command: str = "mineru"`, `mineru_timeout_seconds: float = 120.0`, `chunking_temp_dir: str | None = None`, `chunking_file_ttl_seconds: int = 3600`, `chunking_max_file_bytes: int = 50 * 1024 * 1024`, `chunking_max_tokens: int = 8192`, and `chunking_default_size: int = 500` / `chunking_default_overlap: int = 80`. Keep names compatible with `.env` uppercase conversion.

- [ ] **Step 4: Implement service models and parse/preview orchestration**

Create Pydantic request models for preview with `chunk_size`, `overlap`, `delimiter: str | None`, and a serialized block payload matching `PdfBlock`; convert the request payload to domain `PdfBlock` dataclasses before chunking. Reject non-empty delimiter, invalid range, and token size over the configured maximum. Store parsed documents in a process-local dictionary keyed by `secrets.token_urlsafe()` with bytes, filename, and expiry; cap the store by removing expired entries before inserts. The service must expose `parse_upload()`, `get_original_file()`, and `preview()` and must not write to the database.

- [ ] **Step 5: Implement and register the router**

Add:

- `POST /api/v1/chunking/parse`: validate extension/content/size, call `MinerUParser`, store bytes, return `document_id`, `original_url`, blocks, parsed text, and warnings.
- `GET /api/v1/chunking/files/{document_id}`: return the stored PDF via `Response` with `media_type="application/pdf"`.
- `POST /api/v1/chunking/preview`: validate request, call `TokenChunker`, return normalized config, `delimiter: null`, `chunk_count`, and serialized chunks.

Map validation to 400/422, missing/expired document to 404, MinerU unavailable to 503, process/output failures to 502, and unexpected errors to 500 without exposing command stderr to clients. Register the router in `app/main.py`.

- [ ] **Step 6: Run the API tests**

Run:

```powershell
python -m pytest tests/test_chunking_api.py -q
```

Expected: all route validation, fake parsing, preview, and original-file tests pass.

### Task 4: Replace the page with the three-column workbench

**Files:**
- Modify: `app/web/index.html`

- [ ] **Step 1: Add the required DOM contract**

Keep the page as one static FastAPI-served HTML file. Add a form with PDF file input, numeric `chunk_size` and `overlap`, a parse button, and a separate preview button. Add three fixed-height scroll panels with ids for original PDF iframe, parsed blocks, and chunks. Add a bottom detail strip with exactly these labels: `chunk_size`, `overlap`, `delimiter`, `当前 chunk token 数`, `block 类型`.

- [ ] **Step 2: Implement parse and preview requests**

On parse submit, build `FormData`, call `POST /api/v1/chunking/parse`, save the response in `parsedDocument`, set the iframe `src` to `original_url`, render blocks, and invoke preview once with current values. On preview submit, require `parsedDocument`, call `POST /api/v1/chunking/preview` with `parsedDocument.blocks` and current values, then render chunks without calling parse. Disable the relevant button during each request and update an `aria-live` status.

- [ ] **Step 3: Render safe, inspectable results**

Use `textContent` for block/chunk text. Each block displays index, `block_type`, page, and text; each chunk displays index, token count, token range, char range, block types, and text. Clicking a chunk stores its index and updates the five bottom values. Preserve the previous valid result if a request fails; show a readable error in status.

- [ ] **Step 4: Verify responsive layout manually**

Run the server and check a desktop and narrow viewport. Confirm the three columns are visible on desktop, stack on narrow screens, each text panel scrolls independently, and long block/chunk text does not overflow or resize controls.

### Task 5: Documentation and integration verification

**Files:**
- Modify: `README.md`
- Test: existing `tests/test_ingestion.py`, `tests/test_ingestion_api.py`, and the new focused tests

- [ ] **Step 1: Document native MinerU installation**

Add a section explaining that Docker is not required, MinerU must be installed in the same Python environment or exposed as the configured `mineru` executable, `tiktoken` is installed by the project, and the server is started with `python -m uvicorn app.main:app --reload --port 8001`. Document the three endpoints and the fixed `cl100k_base` tokenizer.

- [ ] **Step 2: Run the complete test suite**

Run:

```powershell
python -m pytest -q
```

Expected: existing Markdown/TXT ingestion, retrieval, QA, and new chunking tests all pass. If an existing test asserts the old homepage copy, update only that assertion to the new workbench title while preserving API coverage.

- [ ] **Step 3: Run lint and application smoke checks**

Run:

```powershell
python -m ruff check app tests
python -m uvicorn app.main:app --port 8001
```

With the server running, check `GET /health`, `GET /`, a mocked/unit-tested parse path, and `POST /api/v1/chunking/preview` with a small block payload. Stop the server after the smoke check.

- [ ] **Step 4: Verify scope and forbidden directory boundary**

Run:

```powershell
rg --files -g '!ragflow/**' | rg '^(app|tests|docs|README|pyproject)'
```

Confirm every changed path is outside `ragflow`, no database or vector indexing code was added to the new flow, and no shell command uses `shell=True` for MinerU invocation.

## Self-review checklist

- Spec coverage: parse, original PDF preview, normalized blocks, fixed token chunks, parameter repeatability, five bottom fields, error mapping, tests, and native MinerU documentation are covered by Tasks 1-5.
- Plan completeness: no placeholder or unspecified implementation step is required; command names, fields, validation, and expected test behavior are explicit.
- Type consistency: `PdfBlock` is the input to `TokenChunker`, `TokenChunk` is serialized by the preview route, and `ParsedPdf.blocks` is the parse response source for preview.
- Scope: the plan deliberately excludes delimiter logic, multiple tokenizers, database writes, embeddings, retrieval, QA, image OCR, batch uploads, and `ragflow`.
