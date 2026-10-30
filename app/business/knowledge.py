"""售后资料开放开关与检索用例。

设计（2026-10-26 调整）：documents 表只有一个 `support_open` 开关，不再维护
型号关联表。型号由 Agent 负责：提问前用账号下的设备型号限定问题，信息不足时
先向用户澄清。检索层只保证“未开放的文档不会被召回”。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.business.errors import Conflict, Forbidden, NotFound, Unavailable
from app.business.principal import Principal
from app.business.utils import iso_datetime
from app.db.mysql_models import Document, DocumentStatus, Generation, GenerationStatus
from app.retrieval.published_search import search_published_documents


def _status(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _published_run(document: Document, run: Generation | None) -> bool:
    return (
        run is not None
        and _status(run.status) == GenerationStatus.SUCCESS.value
        and bool(run.artifacts_available)
        and run.published_at is not None
    )


async def _document_and_run(
    session: AsyncSession, document_id: str, *, lock: bool = False
):
    statement = select(Document).where(Document.id == document_id)
    if lock:
        statement = statement.with_for_update()
    document = (await session.execute(statement)).scalar_one_or_none()
    if document is None or _status(document.status) != DocumentStatus.ACTIVE.value:
        raise NotFound("文档不存在或已删除")
    run = await session.get(Generation, document.active_run_id) if document.active_run_id else None
    return document, run


def _serialize(document: Document, run: Generation | None) -> dict:
    published = _published_run(document, run)
    return {
        "document_id": document.id,
        "filename": document.filename,
        "knowledge_base_id": document.knowledge_base_id,
        "document_status": _status(document.status),
        "support_open": bool(document.support_open),
        "searchable": bool(document.support_open and published),
        "version_no": run.version_no if published else None,
        "published_at": iso_datetime(run.published_at) if published else None,
    }


async def set_document_support_open(
    session: AsyncSession, principal: Principal, document_id: str, *, open_: bool
) -> dict:
    if not principal.is_admin:
        raise Forbidden("只有管理员可以调整售后资料开放")
    document, run = await _document_and_run(session, document_id, lock=True)
    if open_ and not _published_run(document, run):
        raise Conflict(
            "文档尚未完成建库发布，不能开放检索", code="KNOWLEDGE_NOT_PUBLISHED"
        )
    document.support_open = open_
    try:
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        raise Unavailable("资料开放状态未保存，请重试") from exc
    await session.refresh(document)
    return _serialize(document, run)


async def search_support_knowledge(
    session: AsyncSession,
    principal: Principal,
    *,
    query: str,
    top_k: int,
) -> dict:
    if not principal.can("knowledge:search"):
        raise Forbidden("无权检索售后资料")
    document_ids = list(
        (
            await session.execute(
                select(Document.id).where(
                    Document.support_open.is_(True),
                    Document.status == DocumentStatus.ACTIVE.value,
                )
            )
        )
        .scalars()
        .all()
    )
    if not document_ids:
        raise NotFound("暂未对客户开放售后资料")
    data = await search_published_documents(
        session,
        document_ids=document_ids,
        query=query,
        top_k=top_k,
    )
    return {
        "query": query,
        "results": data["results"],
        "eligible_documents": data["eligible_documents"],
        "excluded_documents": data["excluded_documents"],
    }