from importlib.metadata import PackageNotFoundError, version

from fastapi import HTTPException
from sqlalchemy import select

from app.core.config import settings
from app.db import mysql_repository as repository
from app.db.mysql_models import (
    GenerationStatus,
    KnowledgeBase,
    TaskKind,
)
from app.rag.document_parser import file_type_for
from app.rag.fingerprint import build_parse_fingerprint
from app.rag.structured_chunker import StructuredChunker


def build_config_snapshot(document):
    file_type = file_type_for(document.filename)
    try:
        parser_version = version("mineru")
    except PackageNotFoundError:
        parser_version = "unavailable"
    return {
        "file_type": file_type,
        "parser": {
            "name": "mineru" if file_type == "pdf" else "utf8-text",
            "version": settings.mineru_api_version if settings.mineru_base_url else parser_version,
            "mode": "remote" if settings.mineru_base_url else "local",
            "base_url": settings.mineru_base_url,
            "api_version": settings.mineru_api_version,
            "command": settings.mineru_command,
            "backend": settings.mineru_backend,
            "method": settings.mineru_method,
            "timeout_seconds": settings.mineru_timeout_seconds,
        },
        "chunker": {
            "version": StructuredChunker.strategy_version,
            "size": settings.chunking_default_size,
            "overlap": settings.chunking_default_overlap,
        },
        "embedding": {
            "model": settings.embedding_model,
            "dimension": settings.embedding_dimension,
            "base_url": settings.llm_base_url,
            "batch_size": settings.embedding_batch_size,
        },
        "task_page_size": max(1, settings.task_page_size),
        "page_range": {"start": 0, "end": document.page_count if file_type == "pdf" else 0},
    }


def embedding_identity(config):
    value = config.get("embedding", {})
    return value.get("model"), value.get("dimension"), value.get("base_url")


async def create_run(document_id, session):
    # Serialize KB model compatibility decisions, then follow the common Document
    # lock boundary for creating/cancelling/publishing a run.
    initial = await repository.get_document(session, document_id)
    if initial is None:
        raise HTTPException(404, "文档不存在")
    kb = (
        await session.execute(
            select(KnowledgeBase)
            .where(KnowledgeBase.id == initial.knowledge_base_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    document = await repository.get_document(session, document_id, lock=True)
    if document is None:
        raise HTTPException(404, "文档不存在")
    config = build_config_snapshot(document)
    if not config.get("embedding", {}).get("model") or config["embedding"].get("dimension", 0) <= 0:
        raise HTTPException(422, "需要有效的 embedding 模型与维度配置")
    if document.current_run_id:
        current = await repository.get_generation(session, document.current_run_id, lock=True)
        if current and current.status in {GenerationStatus.RUNNING, GenerationStatus.CANCELLING}:
            raise HTTPException(409, "该文档已有有效解析批次，请先取消")
    # A knowledge base must not silently mix embedding spaces. A full model
    # migration requires a separate design, not a run-pointer change.
    if kb.embedding_model is None:
        kb.embedding_model, kb.embedding_dimension, kb.embedding_base_url = embedding_identity(
            config
        )
    elif (kb.embedding_model, kb.embedding_dimension, kb.embedding_base_url) != embedding_identity(
        config
    ):
        raise HTTPException(409, "知识库已固定不同 embedding 模型/维度；需要整体迁移方案")
    if config["file_type"] == "pdf":
        if not document.page_count:
            raise HTTPException(422, "PDF 没有有效页数")
        size = config["task_page_size"]
        ranges = [
            {
                "task_no": i,
                "core_page_start": start,
                "core_page_end": min(start + size, document.page_count),
            }
            for i, start in enumerate(range(0, document.page_count, size), 1)
        ]
    else:
        # [0,0) explicitly marks the entire non-paged document.
        ranges = [{"task_no": 1, "core_page_start": 0, "core_page_end": 0}]
    fingerprint = build_parse_fingerprint({"file_version": document.file_hash, **config})
    run, tasks = await repository.create_generation(
        session,
        document=document,
        parse_fingerprint=fingerprint,
        config_snapshot=config,
        task_ranges=ranges,
    )
    candidates = await repository.list_reusable_generations(session, document.id, fingerprint)
    for task in tasks:
        for candidate in candidates:
            if (
                candidate.id == run.id
                or candidate.file_version != run.file_version
                or candidate.config_snapshot != config
            ):
                continue
            source = await repository.find_reusable_task(
                session, generation_id=candidate.id, task=task
            )
            if source and source.result_chunk_count > 0:
                task.kind, task.source_task_id = TaskKind.COPY, source.id
                break
    # No ES network call or pre-marked SUCCESS in this transaction. The persisted
    # source reference protects candidates from cleanup; Worker verifies completeness.
    await repository.create_outbox_records(session, tasks)
    await session.commit()
    return {
        "generation_id": run.id,
        "run_id": run.id,
        "status": run.status,
        "task_ids": [t.id for t in tasks],
    }
