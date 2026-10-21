from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, synonym


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class KnowledgeBaseStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    DELETED = "DELETED"


class DocumentStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    DELETED = "DELETED"


class GenerationStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    BUILDING = RUNNING  # compatibility name for the existing API
    CANCELLING = "CANCELLING"
    CANCELING = CANCELLING
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    CANCELED = CANCELLED


class TaskStatus(str, enum.Enum):
    WAITING_PUBLISH = "WAITING_PUBLISH"
    PENDING = "PENDING"
    QUEUED = PENDING
    RUNNING = "RUNNING"
    RETRY_WAIT = WAITING_PUBLISH
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLING = "CANCELLING"
    CANCEL_REQUESTED = CANCELLING
    CANCELLED = "CANCELLED"
    CANCELED = CANCELLED


class TaskKind(str, enum.Enum):
    PARSE = "PARSE"
    COPY = "COPY"


class TaskCompletionType(str, enum.Enum):
    EXECUTED = "EXECUTED"
    REUSED = "REUSED"


class RelationType(str, enum.Enum):
    PRODUCED = "PRODUCED"
    REUSED = "REUSED"


class OutboxStatus(str, enum.Enum):
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    DEAD = "DEAD"


class ModelConfiguration(Base):
    """A user-managed provider/model connection usable by later workflows."""

    __tablename__ = "model_configurations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    provider_name: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_key: Mapped[str] = mapped_column(String(2048), nullable=False)
    chat_model: Mapped[str] = mapped_column(String(255), nullable=False)
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capabilities: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[KnowledgeBaseStatus] = mapped_column(
        String(32), nullable=False, default=KnowledgeBaseStatus.ACTIVE
    )
    document_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    documents: Mapped[list[Document]] = relationship(back_populates="knowledge_base")


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "file_hash", name="uk_document_kb_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    knowledge_base_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size: Mapped[int] = mapped_column(nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    page_count: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[DocumentStatus] = mapped_column(
        String(32), nullable=False, default=DocumentStatus.ACTIVE
    )
    current_generation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    building_generation_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    # Keep the physical column names for a non-destructive migration. The former
    # building pointer now retains the latest run even after that run terminates.
    active_run_id = synonym("current_generation_id")
    current_run_id = synonym("building_generation_id")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="documents")
    generations: Mapped[list[Generation]] = relationship(back_populates="document")
    tasks: Mapped[list[Task]] = relationship(back_populates="document")


class Generation(Base):
    __tablename__ = "generations"
    __table_args__ = (
        UniqueConstraint("document_id", "version_no", name="uk_generation_document_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[GenerationStatus] = mapped_column(
        String(32), nullable=False, default=GenerationStatus.BUILDING
    )
    parse_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    config_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    file_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    file_location_snapshot: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    artifacts_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    expected_task_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_task_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_task_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    canceled_task_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reuse_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_reason: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="generations")
    tasks: Mapped[list[Task]] = relationship(back_populates="generation")
    chunk_refs: Mapped[list[TaskChunkRef]] = relationship(back_populates="generation")

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("status", GenerationStatus.BUILDING)
        super().__init__(**kwargs)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("generation_id", "task_no", name="uk_task_generation_no"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_id: Mapped[str] = mapped_column(
        ForeignKey("generations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_no: Mapped[int] = mapped_column(Integer, nullable=False)
    core_page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    core_page_end: Mapped[int] = mapped_column(Integer, nullable=False)
    context_page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[TaskStatus] = mapped_column(
        String(32), nullable=False, default=TaskStatus.PENDING
    )
    kind: Mapped[TaskKind] = mapped_column(String(16), nullable=False, default=TaskKind.PARSE)
    source_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    output_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    dispatch_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    execution_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=func.now()
    )
    completion_type: Mapped[TaskCompletionType | None] = mapped_column(String(32), nullable=True)
    parse_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_chunk_count = synonym("chunk_count")
    # Legacy columns retained for migration/API compatibility, no longer used
    # as execution authority. attempt_count includes the first claim and crashes.
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retry: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    generation: Mapped[Generation] = relationship(back_populates="tasks")
    document: Mapped[Document] = relationship(back_populates="tasks")
    chunk_refs: Mapped[list[TaskChunkRef]] = relationship(back_populates="task")
    outbox_records: Mapped[list[TaskDispatchOutbox]] = relationship(back_populates="task")

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("status", TaskStatus.WAITING_PUBLISH)
        super().__init__(**kwargs)


class TaskChunkRef(Base):
    __tablename__ = "task_chunk_refs"
    __table_args__ = (UniqueConstraint("task_id", "chunk_id", name="uk_task_chunk"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    generation_id: Mapped[str] = mapped_column(
        ForeignKey("generations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    relation_type: Mapped[RelationType] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped[Task] = relationship(back_populates="chunk_refs")
    generation: Mapped[Generation] = relationship(back_populates="chunk_refs")


class TaskDispatchOutbox(Base):
    __tablename__ = "task_dispatch_outbox"
    __table_args__ = (UniqueConstraint("task_id", "message_type", name="uk_outbox_task_type"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    message_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[OutboxStatus] = mapped_column(
        String(32), nullable=False, default=OutboxStatus.PENDING
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    task: Mapped[Task] = relationship(back_populates="outbox_records")


# A ParseRun is represented by the existing generations table.
ParseRun = Generation
