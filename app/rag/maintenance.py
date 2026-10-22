"""Independent recovery and publication; never depends on a Worker surviving."""

import asyncio
import logging
from collections import Counter

from sqlalchemy import select

from app.core.config import settings
from app.db import mysql_repository as repository
from app.db.mysql_models import Document, Generation, GenerationStatus, Task, TaskStatus
from app.db.mysql_session import AsyncSessionFactory
from app.rag.fingerprint import build_task_chunk_id
from app.rag.generation_cleanup import GenerationCleanupService
from app.rag.knowledge_base_deletion import KnowledgeBaseDeletionService
from app.storage.elasticsearch_chunks import ChunkStore

logger = logging.getLogger(__name__)


class MaintenanceService:
    def __init__(self, *, session_factory=None, chunk_store=None):
        self.session_factory = session_factory or AsyncSessionFactory
        self.chunk_store = chunk_store or ChunkStore()
        self.knowledge_base_deletion = KnowledgeBaseDeletionService(
            session_factory=self.session_factory, chunk_store=self.chunk_store
        )

    async def recover(self):
        async with self.session_factory() as session:
            await repository.reclaim_expired_running_tasks(session)
            await session.commit()
        async with self.session_factory() as session:
            await repository.finish_cancellations(session)
            await session.commit()

    async def publish_ready_runs(self):
        async with self.session_factory() as session:
            ids = list(
                (
                    await session.execute(
                        select(Generation.id)
                        .join(Document, Document.current_run_id == Generation.id)
                        .where(
                            Generation.status == GenerationStatus.RUNNING,
                            Generation.completed_task_count == Generation.expected_task_count,
                        )
                        .order_by(Generation.updated_at, Generation.id)
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
        published = 0
        for run_id in ids:
            try:
                async with self.session_factory() as session:
                    run = await repository.get_generation(session, run_id)
                    doc = await repository.get_document(session, run.document_id)
                    tasks = list(
                        (await session.execute(select(Task).where(Task.generation_id == run_id)))
                        .scalars()
                        .all()
                    )
                    if (
                        not repository.valid_run(doc, run)
                        or len(tasks) != run.expected_task_count
                        or not tasks
                        or any(t.status != TaskStatus.SUCCESS for t in tasks)
                    ):
                        continue
                    expected = {t.id: t.result_chunk_count for t in tasks}
                    dimension = run.config_snapshot["embedding"]["dimension"]
                    kb_id, doc_id = doc.knowledge_base_id, doc.id
                await self.chunk_store.refresh()
                rows = await self.chunk_store.read_run_chunks(
                    kb_id=kb_id, doc_id=doc_id, run_id=run_id
                )
                counts = Counter(value.get("task_id") for _id, value in rows)
                actual = {task_id: counts[task_id] for task_id in expected}
                if actual != expected or len(rows) != sum(expected.values()):
                    raise ValueError("发布检查：ES 任务产物数量与成功清单不一致")
                ids_seen = set()
                for chunk_id, value in rows:
                    key = value.get("chunk_key")
                    if (
                        key is None
                        or chunk_id in ids_seen
                        or value.get("task_id") not in expected
                        or chunk_id != build_task_chunk_id(value["task_id"], key)
                        or len(value.get("embedding", [])) != dimension
                        or not value.get("content")
                    ):
                        raise ValueError("发布检查：产物归属、稳定 ID 或文本/向量不完整")
                    ids_seen.add(chunk_id)
                async with self.session_factory() as session:
                    if await repository.publish_generation(
                        session, run_id, expected_tasks=expected
                    ):
                        published += 1
                    await session.commit()
            except Exception:
                logger.exception(
                    "Run %s publication deferred; old active results remain visible", run_id
                )
                try:
                    async with self.session_factory() as session:
                        doc, run = await repository.lock_run(session, run_id)
                        if repository.valid_run(doc, run):
                            run.failure_reason = (
                                "发布检查尚未通过；详见后台日志，旧有效批次继续检索"
                            )
                            run.updated_at = repository.utc_now()
                            await session.commit()
                except Exception:
                    logger.exception("Unable to record deferred publication for %s", run_id)
        return published

    async def run_once(self):
        await self.recover()
        await self.publish_ready_runs()
        await self.knowledge_base_deletion.process_pending()
        async with self.session_factory() as session:
            await GenerationCleanupService(
                chunk_store=self.chunk_store
            ).cleanup_expired_generations(session)


async def run_maintenance():
    service = MaintenanceService()
    try:
        while True:
            try:
                await service.run_once()
            except Exception:
                logger.exception("A1 maintenance scan failed; retrying on next interval")
            await asyncio.sleep(max(0.1, settings.maintenance_interval_seconds))
    finally:
        await service.chunk_store.close()


if __name__ == "__main__":
    asyncio.run(run_maintenance())
