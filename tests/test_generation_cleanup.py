from datetime import UTC, datetime, timedelta

import pytest
from test_a1_lifecycle import seed
from test_a1_pipeline import MemoryChunks

from app.rag.generation_cleanup import GenerationCleanupService


@pytest.mark.asyncio
async def test_cleanup_can_remove_late_results_on_a_later_scan(db):
    doc, run, task = await seed(db)
    run.status = "FAILED"
    run.reuse_expires_at = datetime.now(UTC) - timedelta(days=1)
    await db.commit()
    store = MemoryChunks()
    service = GenerationCleanupService(chunk_store=store)
    assert await service.cleanup_expired_generations(db) == 1
    assert not run.artifacts_available
    store.rows["late"] = {"kb_id": "kb", "doc_id": "doc", "run_id": run.id, "task_id": task.id}
    assert await service.cleanup_expired_generations(db) == 1
    assert not store.rows
    assert doc.current_run_id == run.id
