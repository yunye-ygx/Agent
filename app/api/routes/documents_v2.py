from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas_v2 import (
    ChunkResponse,
    DocumentDetailResponse,
    DocumentResponse,
    GenerationResponse,
    TaskRangeResponse,
)
from app.auth.dependencies import (
    get_current_principal,
    require_admin,
    require_csrf_for_write,
)
from app.business import knowledge as knowledge_service
from app.business.schemas import DocumentSupportResponse, DocumentSupportUpdate
from app.core.config import settings
from app.db import mysql_repository as repository
from app.db.mysql_models import Generation, GenerationStatus, Task
from app.db.mysql_session import get_mysql_session
from app.rag.document_parser import file_type_for
from app.rag.pdf_metadata import get_pdf_page_count
from app.rag.run_creation import create_run
from app.storage.elasticsearch_chunks import ChunkStore
from app.storage.minio_storage import MinioStorage

router = APIRouter(
    prefix="/api/v1",
    tags=["documents"],
    dependencies=[Depends(require_admin), Depends(require_csrf_for_write)],
)
storage = MinioStorage()
chunk_store = ChunkStore()


def _document_response(item, *, building_id=None) -> DocumentResponse:
    return DocumentResponse(
        id=item.id,
        knowledge_base_id=item.knowledge_base_id,
        filename=item.filename,
        file_size=item.file_size,
        mime_type=item.mime_type,
        status=item.status,
        support_open=bool(item.support_open),
        current_generation_id=item.current_generation_id,
        building_generation_id=building_id,
        active_run_id=item.active_run_id,
        current_run_id=item.current_run_id,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _generation_response(item: Generation) -> GenerationResponse:
    return GenerationResponse(
        id=item.id,
        version_no=item.version_no,
        status=item.status,
        expected_task_count=item.expected_task_count,
        completed_task_count=item.completed_task_count,
        failed_task_count=item.failed_task_count,
        canceled_task_count=item.canceled_task_count,
        created_at=item.created_at,
        finished_at=item.finished_at,
        chunk_count=item.chunk_count,
        published_at=item.published_at,
    )


def _task_response(item: Task) -> TaskRangeResponse:
    return TaskRangeResponse(
        id=item.id,
        task_no=item.task_no,
        status=item.status,
        completion_type=item.completion_type,
        chunk_count=item.chunk_count,
        retry_count=item.retry_count,
        core_page_start=item.core_page_start,
        core_page_end=item.core_page_end,
        kind=item.kind,
        attempt_count=item.attempt_count,
        max_attempts=item.max_attempts,
        dispatch_seq=item.dispatch_seq,
        last_error=item.last_error_message,
    )


@router.post(
    "/knowledge-bases/{knowledge_base_id}/documents",
    response_model=DocumentResponse,
    status_code=201,
)
async def upload_document(
    knowledge_base_id: str,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_mysql_session),
) -> DocumentResponse:
    # Serialize uploads with knowledge-base deletion; a delete tombstone must
    # not miss a document whose source object is being uploaded concurrently.
    knowledge_base = await repository.get_knowledge_base(session, knowledge_base_id, lock=True)
    if knowledge_base is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    content = await file.read(settings.chunking_max_file_bytes + 1)
    if not content:
        raise HTTPException(status_code=422, detail="文件不能为空")
    filename = Path(file.filename or "document.bin").name
    if len(content) > settings.chunking_max_file_bytes:
        raise HTTPException(413, "文件超过资源上限")
    try:
        file_type = file_type_for(filename)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    page_count = get_pdf_page_count(content) if file_type == "pdf" else None
    if file_type == "pdf" and not page_count:
        raise HTTPException(422, "PDF 文件无效或没有页面")
    file_hash = hashlib.sha256(content).hexdigest()
    existing = await repository.find_document_by_hash(session, knowledge_base_id, file_hash)
    if existing:
        return _document_response(existing)
    document_id = hashlib.sha256(f"{knowledge_base_id}|{file_hash}".encode()).hexdigest()[:36]
    object_key = f"knowledge-bases/{knowledge_base_id}/documents/{document_id}/source/{filename}"
    await storage.put_bytes(
        settings.minio_files_bucket,
        object_key,
        content,
        file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream",
    )
    document = await repository.create_document(
        session,
        knowledge_base=knowledge_base,
        filename=filename,
        object_key=object_key,
        file_hash=file_hash,
        file_size=len(content),
        mime_type=file.content_type or "application/octet-stream",
    )
    document.page_count = page_count
    await session.commit()
    await session.refresh(document)
    return _document_response(document)


@router.get("/knowledge-bases/{knowledge_base_id}/documents", response_model=list[DocumentResponse])
async def list_documents(
    knowledge_base_id: str,
    session: AsyncSession = Depends(get_mysql_session),
) -> list[DocumentResponse]:
    from sqlalchemy import select

    from app.db.mysql_models import Document, DocumentStatus

    result = await session.execute(
        select(Document)
        .where(
            Document.knowledge_base_id == knowledge_base_id,
            Document.status == DocumentStatus.ACTIVE,
        )
        .order_by(Document.updated_at.desc())
    )
    responses = []
    for item in result.scalars().all():
        latest = (
            await repository.get_generation(session, item.current_run_id)
            if item.current_run_id
            else None
        )
        building_id = (
            latest.id
            if latest and latest.status in {GenerationStatus.RUNNING, GenerationStatus.CANCELLING}
            else None
        )
        responses.append(_document_response(item, building_id=building_id))
    return responses


@router.get("/documents/{document_id}", response_model=DocumentDetailResponse)
async def get_document_detail(
    document_id: str,
    session: AsyncSession = Depends(get_mysql_session),
) -> DocumentDetailResponse:
    document = await repository.get_document(session, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    current = (
        await repository.get_generation(session, document.current_generation_id)
        if document.current_generation_id
        else None
    )
    building = (
        await repository.get_generation(session, document.building_generation_id)
        if document.building_generation_id
        else None
    )
    latest = building
    if building and building.status not in {GenerationStatus.RUNNING, GenerationStatus.CANCELLING}:
        building = None
    return DocumentDetailResponse(
        **_document_response(document, building_id=building.id if building else None).model_dump(),
        current_generation=_generation_response(current) if current else None,
        building_generation=_generation_response(building) if building else None,
        latest_run=_generation_response(latest) if latest else None,
    )


@router.patch("/documents/{document_id}/support", response_model=DocumentSupportResponse)
async def set_document_support(
    document_id: str,
    payload: DocumentSupportUpdate,
    session: AsyncSession = Depends(get_mysql_session),
    principal=Depends(get_current_principal),
) -> DocumentSupportResponse:
    data = await knowledge_service.set_document_support_open(
        session, principal, document_id, open_=payload.open
    )
    return DocumentSupportResponse(**data)


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: str, session: AsyncSession = Depends(get_mysql_session)
) -> Response:
    initial = await repository.get_document(session, document_id)
    if initial is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    # Parse creation also locks KB before Document. Counts must not invert that order.
    from sqlalchemy import select

    from app.db.mysql_models import KnowledgeBase

    await session.execute(
        select(KnowledgeBase)
        .where(KnowledgeBase.id == initial.knowledge_base_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    document = await repository.get_document(session, document_id, lock=True)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    try:
        await repository.delete_document(session, document)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await session.commit()
    return Response(status_code=204)


@router.get("/documents/{document_id}/file")
async def get_document_file(
    document_id: str, session: AsyncSession = Depends(get_mysql_session)
) -> Response:
    document = await repository.get_document(session, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    content = await storage.get_bytes(settings.minio_files_bucket, document.object_key)
    return Response(content=content, media_type=document.mime_type)


@router.post("/documents/{document_id}/parse")
async def parse_document(
    document_id: str, session: AsyncSession = Depends(get_mysql_session)
) -> dict:
    return await _create_generation(document_id, session, force_new=True)


@router.post("/documents/{document_id}/reparse")
async def reparse_document(
    document_id: str, session: AsyncSession = Depends(get_mysql_session)
) -> dict:
    return await _create_generation(document_id, session, force_new=True)


async def _create_generation(document_id: str, session: AsyncSession, *, force_new: bool) -> dict:
    return await create_run(document_id, session)


@router.post("/documents/{document_id}/cancel")
async def cancel_document(
    document_id: str, session: AsyncSession = Depends(get_mysql_session)
) -> dict:
    document = await repository.get_document(session, document_id, lock=True)
    if document is None or not document.building_generation_id:
        raise HTTPException(status_code=409, detail="当前没有正在构建的解析版本")
    try:
        generation = await repository.request_generation_cancel(session, document.current_run_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await repository.finish_cancellations(session, generation_id=generation.id)
    await session.commit()
    return {"generation_id": generation.id, "status": generation.status}


@router.get("/documents/{document_id}/status")
async def document_status(
    document_id: str, session: AsyncSession = Depends(get_mysql_session)
) -> DocumentDetailResponse:
    document = await repository.get_document(session, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return await get_document_detail(document_id, session)


@router.get("/documents/{document_id}/generations")
async def document_generations(
    document_id: str, session: AsyncSession = Depends(get_mysql_session)
) -> list[GenerationResponse]:
    from sqlalchemy import select

    result = await session.execute(
        select(Generation)
        .where(Generation.document_id == document_id)
        .order_by(Generation.version_no.desc())
    )
    return [_generation_response(item) for item in result.scalars().all()]


@router.get("/generations/{generation_id}/tasks", response_model=list[TaskRangeResponse])
async def generation_tasks(
    generation_id: str, session: AsyncSession = Depends(get_mysql_session)
) -> list[TaskRangeResponse]:
    from sqlalchemy import select

    result = await session.execute(
        select(Task).where(Task.generation_id == generation_id).order_by(Task.task_no)
    )
    return [_task_response(item) for item in result.scalars().all()]


@router.get("/documents/{document_id}/chunks", response_model=list[ChunkResponse])
async def document_chunks(
    document_id: str, session: AsyncSession = Depends(get_mysql_session)
) -> list[ChunkResponse]:
    document = await repository.get_document(session, document_id)
    if document is None or not document.current_generation_id:
        return []
    return await generation_chunks(document.current_generation_id, session)


@router.get("/generations/{generation_id}/chunks", response_model=list[ChunkResponse])
async def generation_chunks(
    generation_id: str, session: AsyncSession = Depends(get_mysql_session)
) -> list[ChunkResponse]:
    run = await repository.get_generation(session, generation_id)
    doc = await repository.get_document(session, run.document_id) if run else None
    if not doc or not run:
        raise HTTPException(404, "批次不存在")
    rows = await chunk_store.read_run_chunks(
        kb_id=doc.knowledge_base_id, doc_id=doc.id, run_id=run.id
    )
    return [
        ChunkResponse(
            chunk_id=chunk_id,
            content=value["content"],
            chunk_index=value["chunk_index"],
            page_start=value.get("page_start"),
            page_end=value.get("page_end"),
            metadata=value.get("metadata", {}),
            task_id=value["task_id"],
            generation_id=value["run_id"],
        )
        for chunk_id, value in sorted(
            rows, key=lambda row: (row[1]["task_id"], row[1]["chunk_index"])
        )
    ]
