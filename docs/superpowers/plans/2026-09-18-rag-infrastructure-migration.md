# RAG Infrastructure Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the old PostgreSQL/pgvector demo with a knowledge-base pipeline backed by MySQL, MinIO, RabbitMQ, and Elasticsearch, including asynchronous parsing, versioned generations, chunk reuse, cancellation, retrieval, and the two requested frontend screens.

**Architecture:** MySQL is the source of truth for knowledge bases, documents, generations, tasks, task-to-chunk references, and dispatch outbox records. MinIO stores source and parser artifacts; RabbitMQ transports idempotent task messages; Elasticsearch stores only physical chunks with text, vectors, source metadata, and shared `generation_ids`.

**Tech Stack:** FastAPI, SQLAlchemy async + asyncmy, MySQL 8, MinIO, RabbitMQ/aio-pika, Elasticsearch async client, existing MinerU/chunking/embedding clients, vanilla HTML/CSS/JavaScript.

---

### Task 1: Replace runtime configuration and local infrastructure

**Files:**
- Modify: `C:\javapractice\new\docker-compose.yml`
- Modify: `C:\javapractice\new\.env.example`
- Modify: `C:\javapractice\new\pyproject.toml`
- Create: `C:\javapractice\new\docker\mysql\init.sql`
- Create: `C:\javapractice\new\app\core\config.py` settings for MySQL, ES, MinIO, RabbitMQ
- Test: `C:\javapractice\new\tests\test_config.py`

- [ ] **Step 1: Write configuration tests** for database URL, ES URL, MinIO endpoint, RabbitMQ URL, bucket names, and embedding dimension defaults.
- [ ] **Step 2: Run `python -m pytest tests/test_config.py -q` and confirm it fails because the new settings do not exist.**
- [ ] **Step 3: Implement the new settings and remove PostgreSQL/pgvector settings and dependencies.**
- [ ] **Step 4: Add MySQL, Elasticsearch, MinIO, RabbitMQ services with persistent volumes and health checks to Compose.**
- [ ] **Step 5: Add MySQL UTF-8 initialization SQL and default service credentials for local development.**
- [ ] **Step 6: Run the configuration test and `docker compose config`; record external Docker availability separately if Docker is unavailable.**

### Task 2: Create MySQL schema and repositories

**Files:**
- Create: `C:\javapractice\new\app\db\mysql_models.py`
- Create: `C:\javapractice\new\app\db\mysql_session.py`
- Create: `C:\javapractice\new\app\db\mysql_schema.py`
- Create: `C:\javapractice\new\app\db\mysql_repository.py`
- Create: `C:\javapractice\new\tests\test_mysql_domain.py`
- Remove or replace: old PostgreSQL-only `app/db/models.py`, `app/db/session.py`, `app/db/schema.py`, `app/db/repository.py`

- [ ] **Step 1: Write unit tests for UUID entities, Generation status transitions, single-building-generation enforcement, and Task terminal-state idempotency.**
- [ ] **Step 2: Run the focused tests and confirm they fail before implementing the models.**
- [ ] **Step 3: Implement `KnowledgeBase`, `Document`, `Generation`, `Task`, `TaskChunkRef`, and `TaskDispatchOutbox` with the schema from `docs/offline-rag-infrastructure-design.md`.**
- [ ] **Step 4: Implement async session management using `mysql+asyncmy`.**
- [ ] **Step 5: Implement repository methods with row locking for create generation, cancel generation, claim task, complete task, fail task, and update generation counters.**
- [ ] **Step 6: Implement the MySQL DDL/init path and run the focused tests.**

### Task 3: Implement MinIO, Elasticsearch, and RabbitMQ adapters

**Files:**
- Create: `C:\javapractice\new\app\storage\minio_storage.py`
- Create: `C:\javapractice\new\app\storage\elasticsearch_chunks.py`
- Create: `C:\javapractice\new\app\messaging\rabbitmq.py`
- Create: `C:\javapractice\new\app\messaging\outbox_publisher.py`
- Create: `C:\javapractice\new\app\storage\bootstrap.py`
- Create: `C:\javapractice\new\tests\test_chunk_identity.py`
- Create: `C:\javapractice\new\tests\test_messaging_payload.py`

- [ ] **Step 1: Write tests for deterministic Chunk IDs, generation membership updates, idempotent task messages, and ACK decision rules.**
- [ ] **Step 2: Run focused tests and confirm the expected failures.**
- [ ] **Step 3: Implement MinIO upload/download/stream/presigned URL operations using object keys from the design.**
- [ ] **Step 4: Implement ES index creation, strict mapping, bulk chunk indexing, vector search, generation filtering, atomic generation membership updates, and safe cleanup.**
- [ ] **Step 5: Implement RabbitMQ exchange/queue/DLQ declaration, message serialization, publish, ACK/NACK helpers, and outbox publishing.**
- [ ] **Step 6: Run adapter tests without requiring live services, then add live health checks when Compose is available.**

### Task 4: Implement parse fingerprint, chunk production, and Worker

**Files:**
- Create: `C:\javapractice\new\app\rag/fingerprint.py`
- Create: `C:\javapractice\new\app\rag\task_processor.py`
- Create: `C:\javapractice\new\app\worker.py`
- Modify: reusable MinerU/chunking/embedding modules as required
- Create: `C:\javapractice\new\tests\test_task_processor.py`

- [ ] **Step 1: Write tests for fingerprint matching, same-fingerprint reuse, changed-fingerprint execution, partial ES writes, and cancellation checkpoints.**
- [ ] **Step 2: Run the focused tests and confirm they fail before implementation.**
- [ ] **Step 3: Implement the fingerprint from file hash, parser/chunker versions and configs, embedding model/dimension, page range, and context-page rule.**
- [ ] **Step 4: Implement the executed-task path: MinIO download, MinerU parse, chunk, embedding, deterministic ES write, MySQL reference write, and Task SUCCESS persistence.**
- [ ] **Step 5: Implement the reused-task path: create MySQL references and atomically add the Generation ID to existing ES Chunks without duplicating vectors.**
- [ ] **Step 6: Implement business retry and message retry separation, including ACK only after reliable MySQL terminal/intermediate status persistence.**
- [ ] **Step 7: Implement the standalone Worker entrypoint and run focused tests.**

### Task 5: Implement KnowledgeBase, Document, Generation, Task, Chunk, and retrieval APIs

**Files:**
- Create: `C:\javapractice\new\app\api\routes\knowledge_bases.py`
- Create: `C:\javapractice\new\app\api\routes\documents_v2.py`
- Create: `C:\javapractice\new\app\api\routes\retrieval.py`
- Create: `C:\javapractice\new\app\api\schemas_v2.py`
- Modify: `C:\javapractice\new\app\main.py`
- Create: `C:\javapractice\new\tests\test_knowledge_base_api.py`

- [ ] **Step 1: Write API tests for create/list knowledge base, upload, start parse, status, reparse, cancel, document file, chunks, and current-generation retrieval.**
- [ ] **Step 2: Run API tests and confirm they fail before implementation.**
- [ ] **Step 3: Implement knowledge-base and document upload endpoints backed by MinIO and MySQL.**
- [ ] **Step 4: Implement first parse and reparse transaction boundaries, including the single-building-generation rule and Outbox records.**
- [ ] **Step 5: Implement cancellation as `CANCELING` first, then terminal `CANCELED` only after Tasks reach terminal states.**
- [ ] **Step 6: Implement document status, generation/task status, Chunk listing, original-file streaming, and ES retrieval filtered by MySQL current Generation.**
- [ ] **Step 7: Run API tests with dependency overrides/fakes and then live-service tests when available.**

### Task 6: Replace the frontend with the knowledge-base and chat shell

**Files:**
- Replace: `C:\javapractice\new\app\web\index.html`
- Create: `C:\javapractice\new\app\web\app.css`
- Create: `C:\javapractice\new\app\web\app.js`
- Modify: `C:\javapractice\new\app\main.py` route for document detail/static assets
- Create: `C:\javapractice\new\tests\test_frontend_contract.py`

- [ ] **Step 1: Write frontend contract tests for required labels, API paths, document detail layout, and chat shell notice.**
- [ ] **Step 2: Run the contract tests and confirm they fail against the old workbench.**
- [ ] **Step 3: Implement a responsive gray-white knowledge-base management screen with empty/loading/error states and accessible controls.**
- [ ] **Step 4: Implement document detail with PDF preview on the left and Chunk list/details on the right.**
- [ ] **Step 5: Implement the chat shell with a clear not-connected state and no fake retrieval response.**
- [ ] **Step 6: Run frontend contract tests and verify the main flows with the API test client.**

### Task 7: Remove old PostgreSQL flow and finish documentation

**Files:**
- Remove or rewrite: old PostgreSQL API, QA, Agent, evaluation routes and repositories
- Modify: `C:\javapractice\new\README.md`
- Modify: `C:\javapractice\new\.env.example`
- Modify: `C:\javapractice\new\docs\offline-rag-infrastructure-design.md` only if implementation exposes a concrete inconsistency
- Create: `C:\javapractice\new\tests\test_health.py` replacement if needed

- [ ] **Step 1: Search the repository for PostgreSQL, pgvector, asyncpg, and old `create_schema` references.**
- [ ] **Step 2: Remove obsolete imports/routes/tests and update README to the new startup and Worker commands.**
- [ ] **Step 3: Run the complete test suite.**
- [ ] **Step 4: Run `docker compose config`, start available services, initialize mappings/buckets/queues, and run a smoke test.**
- [ ] **Step 5: Report exact passing tests and any environment-only blockers.**

## Self-review checklist

- [ ] No production code still depends on PostgreSQL or pgvector.
- [ ] MySQL is the source of truth for all business statuses.
- [ ] Elasticsearch contains only Chunk documents and supports `generation_ids` filtering.
- [ ] A new Generation never replaces `current_generation_id` before complete success.
- [ ] Reused Chunks are not duplicated and are not deleted while referenced by another Generation.
- [ ] RabbitMQ ACK follows durable status persistence.
- [ ] Cancellation is terminal only after Task termination.
- [ ] The frontend exposes knowledge-base management and document Chunk inspection.
- [ ] Tests cover first parse, reparse, reuse, cancellation, retry, and current-generation retrieval.
