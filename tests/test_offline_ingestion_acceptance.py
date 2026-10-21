"""A1 fault injection; real SQLAlchemy transactions, isolated external adapters.

Replaces the former shared-membership/heartbeat-only acceptance contract.
Scenario mapping and limitations are recorded in docs/a1-alignment.md.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_a1_lifecycle import seed
from test_a1_pipeline import SessionFactory, processor

from app.api.routes import documents_v2
from app.core.config import settings
from app.db import mysql_repository as repo
from app.db.mysql_models import Generation, Task
from app.rag.embedding import EmbeddingError
from app.rag.maintenance import MaintenanceService
from app.rag.mineru_parser import MinerUExecutionError


async def text_task(db, *, content=b"hello world"):
    doc, run, task = await seed(db)
    doc.filename = "a.txt"
    run.config_snapshot = {
        "file_type": "txt",
        "chunker": {"size": 2, "overlap": 0},
        "embedding": {"model": "test", "dimension": 2},
    }
    import hashlib

    doc.file_hash = run.file_version = hashlib.sha256(content).hexdigest()
    await db.commit()
    return doc, run, task, processor(db, content=content)


@pytest.mark.asyncio
async def test_pdf_creation_uses_half_open_ranges_and_persists_all_outbox_tasks(db):
    doc, run, _task = await seed(db, pages=25)
    run.status = "CANCELLED"
    await db.commit()
    with patch.object(settings, "task_page_size", 10):
        result = await documents_v2._create_generation(doc.id, db, force_new=True)
    tasks = (
        (
            await db.execute(
                select(Task).where(Task.generation_id == result["run_id"]).order_by(Task.task_no)
            )
        )
        .scalars()
        .all()
    )
    assert [(t.core_page_start, t.core_page_end) for t in tasks] == [(0, 10), (10, 20), (20, 25)]
    assert all(t.status == "WAITING_PUBLISH" and t.attempt_count == 0 for t in tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [MinerUExecutionError("parse unavailable"), EmbeddingError("embedding unavailable")]
)
async def test_parse_dependency_failure_saves_retry_before_ack(db, failure):
    _doc, run, task, p = await text_task(db)
    p._parse_payloads = AsyncMock(side_effect=failure)
    result = await p.process(task.id, dispatch_seq=1)
    assert result.acknowledged and result.status == "WAITING_PUBLISH"
    assert task.attempt_count == 1 and task.dispatch_seq == 2
    assert run.completed_task_count == 0 and run.chunk_count == 0


@pytest.mark.asyncio
async def test_partial_es_parse_write_converges_without_duplicate_records(db):
    content = b"one two three four five six seven eight"
    _doc, run, task, p = await text_task(db, content=content)
    p.chunk_store.fail_write = 1
    assert (await p.process(task.id, dispatch_seq=1)).status == "WAITING_PUBLISH"
    partial_ids = set(p.chunk_store.rows)
    task.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    assert (await p.process(task.id, dispatch_seq=2)).status == "SUCCESS"
    assert partial_ids <= set(p.chunk_store.rows)
    assert len(p.chunk_store.rows) == task.result_chunk_count == run.chunk_count
    assert run.completed_task_count == 1


@pytest.mark.asyncio
async def test_es_complete_but_db_commit_failed_is_safe_to_retry(db):
    _doc, run, task, p = await text_task(db)
    with patch.object(
        repo, "complete_task", AsyncMock(side_effect=RuntimeError("DB completion failure"))
    ):
        result = await p.process(task.id, dispatch_seq=1)
    assert result.status == "WAITING_PUBLISH" and run.chunk_count == 0
    ids = set(p.chunk_store.rows)
    assert ids
    task.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    assert (await p.process(task.id, dispatch_seq=2)).status == "SUCCESS"
    assert set(p.chunk_store.rows) == ids and run.completed_task_count == 1


@pytest.mark.asyncio
async def test_task_success_and_statistics_rollback_together(db):
    _doc, run, task = await seed(db)
    await repo.claim_task(db, task.id, dispatch_seq=1)
    await db.commit()
    task_id, run_id, token = task.id, run.id, task.attempt_id
    real_flush = db.flush

    async def failed_flush():
        await real_flush()
        raise RuntimeError("failure after SQL updates, before commit")

    with patch.object(db, "flush", failed_flush), pytest.raises(RuntimeError):
        await repo.complete_task(db, task_id=task_id, attempt_id=token, chunk_count=3)
    await db.rollback()
    restored = await repo.get_task(db, task_id)
    restored_run = await repo.get_generation(db, run_id)
    assert restored.status == "RUNNING" and restored_run.chunk_count == 0
    assert restored_run.completed_task_count == 0


@pytest.mark.asyncio
async def test_redelivery_after_success_before_ack_does_not_repeat_business(db):
    _doc, run, task, p = await text_task(db)
    await p.process(task.id, dispatch_seq=1)
    calls, writes = p.embedding_client.embed_texts.await_count, p.chunk_store.writes
    duplicate = await p.process(task.id, dispatch_seq=1)
    assert duplicate.acknowledged and duplicate.status == "SUCCESS"
    assert p.embedding_client.embed_texts.await_count == calls and p.chunk_store.writes == writes
    assert task.attempt_count == run.completed_task_count == 1


@pytest.mark.asyncio
async def test_cancel_during_embedding_stops_before_es_write(db):
    doc, run, task, p = await text_task(db)

    async def cancel(texts):
        await repo.request_generation_cancel(db, run.id)
        await db.commit()
        return [[1.0, 2.0] for _ in texts]

    p.embedding_client.embed_texts.side_effect = cancel
    result = await p.process(task.id, dispatch_seq=1)
    assert result.status == "CANCELLING" and not p.chunk_store.rows
    await repo.finish_cancellations(db)
    assert task.status == "CANCELLED" and run.status == "CANCELLED"
    assert doc.current_run_id == run.id and doc.active_run_id is None


@pytest.mark.asyncio
async def test_old_dispatch_notification_cannot_start_retry_early(db):
    _doc, _run, task, p = await text_task(db)
    p.chunk_store.fail_write = 1
    await p.process(task.id, dispatch_seq=1)
    result = await p.process(task.id, dispatch_seq=1)
    assert result.acknowledged and task.attempt_count == 1
    assert task.dispatch_seq == 2 and task.status == "WAITING_PUBLISH"


@pytest.mark.asyncio
async def test_final_failure_revokes_remaining_task_work(db):
    doc, run, task = await seed(db, max_attempts=1)
    sibling = Task(
        generation_id=run.id, document_id=doc.id, task_no=2, core_page_start=2, core_page_end=3
    )
    db.add(sibling)
    run.expected_task_count = 2
    await db.flush()
    await repo.create_outbox_records(db, [sibling])
    await db.commit()
    await repo.claim_task(db, task.id, dispatch_seq=1)
    await repo.fail_task(
        db, task_id=task.id, attempt_id=task.attempt_id, code="FAILED", message="fatal"
    )
    assert task.status == "FAILED" and run.status == "FAILED" and sibling.status == "CANCELLED"
    assert await repo.claim_task(db, sibling.id, dispatch_seq=1) is None


@pytest.mark.asyncio
async def test_missing_es_artifacts_prevent_publication(db):
    doc, run, task, p = await text_task(db)
    await p.process(task.id, dispatch_seq=1)
    p.chunk_store.rows.clear()
    service = MaintenanceService(session_factory=SessionFactory(db), chunk_store=p.chunk_store)
    assert await service.publish_ready_runs() == 0
    assert run.status == "RUNNING" and doc.active_run_id is None


@pytest.mark.asyncio
async def test_changed_parser_output_on_retry_is_detected_before_more_es_writes(db):
    _doc, _run, task, p = await text_task(db)
    p.chunk_store.fail_write = 1
    await p.process(task.id, dispatch_seq=1)
    before = dict(p.chunk_store.rows)
    task.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    original = p._parse_payloads

    async def changed(*args):
        rows = await original(*args)
        rows[0][1]["content"] = "unexpected nondeterministic text"
        return rows

    p._parse_payloads = changed
    assert (await p.process(task.id, dispatch_seq=2)).status == "WAITING_PUBLISH"
    assert p.chunk_store.rows == before
    assert "chunk" in task.last_error_message


@pytest.mark.asyncio
async def test_heartbeat_exception_revokes_local_trust(db):
    _doc, _run, task, p = await text_task(db)
    lost = asyncio.Event()
    with (
        patch.object(
            repo, "touch_task_heartbeat", AsyncMock(side_effect=RuntimeError("DB unavailable"))
        ),
        patch.object(settings, "task_heartbeat_interval_seconds", 0.001),
    ):
        await asyncio.wait_for(p._heartbeat_loop(task.id, "attempt", lost), 1)
    assert lost.is_set()


@pytest.mark.asyncio
async def test_kb_rejects_model_switch_without_migration(db):
    doc, run, _task = await seed(db)
    kb = await repo.get_knowledge_base(db, "kb")
    kb.embedding_model, kb.embedding_dimension = "fixed", 2
    run.status = "CANCELLED"
    await db.commit()
    with pytest.raises(HTTPException) as error:
        await documents_v2._create_generation(doc.id, db, force_new=True)
    assert error.value.status_code == 409
    assert len((await db.execute(select(Generation))).scalars().all()) == 1
