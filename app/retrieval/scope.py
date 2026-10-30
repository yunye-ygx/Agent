"""Resolve an immutable publication scope, then release SQL before model requests."""

from fastapi import HTTPException
from sqlalchemy import select

from app.core.config import settings
from app.db import mysql_repository as repository
from app.db.mysql_models import Document, Generation
from app.rag.embedding import OpenAICompatibleEmbeddingClient
from app.rag.run_creation import embedding_identity
from app.retrieval.rerank import RerankClient


async def authorize_snapshot(session, scope):
    # Deliberate extension point: real principal/KB/document ACL checks belong here
    # and in resolve_scope. A changed publication pointer doesn't invalidate a snapshot.
    if await repository.get_knowledge_base(session, scope["kb_id"]) is None:
        raise HTTPException(403, "知识库已不可访问")
    result = await session.execute(
        select(Document.id).where(
            Document.knowledge_base_id == scope["kb_id"],
            Document.status == "ACTIVE",
            Document.id.in_(list(scope["doc_runs"])),
        )
    )
    if set(result.scalars().all()) != set(scope["doc_runs"]):
        raise HTTPException(403, "快照中的文档已删除或不可访问")


async def resolve_scope(session, request):
    kb = await repository.get_knowledge_base(session, request.knowledge_base_id)
    if kb is None:
        raise HTTPException(404, "知识库不存在")
    query = select(Document).where(Document.knowledge_base_id == kb.id, Document.status == "ACTIVE")
    if request.document_id:
        query = query.where(Document.id == request.document_id)
    docs = list((await session.execute(query.order_by(Document.id).limit(2001))).scalars().all())
    if request.document_id and not docs:
        raise HTTPException(404, "文档不属于当前知识库或已删除")
    if len(docs) > 2000:
        raise HTTPException(422, "本版一次支持最多 2000 个文档，请指定文档检索")
    runs = list(
        (
            await session.execute(
                select(Generation).where(
                    Generation.id.in_([d.active_run_id for d in docs if d.active_run_id]),
                    Generation.status == "SUCCESS",
                    Generation.artifacts_available.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    by_id = {r.id: r for r in runs}
    eligible = [
        d for d in docs if d.active_run_id in by_id and by_id[d.active_run_id].document_id == d.id
    ]
    if not eligible:
        raise HTTPException(409, "当前范围没有完整发布的 active_run，可先完成离线建库")
    identity = embedding_identity(by_id[eligible[0].active_run_id].config_snapshot)
    if any(
        embedding_identity(by_id[d.active_run_id].config_snapshot) != identity for d in eligible
    ):
        raise HTTPException(409, "有效版本的 embedding 配置不兼容，无法混合检索")
    cfg = by_id[eligible[0].active_run_id].config_snapshot.get("embedding", {})
    if not cfg.get("model") or not isinstance(cfg.get("dimension"), int) or cfg["dimension"] <= 0:
        raise HTTPException(409, "有效版本缺少 embedding 模型或维度信息")
    return {
        "kb_id": kb.id,
        "doc_runs": {d.id: d.active_run_id for d in eligible},
        "titles": {d.id: d.filename for d in eligible},
        "embedding": {k: cfg.get(k) for k in ("model", "dimension", "base_url")},
        "warnings": [f"{len(docs) - len(eligible)} 个文档尚无完整有效版本，已排除"]
        if len(eligible) != len(docs)
        else [],
    }


async def resolve_clients(session, request, scope):
    configs = await repository.list_model_configurations(session)
    cfg = scope["embedding"]
    base = (cfg.get("base_url") or settings.llm_base_url or "").rstrip("/")
    saved = next(
        (
            c
            for c in configs
            if c.embedding_model == cfg["model"]
            and c.embedding_dimension == cfg["dimension"]
            and c.base_url.rstrip("/") == base
        ),
        None,
    )
    key = (
        saved.api_key
        if saved
        else (settings.llm_api_key if base == (settings.llm_base_url or "").rstrip("/") else None)
    )
    if not key or not base:
        raise HTTPException(
            409, "未找到与离线版本一致的 Embedding 连接，请在模型配置中保存匹配的模型、维度和地址"
        )
    embedding = OpenAICompatibleEmbeddingClient(
        settings.model_copy(
            update={
                "llm_base_url": base,
                "llm_api_key": key,
                "embedding_model": cfg["model"],
                "embedding_dimension": cfg["dimension"],
            }
        )
    )
    rankers = [c for c in configs if "Rerank" in (c.capabilities or [])]
    selected = (
        next((c for c in rankers if c.id == request.rerank_config_id), None)
        if request.rerank_config_id
        else (rankers[0] if rankers else None)
    )
    if request.scoring_mode == "vector":
        return embedding, None, None
    if (request.rerank_config_id or request.scoring_mode == "rerank") and selected is None:
        raise HTTPException(422, "指定的 Rerank 配置不存在或不支持 Rerank")
    if selected is None:
        return embedding, None, None
    return (
        embedding,
        RerankClient(selected.base_url, selected.api_key, selected.chat_model),
        {
            "configuration_id": selected.id,
            "model": selected.chat_model,
            "profile": "relevance-0-1-v1",
            "max_document_chars": 2000,
            "batch_size": 32,
        },
    )
