from datetime import UTC, datetime, timedelta
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from pypdf import PdfReader, PdfWriter
from test_a1_lifecycle import seed

from app.db import mysql_repository as repo
from app.db.mysql_models import TaskKind
from app.rag.task_processor_service import TaskProcessor


class SessionFactory:
    def __init__(self, database):
        self.db = database

    def __call__(self):
        db = self.db

        class Context:
            async def __aenter__(self):
                return db

            async def __aexit__(self, exc_type, *_args):
                if exc_type:
                    await db.rollback()

        return Context()


class MemoryChunks:
    def __init__(self):
        self.rows = {}
        self.fail_write = 0
        self.writes = 0

    async def write_chunks(self, payloads, *, check_execution=None):
        for key, value in payloads:
            if check_execution:
                await check_execution()
            self.rows[key] = value
            self.writes += 1
            if self.fail_write:
                self.fail_write -= 1
                raise RuntimeError("partial write")

    async def read_task_chunks(self, **filters):
        return [
            (k, dict(v))
            for k, v in self.rows.items()
            if all(v.get(f) == value for f, value in filters.items())
        ]

    async def read_run_chunks(self, **filters):
        return [
            (k, dict(v))
            for k, v in self.rows.items()
            if all(v.get(f) == value for f, value in filters.items())
        ]

    async def refresh(self):
        pass

    async def delete_run_chunks(self, **filters):
        for key, _ in await self.read_run_chunks(**filters):
            self.rows.pop(key)


def processor(db, store=None, content=b"hello world"):
    return TaskProcessor(
        session_factory=SessionFactory(db),
        storage=AsyncMock(get_bytes=AsyncMock(return_value=content)),
        chunk_store=store or MemoryChunks(),
        embedding_client=AsyncMock(
            embed_texts=AsyncMock(side_effect=lambda texts: [[1.0, 2.0] for _ in texts])
        ),
    )


@pytest.mark.asyncio
async def test_txt_normal_flow_uses_snapshot_and_stays_unpublished_until_finalizer(db):
    doc, run, task = await seed(db)
    doc.filename = "a.txt"
    run.config_snapshot = {
        "file_type": "txt",
        "chunker": {"size": 16, "overlap": 0},
        "embedding": {"model": "test", "dimension": 2},
    }
    await db.commit()
    p = processor(db)
    result = await p.process(task.id, dispatch_seq=1)
    assert result.status == "SUCCESS" and result.acknowledged
    assert task.attempt_count == 1 and task.chunk_count > 0
    assert doc.active_run_id is None and run.chunk_count == task.chunk_count
    assert next(iter(p.chunk_store.rows.values()))["kb_id"] == "kb"


@pytest.mark.asyncio
async def test_copy_partial_failure_retries_entire_task_without_embedding(db):
    doc, run, source = await seed(db)
    doc.filename = "a.txt"
    run.config_snapshot = {
        "file_type": "txt",
        "chunker": {"size": 2, "overlap": 0},
        "embedding": {"model": "test", "dimension": 2},
    }
    await db.commit()
    store = MemoryChunks()
    doc.file_hash = run.file_version = (
        __import__("hashlib").sha256(b"one two three four five six").hexdigest()
    )
    await db.commit()
    p = processor(db, store, b"one two three four five six")
    await p.process(source.id, dispatch_seq=1)
    run.status = "FAILED"  # a successful task in an unsuccessful run remains reusable
    await db.commit()
    new_run, tasks = await repo.create_generation(
        db,
        document=doc,
        parse_fingerprint="fp",
        config_snapshot=run.config_snapshot,
        task_ranges=[{"task_no": 1, "core_page_start": 0, "core_page_end": 2}],
    )
    target = tasks[0]
    target.kind, target.source_task_id = TaskKind.COPY, source.id
    await repo.create_outbox_records(db, tasks)
    await db.commit()
    embed_calls = p.embedding_client.embed_texts.await_count
    store.fail_write = 1
    result = await p.process(target.id, dispatch_seq=1)
    assert result.status == "WAITING_PUBLISH" and target.attempt_count == 1
    assert new_run.completed_task_count == 0
    target.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    result = await p.process(target.id, dispatch_seq=2)
    assert result.status == "SUCCESS" and target.attempt_count == 2
    assert p.embedding_client.embed_texts.await_count == embed_calls
    source_rows = await store.read_task_chunks(
        kb_id="kb", doc_id="doc", run_id=run.id, task_id=source.id
    )
    target_rows = await store.read_task_chunks(
        kb_id="kb", doc_id="doc", run_id=new_run.id, task_id=target.id
    )
    assert len(source_rows) == len(target_rows) == target.result_chunk_count
    assert {k for k, _ in source_rows}.isdisjoint(k for k, _ in target_rows)


@pytest.mark.asyncio
async def test_copy_missing_source_exhausts_attempts_and_preserves_active_run(db):
    doc, run, source = await seed(db)
    source.status, source.chunk_count = "SUCCESS", 2
    run.status, doc.active_run_id = "SUCCESS", run.id
    new_run, tasks = await repo.create_generation(
        db,
        document=doc,
        parse_fingerprint="fp",
        config_snapshot=run.config_snapshot,
        task_ranges=[{"task_no": 1, "core_page_start": 0, "core_page_end": 2}],
    )
    task = tasks[0]
    task.kind, task.source_task_id, task.max_attempts = "COPY", source.id, 2
    await repo.create_outbox_records(db, tasks)
    await db.commit()
    p = processor(db)
    assert (await p.process(task.id, dispatch_seq=1)).status == "WAITING_PUBLISH"
    task.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    assert (await p.process(task.id, dispatch_seq=2)).status == "FAILED"
    assert new_run.status == "FAILED" and doc.active_run_id == run.id
    assert source.status == "SUCCESS" and task.attempt_count == 2
    p.embedding_client.embed_texts.assert_not_awaited()


@pytest.mark.asyncio
async def test_pdf_task_sends_only_sub_pdf_and_maps_original_page_numbers():
    from app.rag.chunking_schemas import ParsedPdf, PdfBlock
    from app.rag.document_parser import parse_task_document

    writer = PdfWriter()
    for _ in range(5):
        writer.add_blank_page(width=100, height=100)
    output = BytesIO()
    writer.write(output)
    captured = []

    class Parser:
        def parse_pdf(self, filename, data):
            captured.append(len(PdfReader(BytesIO(data)).pages))
            return ParsedPdf(filename, [PdfBlock("b", 0, "text", 1, "page text", {})], "page text")

    parsed = await parse_task_document(
        filename="a.pdf",
        content=output.getvalue(),
        file_type="pdf",
        range_start=2,
        range_end=4,
        parser=Parser(),
    )
    assert captured == [2] and parsed.blocks[0].page_number == 3
