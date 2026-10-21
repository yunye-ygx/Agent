from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas_v2 import KnowledgeBaseCreate, KnowledgeBaseResponse
from app.db import mysql_repository as repository
from app.db.mysql_session import get_mysql_session

router = APIRouter(prefix="/api/v1/knowledge-bases", tags=["knowledge-bases"])


def _serialize(item) -> KnowledgeBaseResponse:
    return KnowledgeBaseResponse(
        id=item.id,
        name=item.name,
        description=item.description,
        status=item.status,
        document_count=item.document_count,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.post("", response_model=KnowledgeBaseResponse, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    session: AsyncSession = Depends(get_mysql_session),
) -> KnowledgeBaseResponse:
    if not payload.name.strip():
        raise HTTPException(status_code=422, detail="知识库名称不能为空")
    item = await repository.create_knowledge_base(
        session, name=payload.name, description=payload.description
    )
    await session.commit()
    await session.refresh(item)
    return _serialize(item)


@router.get("", response_model=list[KnowledgeBaseResponse])
async def list_knowledge_bases(
    session: AsyncSession = Depends(get_mysql_session),
) -> list[KnowledgeBaseResponse]:
    return [_serialize(item) for item in await repository.list_knowledge_bases(session)]


@router.get("/{knowledge_base_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(
    knowledge_base_id: str,
    session: AsyncSession = Depends(get_mysql_session),
) -> KnowledgeBaseResponse:
    item = await repository.get_knowledge_base(session, knowledge_base_id)
    if item is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return _serialize(item)
