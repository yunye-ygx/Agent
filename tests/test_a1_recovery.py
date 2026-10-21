from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_a1_lifecycle import seed
from test_a1_pipeline import MemoryChunks, SessionFactory, processor

from app.db import mysql_repository as repo
from app.db.mysql_models import TaskDispatchOutbox


@pytest.mark.asyncio
async def test_old_confirm_cannot_mark_new_dispatch_pending(db):
    _doc, _run, task = await seed(db)
    await repo.claim_task(db, task.id, dispatch_seq=1)
    await repo.fail_task(db, task_id=task.id, attempt_id=task.attempt_id, code="x", message="x")
    assert task.dispatch_seq == 2
    assert not await repo.confirm_dispatch(db, task.id, 1)
    assert task.status == "WAITING_PUBLISH"
    record = (await db.execute(select(TaskDispatchOutbox))).scalar_one()
    assert record.payload["dispatch_seq"] == 2 and record.status == "PENDING"


@pytest.mark.asyncio
async def test_consumer_can_finish_before_confirm_without_state_regression(db):
    _doc, _run, task = await seed(db)
    now = datetime.now(UTC)
    payload = await repo.reserve_dispatch(db, task.id, 1, now=now)
    assert payload["dispatch_seq"] == 1
    await repo.claim_task(db, task.id, dispatch_seq=1, now=now)
    await repo.complete_task(
        db, task_id=task.id, attempt_id=task.attempt_id, chunk_count=2, now=now
    )
    assert await repo.confirm_dispatch(db, task.id, 1, now=now)
    assert task.status == "SUCCESS" and task.attempt_count == 1


@pytest.mark.asyncio
async def test_dispatch_reservation_throttles_uncertain_publish_without_execution_attempt(db):
    _doc, _run, task = await seed(db)
    now = datetime.now(UTC)
    assert await repo.reserve_dispatch(db, task.id, 1, now=now)
    await db.commit()
    assert await repo.reserve_dispatch(db, task.id, 1, now=now) is None
    assert await repo.reserve_dispatch(db, task.id, 1, now=now + timedelta(seconds=100))
    assert task.attempt_count == 0


@pytest.mark.asyncio
async def test_repeated_crashes_eventually_fail_without_infinite_reactivation(db):
    _doc, run, task = await seed(db, max_attempts=2)
    now = datetime.now(UTC)
    for seq in (1, 2):
        await repo.claim_task(db, task.id, dispatch_seq=seq, now=now)
        now += timedelta(seconds=10000)
        await repo.reclaim_expired_running_tasks(db, now=now)
        now += timedelta(seconds=100)
    assert task.status == "FAILED" and run.status == "FAILED" and task.attempt_count == 2
    assert await repo.reclaim_expired_running_tasks(db, now=now) == []


@pytest.mark.asyncio
async def test_finalizer_recovers_after_last_success_and_publishes_once(db):
    from app.rag.maintenance import MaintenanceService

    doc, run, task = await seed(db)
    doc.filename = "a.txt"
    run.config_snapshot = {
        "file_type": "txt",
        "chunker": {"size": 16, "overlap": 0},
        "embedding": {"model": "test", "dimension": 2},
    }
    await db.commit()
    p = processor(db)
    await p.process(task.id, dispatch_seq=1)
    service = MaintenanceService(session_factory=SessionFactory(db), chunk_store=p.chunk_store)
    assert await service.publish_ready_runs() == 1
    assert run.status == "SUCCESS" and doc.active_run_id == run.id and doc.current_run_id == run.id
    assert await service.publish_ready_runs() == 0


@pytest.mark.asyncio
async def test_cancel_between_es_verification_and_publish_cannot_publish(db):
    from app.rag.maintenance import MaintenanceService

    doc, run, task = await seed(db)
    task.status, task.chunk_count, run.completed_task_count, run.chunk_count = "SUCCESS", 1, 1, 1
    await db.commit()
    store = MemoryChunks()
    from app.rag.fingerprint import build_task_chunk_id

    store.rows[build_task_chunk_id(task.id, "0")] = {
        "kb_id": "kb",
        "doc_id": "doc",
        "run_id": run.id,
        "task_id": task.id,
        "chunk_key": "0",
        "content": "x",
        "embedding": [1.0, 2.0],
    }
    original = store.read_run_chunks

    async def cancel_after_read(**kwargs):
        rows = await original(**kwargs)
        await repo.request_generation_cancel(db, run.id)
        await db.commit()
        return rows

    store.read_run_chunks = cancel_after_read
    service = MaintenanceService(session_factory=SessionFactory(db), chunk_store=store)
    assert await service.publish_ready_runs() == 0
    assert doc.active_run_id is None and run.status == "CANCELLING"


@pytest.mark.asyncio
async def test_cleanup_protects_active_run_and_live_copy_source(db):
    from app.rag.generation_cleanup import GenerationCleanupService

    doc, run, source = await seed(db)
    task_count = 1
    source.status, source.chunk_count = "SUCCESS", task_count
    run.status, doc.active_run_id = "SUCCESS", run.id
    run.reuse_expires_at = datetime.now(UTC) - timedelta(seconds=10)
    store = AsyncMock()
    cleanup = GenerationCleanupService(chunk_store=store)
    assert await cleanup.cleanup_expired_generations(db) == 0
    doc.active_run_id = None
    run.status = "FAILED"
    _new, tasks = await repo.create_generation(
        db,
        document=doc,
        parse_fingerprint="fp",
        config_snapshot=run.config_snapshot,
        task_ranges=[{"task_no": 1, "core_page_start": 0, "core_page_end": 2}],
    )
    tasks[0].kind, tasks[0].source_task_id = "COPY", source.id
    await repo.create_outbox_records(db, tasks)
    await db.commit()
    assert await cleanup.cleanup_expired_generations(db) == 0
    tasks[0].status = "SUCCESS"
    await db.commit()
    assert await cleanup.cleanup_expired_generations(db) == 1
    store.delete_run_chunks.assert_awaited_once_with(kb_id="kb", doc_id="doc", run_id=run.id)


@pytest.mark.asyncio
async def test_publication_preserves_old_run_for_inflight_search_window(db):
    from app.core.config import settings
    from app.rag.generation_cleanup import GenerationCleanupService

    doc, old_run, old_task = await seed(db)
    old_task.status, old_task.chunk_count = "SUCCESS", 1
    old_run.status, doc.active_run_id = "SUCCESS", old_run.id
    new_run, tasks = await repo.create_generation(
        db,
        document=doc,
        parse_fingerprint="fp",
        config_snapshot=old_run.config_snapshot,
        task_ranges=[{"task_no": 1, "core_page_start": 0, "core_page_end": 2}],
    )
    await repo.create_outbox_records(db, tasks)
    await db.commit()
    target = tasks[0]
    await repo.claim_task(db, target.id, dispatch_seq=1)
    await repo.complete_task(db, task_id=target.id, attempt_id=target.attempt_id, chunk_count=1)
    await db.commit()
    assert doc.active_run_id == old_run.id
    assert await repo.publish_generation(db, new_run.id, expected_tasks={target.id: 1})
    await db.commit()
    assert doc.active_run_id == doc.current_run_id == new_run.id
    cleanup = GenerationCleanupService(chunk_store=AsyncMock())
    assert await cleanup.cleanup_expired_generations(db) == 0
    later = datetime.now(UTC) + timedelta(seconds=settings.result_retention_seconds + 1)
    assert await cleanup.cleanup_expired_generations(db, now=later) == 1
    assert old_run.artifacts_available is False and new_run.artifacts_available is True
