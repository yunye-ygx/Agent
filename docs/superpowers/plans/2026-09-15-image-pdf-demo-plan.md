# Image PDF Parsing Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline execution in the current session). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing PDF chunking workbench so a single generated PDF containing a pure image, an image with embedded text, and a table image can be parsed through the existing MinerU flow and inspected with the current token chunk preview.

**Architecture:** Keep MinerU as the PDF parser and keep the existing `PdfBlock`/`TokenChunk` contracts. Enrich image blocks with normalized image metadata and expose image-related information in the API and UI; preserve any text MinerU/OCR emits as block text so the current TokenChunker can display the actual token effect. Add a reproducible local sample-PDF generator and a checked-in generated sample asset, without introducing Docker, PostgreSQL, embeddings, or a second production OCR pipeline.

**Tech Stack:** FastAPI, existing MinerU CLI adapter, `tiktoken` `cl100k_base`, native HTML/JavaScript, Pillow, ReportLab, pytest.

---

## Task 1: Define the image-PDF observable behavior with tests

**Files:**
- Modify: `tests/test_mineru_parser.py`
- Modify: `tests/test_chunking_api.py`
- Modify: `tests/test_token_chunker.py`

- [x] Add failing tests that require image metadata normalization: `img_path`, `img_caption`, `image_kind`, and `ocr_text` remain available in a normalized image block, while image blocks with no text stay visible but do not enter token chunks.
- [x] Add a failing preview test proving that a text-bearing image block contributes its OCR/text payload to token chunks and that a pure image block remains excluded from `TokenChunker` text windows.
- [x] Run the focused tests and confirm they fail for the new expected fields/behavior rather than because of a test typo.

## Task 2: Enrich MinerU image normalization

**Files:**
- Modify: `app/rag/mineru_parser.py`
- Modify: `app/rag/chunking_schemas.py`

- [x] Normalize image fields without changing the stable `PdfBlock` interface: preserve the original MinerU item in `metadata`, copy `img_path`, normalize `img_caption` lists into readable text for metadata, copy OCR-like fields (`ocr_text`, `image_text`, `ocr`) when present, and use text/md/content as the block text when MinerU emits it.
- [x] Add image metadata helpers that classify an image as `pure_image`, `text_image`, `table_image`, or `unknown` from explicit metadata first and filename/caption hints second; never discard the original metadata when classification is uncertain.
- [x] Keep `parsed_text` limited to blocks with non-empty text, so a pure image is observable in parsed blocks but does not create an empty token window.
- [x] Run the focused parser tests and the existing parser suite.

## Task 3: Add a reproducible three-case PDF sample

**Files:**
- Create: `tools/generate_image_pdf_sample.py`
- Create: `samples/image-pdf-chunking-demo.pdf`
- Create: `samples/assets/pure-architecture.png`
- Create: `samples/assets/text-diagram.png`
- Create: `samples/assets/table-image.png`
- Modify: `.gitignore` only if generated cache files require it

- [x] Write a generator using Pillow and ReportLab that creates one PDF with three pages: a pure image with no readable text, a raster image containing a labeled RAG flow diagram, and a raster image containing a database-style table with headers and rows.
- [x] Use built-in drawing primitives and ASCII/Latin labels so the sample is deterministic and does not depend on system CJK fonts or external downloads.
- [x] Generate the PDF and inspect its page count and byte size with a read-only PDF library check; do not start Docker/PostgreSQL.
- [x] Keep the generator so the sample can be regenerated rather than relying on an opaque binary-only artifact.

## Task 4: Show image/OCR metadata in the API and frontend

**Files:**
- Modify: `app/web/index.html`
- Modify: `app/rag/chunking_service.py` only if response shaping needs a focused helper

- [x] Render image blocks with a visible metadata section containing image kind, `img_path`, Caption, OCR/text availability, and page number; keep pure image blocks visible even though they are not tokenized.
- [x] Render chunk metadata with the existing token counts plus a clear note when a chunk contains OCR/text from an image block.
- [x] Add a short UI hint that the sample PDF contains three image cases and that the current comparison is the existing basic Token strategy.
- [x] Preserve the current re-chunk behavior: changing `chunk_size`/`overlap` calls preview only and does not re-run MinerU.

## Task 5: Verify the complete demo path

**Files:**
- Modify: `README.md`
- Test: all existing tests plus the new image-PDF tests

- [x] Document how to generate the sample PDF, upload it in the workbench, and compare Token windows; explicitly state that `img_path`/Caption alone are not OCR and that actual OCR availability depends on MinerU output.
- [x] Run focused tests, then `python -m pytest -q`.
- [x] Run `python -m ruff check app tests tools` if Ruff is installed.
- [x] Run a FastAPI smoke check for `/health`, `/`, and preview using a controlled test client; only run real MinerU against the generated PDF if the local command is available, and report its actual result.
- [x] Verify changed files are outside `C:\javapractice\new\ragflow` and do not start Docker/PostgreSQL.
