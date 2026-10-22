import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from test_a1_lifecycle import seed

from app.api.routes import knowledge_bases
from app.api.schemas_v2 import KnowledgeBaseUpdate
from app.db import mysql_repository as repository
from app.db.mysql_models import (
    Document,
    DocumentStatus,
    Generation,
    GenerationStatus,
    KnowledgeBase,
    KnowledgeBaseStatus,
    Task,
    TaskStatus,
)
from app.rag.knowledge_base_deletion import KnowledgeBaseDeletionService


def test_knowledge_base_update_accepts_only_a_name():
    payload = KnowledgeBaseUpdate(name="新名称")
    assert payload.name == "新名称"
    with pytest.raises(ValidationError):
        KnowledgeBaseUpdate(name="新名称", description="不允许修改")


def test_knowledge_base_management_routes_are_registered():
    methods_by_path = {}
    for route in knowledge_bases.router.routes:
        methods_by_path.setdefault(route.path, set()).update(route.methods)
    assert "PUT" in methods_by_path["/api/v1/knowledge-bases/{knowledge_base_id}"]
    assert "DELETE" in methods_by_path["/api/v1/knowledge-bases/{knowledge_base_id}"]
    assert "POST" in methods_by_path["/api/v1/knowledge-bases/batch-delete"]


@pytest.mark.asyncio
async def test_update_renames_only_the_knowledge_base(db):
    db.add(KnowledgeBase(id="kb", name="旧名称", description="保留描述"))
    await db.commit()

    response = await knowledge_bases.update_knowledge_base(
        "kb", KnowledgeBaseUpdate(name="新名称"), db
    )

    assert response.name == "新名称"
    assert response.description == "保留描述"


@pytest.mark.asyncio
async def test_delete_with_documents_requires_confirmation_without_mutating_data(db):
    _document, run, task = await seed(db)
    run.status, task.status = GenerationStatus.SUCCESS, TaskStatus.SUCCESS
    await db.commit()

    with pytest.raises(HTTPException) as error:
        await knowledge_bases.delete_knowledge_base("kb", db, confirm_with_documents=False)

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "CONFIRMATION_REQUIRED"
    assert error.value.detail["document_count"] == 1
    assert error.value.detail["knowledge_bases"] == [
        {"id": "kb", "name": "test", "document_count": 1}
    ]
    document = (await db.execute(select(Document).where(Document.id == "doc"))).scalar_one()
    kb = await repository.get_knowledge_base(db, "kb")
    assert document.status == DocumentStatus.ACTIVE
    assert kb.status == KnowledgeBaseStatus.ACTIVE


@pytest.mark.asyncio
async def test_delete_rejects_a_knowledge_base_with_a_running_generation(db):
    document, run, _task = await seed(db)
    run.status = GenerationStatus.RUNNING
    await db.commit()

    with pytest.raises(HTTPException) as error:
        await knowledge_bases.delete_knowledge_base(
            "kb", db, confirm_with_documents=True
        )

    assert error.value.status_code == 409
    assert "正在解析" in error.value.detail
    assert document.status == DocumentStatus.ACTIVE
    assert (await repository.get_knowledge_base(db, "kb")).status == KnowledgeBaseStatus.ACTIVE


@pytest.mark.asyncio
async def test_confirmed_delete_is_queued_and_hides_knowledge_base_from_normal_list(db):
    _document, run, task = await seed(db)
    run.status, task.status = GenerationStatus.SUCCESS, TaskStatus.SUCCESS
    await db.commit()
    purger = AsyncMock()
    with patch.object(KnowledgeBaseDeletionService, "purge", purger):
        response = await knowledge_bases.delete_knowledge_base(
            "kb", db, confirm_with_documents=True
        )

    assert response.status_code == 202
    assert json.loads(response.body) == {"accepted": ["kb"], "skipped": []}
    purger.purge.assert_not_awaited()
    assert (await db.execute(select(KnowledgeBase))).scalars().one().status == KnowledgeBaseStatus.DELETING
    assert (await db.execute(select(Document))).scalars().one().status == DocumentStatus.DELETED
    assert (await db.execute(select(Generation))).scalars().one().artifacts_available is False
    assert (await db.execute(select(Task))).scalars().one().id == task.id
    assert await repository.list_knowledge_bases(db) == []


@pytest.mark.asyncio
async def test_delete_without_documents_is_queued_without_confirmation(db):
    db.add(KnowledgeBase(id="empty-kb", name="空知识库"))
    await db.commit()
    purger = AsyncMock()
    with patch.object(KnowledgeBaseDeletionService, "purge", purger):
        response = await knowledge_bases.delete_knowledge_base("empty-kb", db)

    assert response.status_code == 202
    purger.purge.assert_not_awaited()
    assert json.loads(response.body) == {"accepted": ["empty-kb"], "skipped": []}
    assert (await db.execute(select(KnowledgeBase))).scalars().one().status == KnowledgeBaseStatus.DELETING


@pytest.mark.asyncio
async def test_purge_clears_es_files_historical_sources_and_experiment_snapshots():
    from app.core.config import settings

    storage, chunks, snapshots = AsyncMock(), AsyncMock(), AsyncMock()
    service = KnowledgeBaseDeletionService(
        storage=storage, chunk_store=chunks, snapshot_store=snapshots
    )

    await service.purge(knowledge_base_id="kb", source_object_keys=["legacy/source.pdf"])

    chunks.delete_knowledge_base_chunks.assert_awaited_once_with(kb_id="kb")
    assert storage.delete_prefix.await_args_list[0].args == (
        settings.minio_files_bucket,
        "knowledge-bases/kb/",
    )
    assert storage.delete_prefix.await_args_list[1].args == (
        settings.minio_artifacts_bucket,
        "knowledge-bases/kb/",
    )
    storage.delete_object.assert_awaited_once_with(
        settings.minio_files_bucket, "legacy/source.pdf"
    )
    snapshots.delete_knowledge_base_snapshots.assert_awaited_once_with("kb")


@pytest.mark.asyncio
async def test_request_does_not_run_external_cleanup_inline(db):
    _document, run, task = await seed(db)
    run.status, task.status = GenerationStatus.SUCCESS, TaskStatus.SUCCESS
    await db.commit()
    purger = AsyncMock()
    with patch.object(KnowledgeBaseDeletionService, "purge", purger):
        await knowledge_bases.delete_knowledge_base(
            "kb", db, confirm_with_documents=True
        )

    purger.purge.assert_not_awaited()
    kb = (await db.execute(select(KnowledgeBase))).scalar_one()
    document = (await db.execute(select(Document))).scalar_one()
    assert kb.status == KnowledgeBaseStatus.DELETING
    assert document.status == DocumentStatus.DELETED


@pytest.mark.asyncio
async def test_batch_delete_skips_parsing_knowledge_base_and_queues_eligible_items(db):
    _document, run, task = await seed(db)
    run.status, task.status = GenerationStatus.SUCCESS, TaskStatus.SUCCESS
    db.add(KnowledgeBase(id="parsing-kb", name="解析中"))
    await db.flush()
    from app.db.mysql_models import Document as DocumentModel

    parsing_doc = DocumentModel(
        id="parsing-doc", knowledge_base_id="parsing-kb", filename="x.pdf",
        object_key="x", file_hash="hash", file_size=1, mime_type="application/pdf",
    )
    db.add(parsing_doc)
    await db.flush()
    parsing_run = Generation(
        id="parsing-run", document_id="parsing-doc", version_no=1,
        status=GenerationStatus.RUNNING, parse_fingerprint="x", config_snapshot={},
    )
    db.add(parsing_run)
    await db.flush()
    db.add(Task(
        id="parsing-task", generation_id="parsing-run", document_id="parsing-doc",
        task_no=1, core_page_start=1, core_page_end=1, parse_fingerprint="x",
        status=TaskStatus.RUNNING,
    ))
    await db.commit()

    response = await knowledge_bases.batch_delete_knowledge_bases(
        knowledge_bases.KnowledgeBaseBatchDelete(ids=["kb", "parsing-kb"], confirm_with_documents=True),
        db,
    )

    assert response.status_code == 202
    assert json.loads(response.body) == {
        "accepted": ["kb"],
        "skipped": [{"id": "parsing-kb", "reason": "知识库中有文档正在解析，已跳过"}],
    }
    statuses = {item.id: item.status for item in (await db.execute(select(KnowledgeBase))).scalars()}
    assert statuses == {"kb": KnowledgeBaseStatus.DELETING, "parsing-kb": KnowledgeBaseStatus.ACTIVE}


@pytest.mark.asyncio
async def test_batch_confirmation_describes_files_and_does_not_partially_queue(db):
    _document, run, task = await seed(db)
    run.status, task.status = GenerationStatus.SUCCESS, TaskStatus.SUCCESS
    db.add(KnowledgeBase(id="parsing-kb", name="解析中"))
    await db.flush()
    from app.db.mysql_models import Document as DocumentModel

    db.add(DocumentModel(
        id="parsing-doc", knowledge_base_id="parsing-kb", filename="x.pdf",
        object_key="x", file_hash="hash", file_size=1, mime_type="application/pdf",
    ))
    await db.flush()
    db.add(Generation(
        id="parsing-run", document_id="parsing-doc", version_no=1,
        status=GenerationStatus.RUNNING, parse_fingerprint="x", config_snapshot={},
    ))
    await db.commit()

    with pytest.raises(HTTPException) as error:
        await knowledge_bases.batch_delete_knowledge_bases(
            knowledge_bases.KnowledgeBaseBatchDelete(ids=["kb", "parsing-kb"]), db
        )

    assert error.value.status_code == 409
    assert error.value.detail["knowledge_bases"] == [
        {"id": "kb", "name": "test", "document_count": 1}
    ]
    assert error.value.detail["skipped"] == [
        {"id": "parsing-kb", "reason": "知识库中有文档正在解析，已跳过"}
    ]
    statuses = {item.id: item.status for item in (await db.execute(select(KnowledgeBase))).scalars()}
    assert statuses == {"kb": KnowledgeBaseStatus.ACTIVE, "parsing-kb": KnowledgeBaseStatus.ACTIVE}


@pytest.mark.asyncio
async def test_worker_purges_pending_deletion_then_removes_database_history(db):
    _document, run, task = await seed(db)
    run.status, task.status = GenerationStatus.SUCCESS, TaskStatus.SUCCESS
    await repository.prepare_knowledge_base_deletions(
        db, ["kb"], confirm_with_documents=True
    )
    await db.commit()
    from test_a1_pipeline import SessionFactory

    service = KnowledgeBaseDeletionService(
        storage=AsyncMock(), chunk_store=AsyncMock(), snapshot_store=AsyncMock(),
        session_factory=SessionFactory(db),
    )
    service.purge = AsyncMock()
    await service.process_pending()

    service.purge.assert_awaited_once_with(
        knowledge_base_id="kb", source_object_keys=["immutable/pdf"]
    )
    assert (await db.execute(select(KnowledgeBase))).scalars().all() == []


@pytest.mark.asyncio
async def test_worker_retries_failed_cleanup_from_persisted_deleting_marker(db):
    _document, run, task = await seed(db)
    run.status, task.status = GenerationStatus.SUCCESS, TaskStatus.SUCCESS
    await repository.prepare_knowledge_base_deletions(
        db, ["kb"], confirm_with_documents=True
    )
    await db.commit()
    from test_a1_pipeline import SessionFactory

    service = KnowledgeBaseDeletionService(
        storage=AsyncMock(), chunk_store=AsyncMock(), snapshot_store=AsyncMock(),
        session_factory=SessionFactory(db),
    )
    service.purge = AsyncMock(side_effect=[RuntimeError("temporary storage error"), None])

    assert await service.process_pending() == 0
    assert (await db.execute(select(KnowledgeBase))).scalars().one().status == KnowledgeBaseStatus.DELETING
    assert await service.process_pending() == 1
    assert (await db.execute(select(KnowledgeBase))).scalars().all() == []
    assert service.purge.await_count == 2
