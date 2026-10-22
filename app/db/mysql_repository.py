from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.mysql_models import (
    Document,
    DocumentStatus,
    Generation,
    GenerationStatus,
    KnowledgeBase,
    KnowledgeBaseStatus,
    ModelConfiguration,
    Task,
    TaskChunkRef,
    TaskDispatchOutbox,
    TaskStatus,
)

TERMINAL_TASK_STATUSES = {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELED}


def utc_now() -> datetime:
    return datetime.now(UTC)


def is_reusable_generation(
    generation: Generation,
    parse_fingerprint: str,
    *,
    now: datetime | None = None,
) -> bool:
    if generation.artifacts_available is False:
        return False
    if generation.parse_fingerprint != parse_fingerprint:
        return False
    status = (
        generation.status.value
        if isinstance(generation.status, GenerationStatus)
        else generation.status
    )
    if status == GenerationStatus.SUCCESS.value:
        return True
    if status not in {GenerationStatus.FAILED.value, GenerationStatus.CANCELED.value}:
        return False
    if generation.reuse_expires_at is None:
        return False
    current_time = now or utc_now()
    expires_at = generation.reuse_expires_at
    if expires_at.tzinfo is None and current_time.tzinfo is not None:
        expires_at = expires_at.replace(tzinfo=current_time.tzinfo)
    return expires_at > current_time


async def list_reusable_generations(
    session: AsyncSession,
    document_id: str,
    parse_fingerprint: str,
    *,
    now: datetime | None = None,
) -> list[Generation]:
    result = await session.execute(
        select(Generation)
        .where(
            Generation.document_id == document_id,
            Generation.parse_fingerprint == parse_fingerprint,
        )
        .order_by(Generation.version_no.desc())
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return [
        generation
        for generation in result.scalars().all()
        if is_reusable_generation(generation, parse_fingerprint, now=now)
    ]


async def list_model_configurations(session: AsyncSession) -> list[ModelConfiguration]:
    result = await session.execute(
        select(ModelConfiguration).order_by(
            ModelConfiguration.is_default.desc(), ModelConfiguration.updated_at.desc()
        )
    )
    return list(result.scalars().all())


async def get_model_configuration(
    session: AsyncSession, configuration_id: str
) -> ModelConfiguration | None:
    return await session.get(ModelConfiguration, configuration_id)


async def _clear_default_model_configuration(session: AsyncSession) -> None:
    await session.execute(
        update(ModelConfiguration)
        .where(ModelConfiguration.is_default.is_(True))
        .values(is_default=False)
    )


async def create_model_configuration(
    session: AsyncSession,
    *,
    provider_key: str,
    provider_name: str,
    display_name: str,
    base_url: str,
    api_key: str,
    chat_model: str,
    embedding_model: str | None,
    embedding_dimension: int | None,
    capabilities: list[str],
    is_default: bool,
) -> ModelConfiguration:
    has_existing = (
        await session.execute(select(ModelConfiguration.id).limit(1))
    ).scalar_one_or_none()
    if is_default or not has_existing:
        await _clear_default_model_configuration(session)
        is_default = True
    item = ModelConfiguration(
        provider_key=provider_key,
        provider_name=provider_name,
        display_name=display_name,
        base_url=base_url,
        api_key=api_key,
        chat_model=chat_model,
        embedding_model=embedding_model,
        embedding_dimension=embedding_dimension,
        capabilities=capabilities,
        is_default=is_default,
    )
    session.add(item)
    await session.flush()
    return item


async def update_model_configuration(
    session: AsyncSession,
    item: ModelConfiguration,
    *,
    provider_key: str,
    provider_name: str,
    display_name: str,
    base_url: str,
    api_key: str | None,
    chat_model: str,
    embedding_model: str | None,
    embedding_dimension: int | None,
    capabilities: list[str],
    is_default: bool,
) -> ModelConfiguration:
    if is_default:
        await _clear_default_model_configuration(session)
    elif item.is_default:
        is_default = True
    item.provider_key = provider_key
    item.provider_name = provider_name
    item.display_name = display_name
    item.base_url = base_url
    if api_key:
        item.api_key = api_key
    item.chat_model = chat_model
    item.embedding_model = embedding_model
    item.embedding_dimension = embedding_dimension
    item.capabilities = capabilities
    item.is_default = is_default
    await session.flush()
    return item


async def delete_model_configuration(session: AsyncSession, item: ModelConfiguration) -> None:
    was_default = item.is_default
    await session.delete(item)
    await session.flush()
    if was_default:
        replacement = (
            await session.execute(
                select(ModelConfiguration).order_by(ModelConfiguration.updated_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if replacement:
            replacement.is_default = True


async def create_knowledge_base(
    session: AsyncSession, *, name: str, description: str | None = None
) -> KnowledgeBase:
    knowledge_base = KnowledgeBase(name=name.strip(), description=description)
    session.add(knowledge_base)
    await session.flush()
    return knowledge_base


async def list_knowledge_bases(session: AsyncSession) -> list[KnowledgeBase]:
    result = await session.execute(
        select(KnowledgeBase)
        .where(KnowledgeBase.status == KnowledgeBaseStatus.ACTIVE)
        .order_by(KnowledgeBase.updated_at.desc())
    )
    return list(result.scalars().all())


async def get_knowledge_base(
    session: AsyncSession, knowledge_base_id: str, *, lock=False
) -> KnowledgeBase | None:
    query = select(KnowledgeBase).where(
        KnowledgeBase.id == knowledge_base_id,
        KnowledgeBase.status == KnowledgeBaseStatus.ACTIVE,
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    result = await session.execute(query)
    return result.scalar_one_or_none()


class KnowledgeBaseDeleteConfirmationRequired(ValueError):
    def __init__(self, document_count: int, knowledge_bases=None, skipped=None) -> None:
        self.document_count = document_count
        self.knowledge_bases = knowledge_bases or []
        self.skipped = skipped or []
        super().__init__("知识库中包含文档，需要确认删除")


async def rename_knowledge_base(session: AsyncSession, knowledge_base: KnowledgeBase, *, name: str):
    knowledge_base.name = name.strip()
    await session.flush()
    return knowledge_base


async def prepare_knowledge_base_deletions(
    session: AsyncSession, knowledge_base_ids: list[str], *, confirm_with_documents: bool
) -> tuple[list[str], list[dict[str, str]]]:
    """Mark eligible knowledge bases for durable background cleanup."""
    ids = list(dict.fromkeys(knowledge_base_ids))
    if not ids:
        return [], []

    knowledge_bases = list(
        (
            await session.execute(
                select(KnowledgeBase)
                .where(KnowledgeBase.id.in_(ids))
                .order_by(KnowledgeBase.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    kb_by_id = {item.id: item for item in knowledge_bases}
    documents = list(
        (
            await session.execute(
                select(Document)
                .where(Document.knowledge_base_id.in_(ids))
                .order_by(Document.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    docs_by_kb: dict[str, list[Document]] = {item_id: [] for item_id in ids}
    for document in documents:
        docs_by_kb[document.knowledge_base_id].append(document)

    document_ids = [document.id for document in documents]
    generations = []
    tasks = []
    if document_ids:
        generations = list(
            (
                await session.execute(
                    select(Generation)
                    .where(Generation.document_id.in_(document_ids))
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        tasks = list(
            (
                await session.execute(
                    select(Task).where(
                        Task.document_id.in_(document_ids),
                        Task.status.in_(
                            [
                                TaskStatus.WAITING_PUBLISH,
                                TaskStatus.PENDING,
                                TaskStatus.RUNNING,
                                TaskStatus.CANCELLING,
                            ]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
    generations_by_doc: dict[str, list[Generation]] = {}
    for generation in generations:
        generations_by_doc.setdefault(generation.document_id, []).append(generation)
    active_doc_ids = {task.document_id for task in tasks}
    active_doc_ids.update(
        generation.document_id
        for generation in generations
        if generation.status in {GenerationStatus.RUNNING, GenerationStatus.CANCELLING}
    )

    skipped: list[dict[str, str]] = []
    eligible: list[KnowledgeBase] = []
    confirmation_required = []
    for knowledge_base_id in ids:
        knowledge_base = kb_by_id.get(knowledge_base_id)
        if knowledge_base is None or knowledge_base.status == KnowledgeBaseStatus.DELETED:
            skipped.append({"id": knowledge_base_id, "reason": "知识库不存在"})
            continue
        if knowledge_base.status == KnowledgeBaseStatus.DELETING:
            # A prior request already persisted this work; accept it idempotently.
            eligible.append(knowledge_base)
            continue
        kb_documents = docs_by_kb[knowledge_base_id]
        if any(document.id in active_doc_ids for document in kb_documents):
            skipped.append({
                "id": knowledge_base_id,
                "reason": "知识库中有文档正在解析，已跳过",
            })
            continue
        if kb_documents and not confirm_with_documents:
            confirmation_required.append({
                "id": knowledge_base.id,
                "name": knowledge_base.name,
                "document_count": len(kb_documents),
            })
            continue
        eligible.append(knowledge_base)

    if confirmation_required:
        raise KnowledgeBaseDeleteConfirmationRequired(
            sum(item["document_count"] for item in confirmation_required),
            knowledge_bases=confirmation_required,
            skipped=skipped,
        )

    for knowledge_base in eligible:
        if knowledge_base.status == KnowledgeBaseStatus.DELETING:
            continue
        knowledge_base.status = KnowledgeBaseStatus.DELETING
        kb_documents = docs_by_kb[knowledge_base.id]
        for document in kb_documents:
            document.status = DocumentStatus.DELETED
            document.current_generation_id = None
            document.building_generation_id = None
            for generation in generations_by_doc.get(document.id, []):
                generation.artifacts_available = False

    await session.flush()
    return [item.id for item in eligible], skipped


async def complete_knowledge_base_deletion(session: AsyncSession, knowledge_base_id: str) -> None:
    document_ids = list(
        (
            await session.execute(
                select(Document.id).where(Document.knowledge_base_id == knowledge_base_id)
            )
        )
        .scalars()
        .all()
    )
    if document_ids:
        generation_ids = list(
            (
                await session.execute(
                    select(Generation.id).where(Generation.document_id.in_(document_ids))
                )
            )
            .scalars()
            .all()
        )
        task_ids = list(
            (
                await session.execute(
                    select(Task.id).where(Task.document_id.in_(document_ids))
                )
            )
            .scalars()
            .all()
        )
        if task_ids:
            await session.execute(
                delete(TaskDispatchOutbox).where(TaskDispatchOutbox.task_id.in_(task_ids))
            )
        if task_ids or generation_ids:
            from sqlalchemy import or_

            await session.execute(
                delete(TaskChunkRef).where(
                    or_(
                        TaskChunkRef.task_id.in_(task_ids) if task_ids else False,
                        TaskChunkRef.generation_id.in_(generation_ids)
                        if generation_ids
                        else False,
                    )
                )
            )
        if task_ids:
            await session.execute(delete(Task).where(Task.id.in_(task_ids)))
        if generation_ids:
            await session.execute(delete(Generation).where(Generation.id.in_(generation_ids)))
        await session.execute(delete(Document).where(Document.id.in_(document_ids)))
    await session.execute(
        delete(KnowledgeBase).where(
            KnowledgeBase.id == knowledge_base_id,
            KnowledgeBase.status == KnowledgeBaseStatus.DELETING,
        )
    )
    await session.commit()


async def get_document(
    session: AsyncSession, document_id: str, *, lock: bool = False
) -> Document | None:
    query = select(Document).where(
        Document.id == document_id,
        Document.status == DocumentStatus.ACTIVE,
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    result = await session.execute(query)
    return result.scalar_one_or_none()


async def find_document_by_hash(
    session: AsyncSession, knowledge_base_id: str, file_hash: str
) -> Document | None:
    result = await session.execute(
        select(Document).where(
            Document.knowledge_base_id == knowledge_base_id,
            Document.file_hash == file_hash,
            Document.status == DocumentStatus.ACTIVE,
        )
    )
    return result.scalar_one_or_none()


async def create_document(
    session: AsyncSession,
    *,
    knowledge_base: KnowledgeBase,
    filename: str,
    object_key: str,
    file_hash: str,
    file_size: int,
    mime_type: str,
) -> Document:
    document = Document(
        knowledge_base_id=knowledge_base.id,
        filename=filename,
        object_key=object_key,
        file_hash=file_hash,
        file_size=file_size,
        mime_type=mime_type,
    )
    session.add(document)
    knowledge_base.document_count += 1
    await session.flush()
    return document


async def delete_document(session: AsyncSession, document: Document) -> Document:
    if document.current_run_id:
        latest = await get_generation(session, document.current_run_id, lock=True)
        if latest and latest.status in {GenerationStatus.RUNNING, GenerationStatus.CANCELLING}:
            raise ValueError("文档正在解析，请先取消解析")

    document.status = DocumentStatus.DELETED
    document.current_generation_id = None

    knowledge_base = await get_knowledge_base(session, document.knowledge_base_id, lock=True)
    if knowledge_base:
        knowledge_base.document_count = max(0, knowledge_base.document_count - 1)

    await session.flush()
    return document


async def next_generation_version(session: AsyncSession, document_id: str) -> int:
    result = await session.execute(
        select(Generation.version_no)
        .where(Generation.document_id == document_id)
        .order_by(Generation.version_no.desc())
        .limit(1)
        .with_for_update()
    )
    return int(result.scalar() or 0) + 1


# Task control plane is split out to keep provider/document CRUD independent.
from app.db.task_lifecycle import (  # noqa: F401
    ExecutionLost,
    assert_execution,
    claim_task,
    complete_task,
    confirm_dispatch,
    create_generation,
    create_outbox_records,
    fail_task,
    find_reusable_task,
    finish_cancellations,
    get_generation,
    get_task,
    is_task_lease_expired,
    lock_run,
    lock_task,
    mark_tasks_queued,
    pin_task_output,
    publish_generation,
    reclaim_expired_running_tasks,
    refresh_generation_status,
    request_generation_cancel,
    reserve_dispatch,
    schedule_task_retry,
    touch_task_heartbeat,
    valid_run,
)
