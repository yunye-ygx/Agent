from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.business.models import BusinessBase
from app.business.utils import new_id, utc_now


class Conversation(BusinessBase):
    __tablename__ = "chat_conversations"
    __table_args__ = (Index("ix_chat_owner_updated", "user_id", "updated_at", "id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("business_users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(120), default="新会话", nullable=False)
    next_sequence: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    active_turn: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class Message(BusinessBase):
    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uk_chat_message_sequence"),
        UniqueConstraint("run_id", "source_key", name="uk_chat_message_projection"),
        Index("ix_chat_turn", "conversation_id", "turn_id"),
        Index("ix_chat_message_run", "run_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("chat_conversations.id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    turn_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("chat_runs.id"), nullable=True)
    source_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(200))
    tool_name: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class Turn(BusinessBase):
    __tablename__ = "chat_turns"
    __table_args__ = (
        UniqueConstraint("conversation_id", "turn_no", name="uk_chat_turn_number"),
        UniqueConstraint(
            "conversation_id", "client_request_id", name="uk_chat_turn_request"
        ),
        Index("ix_chat_turn_status", "conversation_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("chat_conversations.id"), nullable=False
    )
    user_message_id: Mapped[str] = mapped_column(ForeignKey("chat_messages.id"), nullable=False)
    turn_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    latest_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_runs.id"), nullable=True
    )
    active_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_runs.id"), nullable=True
    )
    run_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_run_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Run(BusinessBase):
    __tablename__ = "chat_runs"
    __table_args__ = (
        UniqueConstraint("turn_id", "attempt_no", name="uk_chat_run_attempt"),
        Index("ix_chat_run_lease", "status", "lease_expires_at"),
        Index("ix_chat_run_queue", "status", "queued_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    turn_id: Mapped[str] = mapped_column(ForeignKey("chat_turns.id"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    retry_of_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_runs.id"), nullable=True
    )
    trigger_reason: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    agent_version: Mapped[str] = mapped_column(String(100), nullable=False, default="v1")
    checkpoint_thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    latest_checkpoint_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    checkpoint_schema_version: Mapped[str] = mapped_column(
        String(50), nullable=False, default="v1"
    )
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    recovery_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_recovery_count: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class ToolExecution(BusinessBase):
    __tablename__ = "chat_tool_executions"
    __table_args__ = (
        UniqueConstraint("run_id", "tool_call_id", name="uk_chat_tool_call"),
        Index("ix_chat_tool_batch", "run_id", "tool_batch_id", "call_index"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("chat_runs.id"), nullable=False)
    model_response_id: Mapped[str] = mapped_column(String(100), nullable=False)
    assistant_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_messages.id"), nullable=True
    )
    tool_batch_id: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_call_id: Mapped[str] = mapped_column(String(200), nullable=False)
    call_index: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(50), nullable=False)
    arguments_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    result_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    result_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    result_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reused_from_execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_tool_executions.id"), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class RunEvent(BusinessBase):
    __tablename__ = "chat_run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uk_chat_run_event_seq"),
        Index("ix_chat_run_event_created", "run_id", "created_at"),
        Index("ix_chat_run_event_expiry", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("chat_runs.id"), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    model_response_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    response_segment_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ChatOutboxEvent(BusinessBase):
    __tablename__ = "chat_outbox_events"
    __table_args__ = (
        UniqueConstraint("deduplication_key", name="uk_chat_outbox_dedup"),
        Index("ix_chat_outbox_available", "status", "available_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False, default="run")
    aggregate_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    deduplication_key: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
