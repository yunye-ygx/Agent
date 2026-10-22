import asyncio
import logging

from sqlalchemy import select

from app.core.config import settings
from app.db import mysql_repository as repository
from app.db.mysql_models import Document, Generation, KnowledgeBase, KnowledgeBaseStatus
from app.db.mysql_session import AsyncSessionFactory
from app.retrieval.snapshots import SnapshotStore
from app.storage.elasticsearch_chunks import ChunkStore
from app.storage.minio_storage import MinioStorage

logger = logging.getLogger(__name__)


class KnowledgeBaseDeletionService:
    def __init__(
        self, *, storage=None, chunk_store=None, snapshot_store=None, session_factory=None
    ):
        self.storage = storage or MinioStorage()
        self.chunk_store = chunk_store or ChunkStore()
        self.snapshot_store = snapshot_store or SnapshotStore(storage=self.storage)
        self.session_factory = session_factory or AsyncSessionFactory
        self._pending_scan_lock = asyncio.Lock()

    async def purge(
        self,
        *,
        knowledge_base_id: str,
        source_object_keys: list[str],
    ) -> None:
        await self.chunk_store.delete_knowledge_base_chunks(kb_id=knowledge_base_id)
        prefix = f"knowledge-bases/{knowledge_base_id}/"
        await self.storage.delete_prefix(settings.minio_files_bucket, prefix)
        for object_key in source_object_keys:
            await self.storage.delete_object(settings.minio_files_bucket, object_key)
        await self.storage.delete_prefix(settings.minio_artifacts_bucket, prefix)
        await self.snapshot_store.delete_knowledge_base_snapshots(knowledge_base_id)

    async def process_pending(self, *, limit: int = 20) -> int:
        """Retry persisted DELETING markers until their external data is purged."""
        if self._pending_scan_lock.locked():
            return 0
        async with self._pending_scan_lock:
            async with self.session_factory() as session:
                knowledge_base_ids = list(
                    (
                        await session.execute(
                            select(KnowledgeBase.id)
                            .where(KnowledgeBase.status == KnowledgeBaseStatus.DELETING)
                            .order_by(KnowledgeBase.updated_at, KnowledgeBase.id)
                            .limit(limit)
                        )
                    )
                    .scalars()
                    .all()
                )

            completed = 0
            for knowledge_base_id in knowledge_base_ids:
                async with self.session_factory() as session:
                    documents = list(
                        (
                            await session.execute(
                                select(Document).where(
                                    Document.knowledge_base_id == knowledge_base_id
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    object_keys = {doc.object_key for doc in documents if doc.object_key}
                    if documents:
                        historical_keys = (
                            await session.execute(
                                select(Generation.file_location_snapshot).where(
                                    Generation.document_id.in_([doc.id for doc in documents])
                                )
                            )
                        ).scalars()
                        object_keys.update(key for key in historical_keys if key)

                try:
                    await self.purge(
                        knowledge_base_id=knowledge_base_id,
                        source_object_keys=sorted(object_keys),
                    )
                    async with self.session_factory() as session:
                        await repository.complete_knowledge_base_deletion(
                            session, knowledge_base_id
                        )
                    completed += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Knowledge-base deletion cleanup failed; will retry: %s",
                        knowledge_base_id,
                    )
            return completed
