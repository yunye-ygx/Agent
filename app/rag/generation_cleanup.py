from datetime import datetime

from sqlalchemy import select

from app.db import mysql_repository as repository
from app.db.mysql_models import Generation, GenerationStatus, Task, TaskStatus
from app.db.task_lifecycle import aware
from app.storage.elasticsearch_chunks import ChunkStore


class GenerationCleanupService:
    def __init__(self, *, chunk_store=None):
        self.chunk_store = chunk_store or ChunkStore()

    async def cleanup_expired_generations(self, session, *, now: datetime | None = None):
        timestamp = now or repository.utc_now()
        candidates = list(
            (
                await session.execute(
                    select(Generation.id)
                    .where(
                        Generation.status.in_(
                            [
                                GenerationStatus.FAILED,
                                GenerationStatus.CANCELLED,
                                GenerationStatus.SUCCESS,
                            ]
                        ),
                        Generation.reuse_expires_at.is_not(None),
                        Generation.reuse_expires_at <= timestamp,
                    )
                    .order_by(Generation.updated_at, Generation.id)
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        cleaned = 0
        for run_id in candidates:
            doc, run = await repository.lock_run(session, run_id)
            if run:
                run.updated_at = timestamp
            if not doc or not run or doc.active_run_id == run_id:
                await session.commit()
                continue
            if not run.reuse_expires_at or aware(run.reuse_expires_at) > aware(timestamp):
                await session.commit()
                continue
            if run.status not in {
                GenerationStatus.FAILED,
                GenerationStatus.CANCELLED,
                GenerationStatus.SUCCESS,
            }:
                await session.commit()
                continue
            source_ids = select(Task.id).where(Task.generation_id == run_id)
            protected = (
                await session.execute(
                    select(Task.id)
                    .join(Generation, Task.generation_id == Generation.id)
                    .where(
                        Task.source_task_id.in_(source_ids),
                        Task.status.in_(
                            [TaskStatus.WAITING_PUBLISH, TaskStatus.PENDING, TaskStatus.RUNNING]
                        ),
                        Generation.status == GenerationStatus.RUNNING,
                    )
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if protected:
                await session.commit()
                continue
            # Tombstone is committed under the same Document lock used to choose
            # COPY sources. Future runs cannot select artifacts being deleted.
            run.artifacts_available = False
            kb_id, doc_id = doc.knowledge_base_id, doc.id
            await session.commit()
            await self.chunk_store.delete_run_chunks(kb_id=kb_id, doc_id=doc_id, run_id=run_id)
            cleaned += 1
            # Retain ownership/control records and the expiry marker: repeated
            # scans can remove records from in-flight ES requests arriving late.
        await session.commit()
        return cleaned
