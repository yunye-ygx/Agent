from io import BytesIO
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import UploadFile
from sqlalchemy import select
from test_a1_lifecycle import seed
from test_a1_pipeline import SessionFactory

from app.api.routes import documents_v2, retrieval
from app.db import mysql_repository as repo
from app.db.mysql_models import Document, Generation, KnowledgeBase, Task


@pytest.mark.asyncio
async def test_upload_txt_does_not_create_run_or_outbox(db):
    kb = KnowledgeBase(id="kb", name="test")
    db.add(kb)
    await db.commit()
    with patch.object(documents_v2, "storage", AsyncMock()):
        response = await documents_v2.upload_document(
            "kb", UploadFile(filename="a.txt", file=BytesIO(b"text")), db
        )
    assert response.active_run_id is None and response.current_run_id is None
    assert (await db.execute(select(Generation))).scalars().all() == []
    result = await documents_v2._create_generation(response.id, db, force_new=True)
    task = (await db.execute(select(Task))).scalar_one()
    run = await repo.get_generation(db, result["generation_id"])
    assert task.core_page_start == task.core_page_end == 0
    assert run.config_snapshot["file_type"] == "txt"


@pytest.mark.asyncio
async def test_reparse_schedules_copy_without_premarking_success_or_writing_es(db):
    doc, run, source = await seed(db)
    source.status, source.chunk_count, run.status = "SUCCESS", 2, "FAILED"
    from datetime import UTC, datetime, timedelta

    run.reuse_expires_at = datetime.now(UTC) + timedelta(days=1)
    await db.commit()
    store = AsyncMock()
    with (
        patch("app.rag.run_creation.build_parse_fingerprint", return_value="fp"),
        patch(
            "app.rag.run_creation.build_config_snapshot",
            return_value={**run.config_snapshot, "file_type": "pdf", "task_page_size": 10},
        ),
        patch.object(documents_v2, "chunk_store", store),
    ):
        run.config_snapshot = {**run.config_snapshot, "file_type": "pdf", "task_page_size": 10}
        await db.commit()
        result = await documents_v2._create_generation(doc.id, db, force_new=True)
    target = (
        await db.execute(select(Task).where(Task.generation_id == result["generation_id"]))
    ).scalar_one()
    assert target.kind == "COPY" and target.source_task_id == source.id
    assert target.status == "WAITING_PUBLISH" and target.chunk_count == 0
    assert store.method_calls == []


@pytest.mark.asyncio
async def test_kb_search_queries_all_active_documents_and_excludes_deleted(db):
    doc, run, _task = await seed(db)
    run.status, doc.active_run_id = "SUCCESS", run.id
    other = Document(
        id="doc2",
        knowledge_base_id="kb",
        filename="b.txt",
        object_key="b",
        file_hash="b",
        file_size=1,
        mime_type="text/plain",
        current_generation_id="run2",
    )
    db.add(other)
    db.add(
        Generation(
            id="run2",
            document_id="doc2",
            version_no=1,
            status="SUCCESS",
            parse_fingerprint="fp",
            config_snapshot=run.config_snapshot,
        )
    )
    deleted = Document(
        id="deleted",
        knowledge_base_id="kb",
        filename="c.txt",
        object_key="c",
        file_hash="c",
        file_size=1,
        mime_type="text/plain",
        status="DELETED",
        current_generation_id="run3",
    )
    db.add(deleted)
    await db.commit()
    client = AsyncMock()
    client.embed_texts.return_value = [[1.0, 2.0]]
    store = AsyncMock()
    store.search.return_value = []
    with (
        patch.object(retrieval, "AsyncSessionFactory", SessionFactory(db)),
        patch.object(retrieval, "embedding_client", client),
        patch.object(retrieval, "chunk_store", store),
    ):
        result = await retrieval.search(
            q="hello", knowledge_base_id="kb", document_id=None, top_k=5
        )
    kwargs = store.search.call_args.kwargs
    assert set(kwargs["run_ids"]) == {run.id, "run2"}
    assert kwargs["kb_id"] == "kb" and kwargs["document_id"] is None
    assert set(result.active_run_ids) == {run.id, "run2"}


@pytest.mark.asyncio
async def test_cancel_endpoint_only_finishes_its_own_run(db):
    doc, run, _task = await seed(db)
    db.add(
        Document(
            id="other",
            knowledge_base_id="kb",
            filename="other.txt",
            object_key="other",
            file_hash="other",
            file_size=1,
            mime_type="text/plain",
            building_generation_id="other-run",
        )
    )
    await db.flush()
    other = Generation(
        id="other-run",
        document_id="other",
        version_no=1,
        status="CANCELLING",
        parse_fingerprint="other",
    )
    db.add(other)
    await db.commit()
    await documents_v2.cancel_document(doc.id, db)
    assert run.status == "CANCELLED" and other.status == "CANCELLING"
