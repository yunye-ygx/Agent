from unittest.mock import AsyncMock, patch

import pytest

from app.api.routes.documents_v2 import router
from app.db import mysql_repository as repository
from app.db.mysql_models import Document, DocumentStatus, KnowledgeBase


def test_document_delete_route_is_registered() -> None:
    assert any(
        route.path == "/api/v1/documents/{document_id}" and "DELETE" in route.methods
        for route in router.routes
    )


@pytest.mark.asyncio
async def test_delete_document_soft_deletes_and_decrements_knowledge_base_count() -> None:
    document = Document(
        knowledge_base_id="kb-1",
        filename="guide.pdf",
        object_key="documents/guide.pdf",
        file_hash="a" * 64,
        file_size=42,
        mime_type="application/pdf",
    )
    knowledge_base = KnowledgeBase(name="知识库", document_count=2)
    session = AsyncMock()

    with patch.object(repository, "get_knowledge_base", new=AsyncMock(return_value=knowledge_base)):
        result = await repository.delete_document(session, document)

    assert result is document
    assert document.status == DocumentStatus.DELETED
    assert document.current_generation_id is None
    assert document.building_generation_id is None
    assert knowledge_base.document_count == 1
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_document_rejects_a_document_that_is_being_parsed() -> None:
    document = Document(
        knowledge_base_id="kb-1",
        filename="guide.pdf",
        object_key="documents/guide.pdf",
        file_hash="b" * 64,
        file_size=42,
        mime_type="application/pdf",
        building_generation_id="generation-1",
    )

    with (
        pytest.raises(ValueError, match="请先取消解析"),
        patch.object(
            repository,
            "get_generation",
            new=AsyncMock(return_value=__import__("types").SimpleNamespace(status="RUNNING")),
        ),
    ):
        await repository.delete_document(AsyncMock(), document)


@pytest.mark.asyncio
async def test_delete_uses_current_kb_count_after_other_committed_updates(db):
    from sqlalchemy import text
    from test_a1_lifecycle import seed

    doc, run, _task = await seed(db)
    run.status = "CANCELLED"
    kb = await repository.get_knowledge_base(db, "kb")
    await db.execute(text("UPDATE knowledge_bases SET document_count=5 WHERE id='kb'"))
    await db.commit()
    assert kb.document_count == 0  # deliberately stale identity-map instance
    await repository.delete_document(db, doc)
    await db.commit()
    await db.refresh(kb)
    assert kb.document_count == 4
