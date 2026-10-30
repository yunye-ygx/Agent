"""单字段开放设计下的售后资料检索与开关用例。

型号维度不入库：检索只保证“未开放的文档不会被召回”，型号由 Agent 负责澄清。
"""

from datetime import UTC, datetime

import pytest

from app.business import knowledge as service
from app.business.errors import Conflict, Forbidden, NotFound
from app.business.models import BusinessUser
from app.business.principal import Principal
from app.db.mysql_models import (
    Document,
    DocumentStatus,
    Generation,
    GenerationStatus,
    KnowledgeBase,
)
from app.retrieval import published_search


class FakeEmbedding:
    async def embed_texts(self, texts):
        assert texts
        return [[0.1, 0.2]]


class FakeStore:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []
        self.closed = False

    async def search(self, vector, **kwargs):
        self.calls.append(kwargs)
        assert vector == [0.1, 0.2]
        return list(self.hits)

    async def close(self):
        self.closed = True


def hit(document_id="doc1", run_id="run-doc1", **overrides):
    payload = {
        "chunk_id": "chunk-1",
        "doc_id": document_id,
        "run_id": run_id,
        "content": "E17 表示上盖未闭合。",
        "metadata": {"section_path": ["MAN-NX100", "S2"]},
        "page_start": 2,
        "page_end": 2,
        "score": 0.873,
        "task_id": "task-secret",
        "object_key": "private/source.md",
    }
    payload.update(overrides)
    return payload


def install_fakes(monkeypatch, store):
    async def fake_resolve(session, config):
        return FakeEmbedding()

    monkeypatch.setattr(published_search, "_resolve_embedding", fake_resolve)
    monkeypatch.setattr(published_search, "_chunk_store", lambda: store)


async def add_document(
    db,
    document_id: str,
    filename: str,
    *,
    support_open: bool = True,
    published: bool = True,
) -> tuple[Document, Generation]:
    document = Document(
        id=document_id,
        knowledge_base_id="kb",
        filename=filename,
        object_key=f"private/{document_id}.md",
        file_hash=document_id.ljust(64, "x")[:64],
        file_size=10,
        mime_type="text/markdown",
        status=DocumentStatus.ACTIVE,
        support_open=support_open,
    )
    run = Generation(
        id=f"run-{document_id}",
        document_id=document_id,
        version_no=1,
        status=GenerationStatus.SUCCESS,
        parse_fingerprint=f"fp-{document_id}",
        config_snapshot={
            "embedding": {"model": "embedding-3", "dimension": 2, "base_url": "https://embed.test/v1"}
        },
        file_version="v1",
        file_location_snapshot=f"private/{document_id}.md",
        chunk_count=1,
        published_at=datetime.now(UTC) if published else None,
        artifacts_available=True,
    )
    db.add(document)
    db.add(run)
    document.active_run_id = run.id
    await db.commit()
    return document, run


async def seed_environment(db, *, published: bool = True, support_open: bool = True):
    admin = BusinessUser(
        username="admin", display_name="管理员", password_hash="unused", role="admin"
    )
    alice = BusinessUser(
        username="alice", display_name="爱丽丝", password_hash="unused", role="customer"
    )
    db.add(admin)
    db.add(alice)
    db.add(KnowledgeBase(id="kb", name="售后开放资料", status="ACTIVE"))
    await db.flush()
    document, run = await add_document(
        db, "doc1", "NX100 产品手册.md", support_open=support_open, published=published
    )
    return (
        Principal.for_user(
            user_id=admin.id, username="admin", display_name="管理员", role="admin"
        ),
        Principal.for_user(
            user_id=alice.id, username="alice", display_name="爱丽丝", role="customer"
        ),
        document,
        run,
    )


@pytest.mark.asyncio
async def test_search_only_covers_open_published_documents_and_redacts_internals(
    knowledge_db, monkeypatch
):
    _admin, alice, _doc, _run = await seed_environment(knowledge_db)
    _closed, _closed_run = await add_document(
        knowledge_db, "doc2", "NX200 产品手册.md", support_open=False
    )
    store = FakeStore([hit(), hit(document_id="doc2", run_id="run-doc2", chunk_id="chunk-2")])
    install_fakes(monkeypatch, store)

    result = await service.search_support_knowledge(
        knowledge_db, alice, query="E17 怎么处理", top_k=5
    )
    assert store.calls[0]["document_ids"] == ["doc1"]
    assert store.calls[0]["run_ids"] == ["run-doc1"]
    assert store.closed is True
    assert [item["document_id"] for item in result["results"]] == ["doc1"]
    assert result["results"][0]["document_title"] == "NX100 产品手册.md"
    assert result["results"][0]["section"] == "MAN-NX100 / S2"
    serialized = str(result)
    assert "object_key" not in serialized
    assert "task-secret" not in serialized
    assert "private/doc1.md" not in serialized


@pytest.mark.asyncio
async def test_all_open_documents_are_eligible_regardless_of_model(knowledge_db, monkeypatch):
    """型号不入库：开放文档都会进入召回范围，由 Agent 负责限定型号。"""
    _admin, alice, _doc, _run = await seed_environment(knowledge_db)
    await add_document(knowledge_db, "doc2", "NX200 产品手册.md", support_open=True)
    store = FakeStore([hit()])
    install_fakes(monkeypatch, store)

    await service.search_support_knowledge(knowledge_db, alice, query="E17", top_k=5)
    assert set(store.calls[0]["document_ids"]) == {"doc1", "doc2"}


@pytest.mark.asyncio
async def test_search_fails_when_no_document_is_open(knowledge_db, monkeypatch):
    _admin, alice, _doc, _run = await seed_environment(knowledge_db, support_open=False)
    store = FakeStore([hit()])
    install_fakes(monkeypatch, store)
    with pytest.raises(NotFound):
        await service.search_support_knowledge(knowledge_db, alice, query="E17", top_k=5)
    assert store.calls == []


@pytest.mark.asyncio
async def test_unpublished_open_document_is_not_silently_searched(knowledge_db, monkeypatch):
    _admin, alice, _doc, _run = await seed_environment(knowledge_db, published=False)
    store = FakeStore([hit()])
    install_fakes(monkeypatch, store)
    with pytest.raises(published_search.KnowledgeUnavailable) as error:
        await service.search_support_knowledge(knowledge_db, alice, query="E17", top_k=5)
    assert error.value.code == "KNOWLEDGE_NOT_PUBLISHED"
    assert store.calls == []


@pytest.mark.asyncio
async def test_candidates_outside_the_open_set_are_dropped(knowledge_db, monkeypatch):
    _admin, alice, _doc, _run = await seed_environment(knowledge_db)
    store = FakeStore([hit(document_id="evil", run_id="run-evil")])
    install_fakes(monkeypatch, store)
    result = await service.search_support_knowledge(
        knowledge_db, alice, query="E17", top_k=5
    )
    assert result["results"] == []


@pytest.mark.asyncio
async def test_search_reports_unavailable_when_embedding_is_not_configured(
    knowledge_db, monkeypatch
):
    _admin, alice, _doc, _run = await seed_environment(knowledge_db)

    async def unavailable(session, config):
        raise published_search.KnowledgeUnavailable("知识检索的 Embedding 配置不可用")

    monkeypatch.setattr(published_search, "_resolve_embedding", unavailable)
    with pytest.raises(published_search.KnowledgeUnavailable):
        await service.search_support_knowledge(knowledge_db, alice, query="E17", top_k=5)


@pytest.mark.asyncio
async def test_admin_opens_and_closes_published_document(knowledge_db, monkeypatch):
    admin, alice, document, _run = await seed_environment(knowledge_db, support_open=False)
    store = FakeStore([hit()])
    install_fakes(monkeypatch, store)
    with pytest.raises(NotFound):
        await service.search_support_knowledge(knowledge_db, alice, query="E17", top_k=5)

    opened = await service.set_document_support_open(
        knowledge_db, admin, document.id, open_=True
    )
    assert opened["support_open"] is True and opened["searchable"] is True
    assert opened["version_no"] == 1 and opened["filename"] == "NX100 产品手册.md"

    result = await service.search_support_knowledge(
        knowledge_db, alice, query="E17", top_k=5
    )
    assert result["eligible_documents"] == 1

    closed = await service.set_document_support_open(
        knowledge_db, admin, document.id, open_=False
    )
    assert closed["support_open"] is False and closed["searchable"] is False
    with pytest.raises(NotFound):
        await service.search_support_knowledge(knowledge_db, alice, query="E17", top_k=5)


@pytest.mark.asyncio
async def test_opening_unpublished_document_is_rejected(knowledge_db):
    admin, _alice, document, _run = await seed_environment(
        knowledge_db, published=False, support_open=False
    )
    with pytest.raises(Conflict) as error:
        await service.set_document_support_open(
            knowledge_db, admin, document.id, open_=True
        )
    assert error.value.code == "KNOWLEDGE_NOT_PUBLISHED"
    await knowledge_db.refresh(document)
    assert document.support_open is False


@pytest.mark.asyncio
async def test_only_admin_can_toggle_support_open(knowledge_db):
    _admin, alice, document, _run = await seed_environment(knowledge_db, support_open=False)
    with pytest.raises(Forbidden):
        await service.set_document_support_open(
            knowledge_db, alice, document.id, open_=True
        )
    await knowledge_db.refresh(document)
    assert document.support_open is False


@pytest.mark.asyncio
async def test_unknown_document_cannot_be_opened(knowledge_db):
    admin, _alice, _document, _run = await seed_environment(knowledge_db)
    with pytest.raises(NotFound):
        await service.set_document_support_open(knowledge_db, admin, "missing", open_=True)