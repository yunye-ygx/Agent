# Model Provider Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add local Zhipu configuration and verification, plus a safe frontend configuration panel for Zhipu and GPT-compatible providers.

**Architecture:** The backend reads provider settings from environment variables and exposes masked read-only status plus connection tests. The frontend never receives a full API key; it displays provider cards, capability status, and test results. GPT is configuration-ready but is only tested when its own key is configured.

**Tech Stack:** FastAPI, Pydantic Settings, httpx, vanilla HTML/CSS/JavaScript, pytest.

---

### Task 1: Configure and verify Zhipu

**Files:**
- Modify: `.env`
- Modify: `.env.example`
- Modify: `app/core/config.py`
- Modify: `app/rag/embedding.py`
- Test: `tests/test_embedding.py`

- [ ] Add Zhipu defaults for chat and embedding model names and preserve the API key only in local environment configuration.
- [ ] Ensure Embedding accepts the configured model and validates returned vector dimensions.
- [ ] Run a real `/embeddings` request without printing the key.

### Task 2: Add provider configuration API

**Files:**
- Create: `app/api/routes/model_providers.py`
- Modify: `app/api/schemas_v2.py`
- Modify: `app/main.py`
- Test: `tests/test_model_providers.py`

- [ ] Expose provider metadata with masked key, configured model names, base URL, and capabilities.
- [ ] Add provider-specific test endpoints that return success or a safe error message without credentials.
- [ ] Keep GPT as an OpenAI-compatible provider and skip its live test when no key exists.

### Task 3: Add frontend model settings view

**Files:**
- Modify: `app/web/index.html`
- Modify: `app/web/app.js`
- Modify: `app/web/app.css`
- Modify: `tests/test_frontend_contract.py`

- [ ] Add a top-level “模型配置” navigation view.
- [ ] Add Zhipu and GPT provider cards with masked credential status, model fields, capability tags, and test buttons.
- [ ] Add responsive and accessible loading, success, failure, and unconfigured states.

### Task 4: Verify

- [ ] Run `python -m ruff check app tests`.
- [ ] Run `python -m pytest -q`.
- [ ] Run frontend JavaScript syntax validation.
- [ ] Run a live Zhipu Embedding test and report only status, model, dimension, and latency.
