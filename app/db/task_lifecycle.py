"""A1 control plane. All competing transitions lock Document -> Run -> Task.

Callers own the short transaction and must commit before ACK/publishing to MQ.
Network work must not run inside these transactions.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from app.core.config import settings
from app.db import mysql_repository as repo
from app.db.mysql_models import (
    Document,
    DocumentStatus,
    Generation,
    GenerationStatus,
    OutboxStatus,
    Task,
    TaskCompletionType,
    TaskDispatchOutbox,
    TaskKind,
    TaskStatus,
    new_id,
)

TERMINAL = {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED}
LIVE = {GenerationStatus.RUNNING, GenerationStatus.CANCELLING}


class ExecutionLost(RuntimeError):
    """The caller no longer holds permission to commit task results."""


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def get_generation(session, generation_id, *, lock=False):
    query = select(Generation).where(Generation.id == generation_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return (await session.execute(query)).scalar_one_or_none()


async def get_task(session, task_id, *, lock=False):
    query = select(Task).where(Task.id == task_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return (await session.execute(query)).scalar_one_or_none()


async def lock_run(session, run_id):
    run = await get_generation(session, run_id)
    if run is None:
        return None, None
    doc = (
        await session.execute(
            select(Document)
            .where(Document.id == run.document_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    return doc, await get_generation(session, run_id, lock=True)


async def lock_task(session, task_id):
    task = await get_task(session, task_id)
    if task is None:
        return None, None, None
    doc, run = await lock_run(session, task.generation_id)
    return doc, run, await get_task(session, task_id, lock=True)


def valid_run(doc, run) -> bool:
    return bool(
        doc
        and run
        and doc.status == DocumentStatus.ACTIVE
        and doc.current_run_id == run.id
        and run.status == GenerationStatus.RUNNING
    )


def owns(task, attempt_id, now) -> bool:
    return bool(
        task
        and task.status == TaskStatus.RUNNING
        and attempt_id
        and task.attempt_id == attempt_id
        and task.lease_until
        and aware(task.lease_until) > now
        and task.execution_deadline
        and aware(task.execution_deadline) > now
    )


async def create_generation(session, *, document, parse_fingerprint, config_snapshot, task_ranges):
    locked = await repo.get_document(session, document.id, lock=True)
    if locked is None:
        raise ValueError("文档不存在")
    if locked.current_run_id:
        previous = await get_generation(session, locked.current_run_id, lock=True)
        if previous and previous.status in LIVE:
            raise ValueError("该文档已有有效解析批次，请先取消")
    if not task_ranges:
        raise ValueError("任务清单不能为空")
    run = Generation(
        document_id=locked.id,
        version_no=await repo.next_generation_version(session, locked.id),
        parse_fingerprint=parse_fingerprint,
        config_snapshot=deepcopy(config_snapshot),
        file_version=locked.file_hash,
        file_location_snapshot=locked.object_key,
        expected_task_count=len(task_ranges),
        status=GenerationStatus.RUNNING,
    )
    session.add(run)
    await session.flush()
    tasks = []
    now = repo.utc_now()
    for part in task_ranges:
        task = Task(
            generation_id=run.id,
            document_id=locked.id,
            task_no=part["task_no"],
            core_page_start=part["core_page_start"],
            core_page_end=part["core_page_end"],
            context_page_start=part.get("context_page_start"),
            context_page_end=part.get("context_page_end"),
            parse_fingerprint=parse_fingerprint,
            kind=TaskKind.PARSE,
            max_attempts=settings.task_max_retry + 1,
            max_retry=settings.task_max_retry,
            next_attempt_at=now,
            attempt_count=0,
            dispatch_seq=1,
        )
        session.add(task)
        tasks.append(task)
    locked.current_run_id = run.id
    await session.flush()
    return run, tasks


def task_payload(task):
    return {
        "task_id": task.id,
        "doc_id": task.document_id,
        "run_id": task.generation_id,
        "dispatch_seq": task.dispatch_seq,
    }


async def create_outbox_records(session, tasks, *, now=None):
    timestamp = now or repo.utc_now()
    records = []
    for task in tasks:
        record = TaskDispatchOutbox(
            task_id=task.id,
            message_type="TASK_EXECUTE",
            payload=task_payload(task),
            status=OutboxStatus.PENDING,
            next_attempt_at=timestamp,
        )
        session.add(record)
        records.append(record)
    await session.flush()
    return records


async def claim_task(session, task_id, *, dispatch_seq, now=None):
    doc, run, task = await lock_task(session, task_id)
    now = now or repo.utc_now()
    if not valid_run(doc, run) or task is None or task.dispatch_seq != dispatch_seq:
        return None
    if task.status not in {TaskStatus.WAITING_PUBLISH, TaskStatus.PENDING}:
        return None
    if task.attempt_count >= task.max_attempts or aware(task.next_attempt_at) > now:
        return None
    task.status = TaskStatus.RUNNING
    task.attempt_id = new_id()
    task.attempt_count += 1
    task.started_at = now
    task.heartbeat_at = now
    task.execution_deadline = now + timedelta(seconds=settings.task_execution_timeout_seconds)
    task.lease_until = min(
        now + timedelta(seconds=settings.task_lease_seconds), task.execution_deadline
    )
    await session.flush()
    return task


async def assert_execution(session, task_id, attempt_id, *, now=None):
    doc, run, task = await lock_task(session, task_id)
    now = now or repo.utc_now()
    if not valid_run(doc, run) or not owns(task, attempt_id, now):
        raise ExecutionLost("执行资格过期、被接管或批次已取消/结束")
    return task, run, doc


async def touch_task_heartbeat(session, task_id, *, attempt_id, now=None):
    try:
        task, _run, _doc = await assert_execution(session, task_id, attempt_id, now=now)
    except ExecutionLost:
        return None
    now = now or repo.utc_now()
    if not owns(task, attempt_id, now):
        return None
    task.heartbeat_at = now
    task.lease_until = min(
        now + timedelta(seconds=settings.task_lease_seconds), aware(task.execution_deadline)
    )
    await session.flush()
    return task


async def complete_task(
    session, *, task_id, attempt_id, chunk_count, now=None, completion_type=None
):
    task, run, _doc = await assert_execution(session, task_id, attempt_id, now=now)
    if chunk_count < 0:
        raise ValueError("产物数量不得为负")
    task.status = TaskStatus.SUCCESS
    task.result_chunk_count = chunk_count
    task.completion_type = completion_type or (
        TaskCompletionType.REUSED if task.kind == TaskKind.COPY else TaskCompletionType.EXECUTED
    )
    task.lease_until = None
    task.finished_at = now or repo.utc_now()
    run.completed_task_count += 1
    run.chunk_count += chunk_count
    await session.flush()
    return task


async def pin_task_output(session, task_id, attempt_id, fingerprint):
    task, _run, _doc = await assert_execution(session, task_id, attempt_id)
    if task.output_fingerprint and task.output_fingerprint != fingerprint:
        raise ValueError("固定输入的 chunk 集合/文本发生变化，拒绝混写；需新建批次或核实解析稳定性")
    task.output_fingerprint = fingerprint
    await session.flush()


async def schedule_task_retry(session, task, *, now=None):
    now = now or repo.utc_now()
    # Must be called while holding Document -> Run -> Task, in the failure/recovery transaction.
    task.status = TaskStatus.WAITING_PUBLISH
    task.dispatch_seq += 1
    task.attempt_id = None
    task.lease_until = None
    task.heartbeat_at = None
    task.execution_deadline = None
    task.retry_count = task.attempt_count  # compatibility display only
    task.next_attempt_at = now + timedelta(
        seconds=min(60, settings.task_retry_backoff_seconds * 2 ** min(task.attempt_count - 1, 6))
    )
    outbox = (
        await session.execute(
            select(TaskDispatchOutbox)
            .where(TaskDispatchOutbox.task_id == task.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if outbox is None:
        raise ValueError("任务缺少持久化投递记录")
    outbox.status = OutboxStatus.PENDING
    outbox.payload = task_payload(task)
    outbox.next_attempt_at = task.next_attempt_at
    outbox.published_at = None
    outbox.last_error = task.last_error_message
    await session.flush()


async def stop_run(session, doc, run, *, failed=False, now=None):
    now = now or repo.utc_now()
    run.status = GenerationStatus.FAILED if failed else GenerationStatus.CANCELLING
    tasks = (
        (
            await session.execute(
                select(Task)
                .where(Task.generation_id == run.id)
                .order_by(Task.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    for task in tasks:
        if task.status in TERMINAL:
            continue
        task.status = (
            TaskStatus.CANCELLING if task.status == TaskStatus.RUNNING else TaskStatus.CANCELLED
        )
        task.cancel_requested_at = now
        # Remote calls may still run; control-plane permission is revoked now.
        task.attempt_id = None
        task.lease_until = None
        if task.status == TaskStatus.CANCELLED:
            task.finished_at = now
    run.reuse_expires_at = now + timedelta(seconds=settings.failed_generation_ttl_seconds)
    if failed:
        run.finished_at = now
    await session.flush()


async def fail_task(session, *, task_id, attempt_id, code, message, retry=True, now=None):
    task, run, doc = await assert_execution(session, task_id, attempt_id, now=now)
    now = now or repo.utc_now()
    task.last_error_code = code
    task.last_error_message = message[:2000]
    if retry and task.attempt_count < task.max_attempts:
        await schedule_task_retry(session, task, now=now)
    else:
        task.status = TaskStatus.FAILED
        task.attempt_id = None
        task.lease_until = None
        task.finished_at = now
        run.failed_task_count += 1
        run.failure_reason = task.last_error_message
        await session.flush()
        await stop_run(session, doc, run, failed=True, now=now)
    await session.flush()
    return task


def is_task_lease_expired(task, *, now=None, lease_seconds=None):
    now = now or repo.utc_now()
    return bool(task.lease_until and aware(task.lease_until) <= now)


async def reclaim_expired_running_tasks(session, *, now=None, lease_seconds=None):
    now = now or repo.utc_now()
    candidates = (
        (
            await session.execute(
                select(Task.id)
                .where(Task.status == TaskStatus.RUNNING, Task.lease_until <= now)
                .order_by(Task.document_id, Task.id)
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    recovered = []
    for task_id in candidates:
        doc, run, task = await lock_task(session, task_id)
        if (
            not valid_run(doc, run)
            or task.status != TaskStatus.RUNNING
            or not is_task_lease_expired(task, now=now)
        ):
            continue
        task.last_error_code = "LEASE_EXPIRED"
        task.last_error_message = "执行租约过期；撤销旧资格后重新调度整个任务"
        if task.attempt_count < task.max_attempts:
            await schedule_task_retry(session, task, now=now)
        else:
            task.status = TaskStatus.FAILED
            task.attempt_id = None
            task.lease_until = None
            task.finished_at = now
            run.failed_task_count += 1
            run.failure_reason = task.last_error_message
            await session.flush()
            await stop_run(session, doc, run, failed=True, now=now)
        recovered.append(task)
    await session.flush()
    return recovered


async def request_generation_cancel(session, generation_id):
    doc, run = await lock_run(session, generation_id)
    if not run or run.status not in LIVE or doc.current_run_id != run.id:
        raise ValueError("当前批次已经结束或失效")
    await stop_run(session, doc, run)
    return run


async def finish_cancellations(session, *, generation_id=None):
    unfinished = (
        select(Task.id)
        .where(Task.generation_id == Generation.id, Task.status == TaskStatus.CANCELLING)
        .exists()
    )
    query = select(Generation.id).where(
        Generation.status.in_([GenerationStatus.CANCELLING, GenerationStatus.FAILED]),
        or_(Generation.status == GenerationStatus.CANCELLING, unfinished),
    )
    if generation_id:
        query = query.where(Generation.id == generation_id)
    ids = (await session.execute(query.order_by(Generation.document_id).limit(100))).scalars().all()
    for run_id in ids:
        _doc, run = await lock_run(session, run_id)
        if run.status not in {GenerationStatus.CANCELLING, GenerationStatus.FAILED}:
            continue
        tasks = (
            (
                await session.execute(
                    select(Task)
                    .where(Task.generation_id == run_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        for task in tasks:
            if task.status == TaskStatus.CANCELLING:
                task.status = TaskStatus.CANCELLED
                task.attempt_id = None
                task.lease_until = None
                task.finished_at = repo.utc_now()
        run.canceled_task_count = sum(t.status == TaskStatus.CANCELLED for t in tasks)
        if run.status == GenerationStatus.CANCELLING:
            run.status = GenerationStatus.CANCELLED
            run.finished_at = repo.utc_now()
    await session.flush()
    return len(ids)


async def refresh_generation_status(session, generation_id):
    """Refresh statistics only. Publishing needs ES validation outside this transaction."""
    _doc, run = await lock_run(session, generation_id)
    if run is None:
        raise ValueError("批次不存在")
    tasks = (
        (await session.execute(select(Task).where(Task.generation_id == run.id))).scalars().all()
    )
    run.completed_task_count = sum(t.status == TaskStatus.SUCCESS for t in tasks)
    run.failed_task_count = sum(t.status == TaskStatus.FAILED for t in tasks)
    run.canceled_task_count = sum(t.status == TaskStatus.CANCELLED for t in tasks)
    run.chunk_count = sum(t.result_chunk_count for t in tasks if t.status == TaskStatus.SUCCESS)
    await session.flush()
    return run


async def publish_generation(session, run_id, *, expected_tasks):
    doc, run = await lock_run(session, run_id)
    if not valid_run(doc, run):
        return False
    tasks = (
        (
            await session.execute(
                select(Task)
                .where(Task.generation_id == run.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    actual = {t.id: t.result_chunk_count for t in tasks}
    if not tasks or len(tasks) != run.expected_task_count or actual != expected_tasks:
        return False
    if any(t.status != TaskStatus.SUCCESS for t in tasks):
        return False
    now = repo.utc_now()
    if doc.active_run_id and doc.active_run_id != run.id:
        previous = await get_generation(session, doc.active_run_id, lock=True)
        if previous:
            previous.reuse_expires_at = now + timedelta(seconds=settings.result_retention_seconds)
    doc.active_run_id = run.id
    run.status = GenerationStatus.SUCCESS
    run.failure_reason = None
    run.published_at = run.finished_at = now
    await session.flush()
    return True


async def find_reusable_task(session, *, generation_id, task):
    return (
        await session.execute(
            select(Task)
            .where(
                Task.generation_id == generation_id,
                Task.document_id == task.document_id,
                Task.status == TaskStatus.SUCCESS,
                Task.parse_fingerprint == task.parse_fingerprint,
                Task.core_page_start == task.core_page_start,
                Task.core_page_end == task.core_page_end,
                Task.context_page_start == task.context_page_start,
                Task.context_page_end == task.context_page_end,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def reserve_dispatch(session, task_id, dispatch_seq, *, now=None):
    doc, run, task = await lock_task(session, task_id)
    now = now or repo.utc_now()
    if not valid_run(doc, run) or not task or task.status != TaskStatus.WAITING_PUBLISH:
        return None
    if (
        task.dispatch_seq != dispatch_seq
        or task.attempt_count >= task.max_attempts
        or aware(task.next_attempt_at) > now
    ):
        return None
    record = (
        await session.execute(
            select(TaskDispatchOutbox)
            .where(TaskDispatchOutbox.task_id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if not record or record.status != OutboxStatus.PENDING or aware(record.next_attempt_at) > now:
        return None
    # Reservation is durable before I/O; crashes merely delay a bounded re-send.
    record.next_attempt_at = now + timedelta(seconds=settings.dispatch_interval_seconds)
    record.attempt_count += 1
    payload = task_payload(task)
    record.payload = payload
    await session.flush()
    return payload


async def confirm_dispatch(session, task_id, dispatch_seq, *, now=None):
    doc, run, task = await lock_task(session, task_id)
    if not task or task.dispatch_seq != dispatch_seq or not valid_run(doc, run):
        return False
    record = (
        await session.execute(
            select(TaskDispatchOutbox)
            .where(TaskDispatchOutbox.task_id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if not record or record.payload.get("dispatch_seq") != dispatch_seq:
        return False
    if task.status == TaskStatus.WAITING_PUBLISH:
        task.status = TaskStatus.PENDING
    record.status = OutboxStatus.PUBLISHED
    record.published_at = now or repo.utc_now()
    await session.flush()
    return True


async def mark_tasks_queued(session, dispatches):
    for task_id, seq in dispatches:
        await confirm_dispatch(session, task_id, seq)
