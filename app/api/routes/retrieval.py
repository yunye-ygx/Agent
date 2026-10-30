from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from app.api.schemas_v2 import SearchResponse
from app.auth.dependencies import require_admin
from app.core.config import settings
from app.db import mysql_repository as repository
from app.db.mysql_models import Document, DocumentStatus, Generation
from app.db.mysql_session import AsyncSessionFactory
from app.rag.embedding import EmbeddingError, OpenAICompatibleEmbeddingClient
from app.rag.run_creation import embedding_identity
from app.storage.elasticsearch_chunks import ChunkStore

router = APIRouter(
    prefix="/api/v1/search", tags=["retrieval"], dependencies=[Depends(require_admin)]
)
# Injectable override for isolated tests; production selects the active snapshot.
embedding_client = None
chunk_store = ChunkStore()


@router.get("", response_model=SearchResponse)
async def search(
    q: str = Query(..., min_length=1, max_length=1000),
    knowledge_base_id: str = Query(...),
    document_id: str | None = Query(default=None),
    top_k: int = Query(default=5, ge=1, le=20),
) -> SearchResponse:
    async with AsyncSessionFactory() as session:
        kb = await repository.get_knowledge_base(session, knowledge_base_id)
        if kb is None:
            raise HTTPException(404, "知识库不存在")
        if document_id:
            doc = await repository.get_document(session, document_id)
            if doc is None or doc.knowledge_base_id != knowledge_base_id:
                raise HTTPException(404, "文档不存在")
        query = (
            select(Generation)
            .join(Document, Document.active_run_id == Generation.id)
            .where(
                Document.knowledge_base_id == knowledge_base_id,
                Document.status == DocumentStatus.ACTIVE,
                Generation.artifacts_available.is_(True),
            )
        )
        if document_id:
            query = query.where(Document.id == document_id)
        runs = list((await session.execute(query)).scalars().all())
        if not runs:
            raise HTTPException(409, "尚无完整发布的结果，当前不可检索")
        identity = embedding_identity(runs[0].config_snapshot)
        if any(embedding_identity(run.config_snapshot) != identity for run in runs):
            raise HTTPException(409, "知识库有效版本的 embedding 配置不兼容")
        vector_config = runs[0].config_snapshot["embedding"]
        run_ids = [run.id for run in runs]
    # Release the DB transaction before remote embedding/ES calls. Superseded
    # runs remain retained for an in-flight search that captured these pointers.
    client = embedding_client or OpenAICompatibleEmbeddingClient(
        settings.model_copy(
            update={
                "embedding_model": vector_config["model"],
                "embedding_dimension": vector_config["dimension"],
                "llm_base_url": vector_config.get("base_url", settings.llm_base_url),
            }
        )
    )
    try:
        vector = (await client.embed_texts([q.strip()]))[0]
        results = await chunk_store.search(
            vector, run_ids=run_ids, kb_id=knowledge_base_id, document_id=document_id, limit=top_k
        )
    except EmbeddingError as exc:
        raise HTTPException(400, str(exc)) from exc
    return SearchResponse(
        query=q.strip(),
        current_generation_id=run_ids[0] if len(run_ids) == 1 else None,
        active_run_ids=run_ids,
        results=results,
    )
