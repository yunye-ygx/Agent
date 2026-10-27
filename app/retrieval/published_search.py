"""Search only explicitly published support documents.

The allowlist is applied inside the Elasticsearch/vector recall (run and document
terms), and every returned candidate is checked against the captured publication
snapshot before it is exposed.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.business.errors import Unavailable
from app.core.config import settings
from app.db import mysql_repository as repository
from app.db.mysql_models import (
    Document,
    DocumentStatus,
    Generation,
    GenerationStatus,
    KnowledgeBase,
    KnowledgeBaseStatus,
)
from app.rag.embedding import EmbeddingError, OpenAICompatibleEmbeddingClient
from app.rag.run_creation import embedding_identity
from app.storage.elasticsearch_chunks import ChunkStore

logger = logging.getLogger(__name__)


class KnowledgeUnavailable(Unavailable):
    default_code = "KNOWLEDGE_UNAVAILABLE"


async def _resolve_embedding(session: AsyncSession, config: dict):
    base = (config.get("base_url") or settings.llm_base_url or "").rstrip("/")
    configs = await repository.list_model_configurations(session)
    saved = next(
        (
            item
            for item in configs
            if item.embedding_model == config.get("model")
            and item.embedding_dimension == config.get("dimension")
            and item.base_url.rstrip("/") == base
        ),
        None,
    )
    key = saved.api_key if saved else (
        settings.llm_api_key if base == (settings.llm_base_url or "").rstrip("/") else None
    )
    if not key or not base or not config.get("model") or not config.get("dimension"):
        raise KnowledgeUnavailable("知识检索的 Embedding 配置不可用")
    return OpenAICompatibleEmbeddingClient(
        settings.model_copy(
            update={
                "llm_base_url": base,
                "llm_api_key": key,
                "embedding_model": config["model"],
                "embedding_dimension": int(config["dimension"]),
            }
        )
    )


def _chunk_store() -> ChunkStore:
    return ChunkStore()


async def search_published_documents(
    session: AsyncSession,
    *,
    document_ids: list[str],
    query: str,
    top_k: int,
) -> dict:
    ids = list(dict.fromkeys(document_ids))
    if not ids:
        return {"results": [], "eligible_documents": 0, "excluded_documents": 0}
    docs = list(
        (
            await session.execute(
                select(Document)
                .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
                .where(
                    Document.id.in_(ids),
                    Document.status == DocumentStatus.ACTIVE,
                    KnowledgeBase.status == KnowledgeBaseStatus.ACTIVE,
                )
            )
        )
        .scalars()
        .all()
    )
    run_ids = [doc.active_run_id for doc in docs if doc.active_run_id]
    runs = list(
        (
            await session.execute(
                select(Generation).where(
                    Generation.id.in_(run_ids),
                    Generation.status == GenerationStatus.SUCCESS,
                    Generation.artifacts_available.is_(True),
                    Generation.published_at.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    by_run = {run.id: run for run in runs}
    eligible = {
        doc.id: by_run[doc.active_run_id]
        for doc in docs
        if doc.active_run_id in by_run and by_run[doc.active_run_id].document_id == doc.id
    }
    excluded = len(ids) - len(eligible)
    if not eligible:
        raise KnowledgeUnavailable(
            "所选资料尚未完成建库发布，暂不可检索", code="KNOWLEDGE_NOT_PUBLISHED"
        )
    identities = {embedding_identity(run.config_snapshot) for run in eligible.values()}
    if len(identities) != 1:
        raise KnowledgeUnavailable("开放资料的向量配置不一致，暂不可混合检索")
    config = next(iter(eligible.values())).config_snapshot.get("embedding", {})
    client = await _resolve_embedding(session, config)
    try:
        vectors = await client.embed_texts([query])
    except EmbeddingError as exc:
        raise KnowledgeUnavailable("知识检索的查询向量生成失败") from exc
    if len(vectors) != 1:
        raise KnowledgeUnavailable("知识检索的查询向量生成失败")
    store = _chunk_store()
    try:
        raw = await store.search(
            vectors[0],
            run_ids=[run.id for run in eligible.values()],
            document_ids=list(eligible),
            limit=max(top_k * 3, top_k),
        )
    except KnowledgeUnavailable:
        raise
    except Exception as exc:
        # ES errors can include internal URLs; keep the public message generic.
        logger.warning("Support knowledge search failed (%s)", type(exc).__name__)
        raise KnowledgeUnavailable("知识检索服务暂不可用") from exc
    finally:
        await store.close()

    results = []
    skipped = 0
    for hit in raw:
        document_id = hit.get("doc_id")
        run = eligible.get(document_id)
        if run is None or hit.get("run_id") != run.id:
            skipped += 1
            continue
        metadata = hit.get("metadata") or {}
        section = metadata.get("section_path", "")
        if isinstance(section, list):
            section = " / ".join(str(part) for part in section)
        results.append(
            {
                "chunk_id": hit["chunk_id"],
                "document_id": document_id,
                "document_title": next(
                    doc.filename for doc in docs if doc.id == document_id
                ),
                "content": str(hit.get("content", ""))[:2000],
                "section": str(section or ""),
                "page_start": hit.get("page_start"),
                "page_end": hit.get("page_end"),
                "score": float(hit.get("score") or 0.0),
                "document_version": run.version_no,
                "published_at": run.published_at.isoformat() if run.published_at else "",
            }
        )
        if len(results) >= top_k:
            break
    if skipped:
        logger.warning("Support knowledge search skipped %s out-of-scope candidates", skipped)
    return {"results": results, "eligible_documents": len(eligible), "excluded_documents": excluded}
