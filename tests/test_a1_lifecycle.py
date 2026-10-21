"""A1 state/transaction tests against an isolated SQLAlchemy SQLite database.

SQLite proves transaction outcomes, not InnoDB's row-lock implementation.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import mysql_repository as repo
from app.db.mysql_models import Base, Document, Generation, KnowledgeBase, Task, TaskStatus


class DatabaseSession:
    def __init__(self, session):
        self.session = session

    async def execute(self, query, *args, **kwargs):
        return self.session.execute(query, *args, **kwargs)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    def add(self, item):
        self.session.add(item)

    async def get(self, model, key):
        return self.session.get(model, key)

    async def refresh(self, item):
        self.session.refresh(item)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield DatabaseSession(session)
    engine.dispose()


async def seed(db, *, max_attempts=3, pages=2):
    kb = KnowledgeBase(id="kb", name="test")
    db.add(kb)
    await db.flush()
    doc = Document(
        id="doc",
        knowledge_base_id=kb.id,
        filename="test.pdf",
        object_key="immutable/pdf",
        file_hash=__import__("hashlib").sha256(b"hello world").hexdigest(),
        file_size=10,
        mime_type="application/pdf",
        page_count=pages,
    )
    db.add(doc)
    await db.flush()
    run, tasks = await repo.create_generation(
        db,
        document=doc,
        parse_fingerprint="fp",
        config_snapshot={"embedding": {"model": "test", "dimension": 2}},
        task_ranges=[{"task_no": 1, "core_page_start": 0, "core_page_end": pages}],
    )
    tasks[0].max_attempts = max_attempts
    await repo.create_outbox_records(db, tasks)
    await db.commit()
    return doc, run, tasks[0]


@pytest.mark.asyncio
async def test_new_run_pins_file_and_leaves_task_waiting_without_publishing(db):
    doc, run, task = await seed(db)
    assert run.file_version == doc.file_hash
    assert run.file_location_snapshot == doc.object_key
    assert doc.current_run_id == run.id
    assert doc.active_run_id is None
    assert task.status == "WAITING_PUBLISH"
    assert task.attempt_count == 0
    assert task.dispatch_seq == 1


@pytest.mark.asyncio
async def test_claim_is_once_per_lease_and_old_dispatch_is_ignored(db):
    _doc, _run, task = await seed(db)
    now = datetime.now(UTC)
    claimed = await repo.claim_task(db, task.id, dispatch_seq=1, now=now)
    assert claimed.attempt_id and claimed.attempt_count == 1
    token = claimed.attempt_id
    assert await repo.claim_task(db, task.id, dispatch_seq=1, now=now) is None
    assert task.attempt_id == token and task.attempt_count == 1
    assert await repo.claim_task(db, task.id, dispatch_seq=0, now=now) is None


@pytest.mark.asyncio
async def test_expired_attempt_cannot_renew_or_complete_and_takeover_counts(db):
    _doc, run, task = await seed(db)
    now = datetime.now(UTC)
    await repo.claim_task(db, task.id, dispatch_seq=1, now=now)
    old = task.attempt_id
    expired = now + timedelta(seconds=10000)
    assert await repo.touch_task_heartbeat(db, task.id, attempt_id=old, now=expired) is None
    await repo.reclaim_expired_running_tasks(db, now=expired)
    assert task.status == "WAITING_PUBLISH" and task.dispatch_seq == 2
    assert task.attempt_count == 1
    await repo.claim_task(db, task.id, dispatch_seq=2, now=expired + timedelta(seconds=100))
    assert task.attempt_count == 2 and task.attempt_id != old
    with pytest.raises(repo.ExecutionLost):
        await repo.complete_task(db, task_id=task.id, attempt_id=old, chunk_count=2, now=expired)
    assert task.status == TaskStatus.RUNNING and run.completed_task_count == 0


@pytest.mark.asyncio
async def test_success_statistics_once_and_publication_is_separate(db):
    doc, run, task = await seed(db)
    now = datetime.now(UTC)
    await repo.claim_task(db, task.id, dispatch_seq=1, now=now)
    token = task.attempt_id
    await repo.complete_task(db, task_id=task.id, attempt_id=token, chunk_count=4, now=now)
    await db.commit()
    assert task.status == TaskStatus.SUCCESS
    assert run.completed_task_count == 1 and run.chunk_count == 4
    assert run.status == "RUNNING" and doc.active_run_id is None
    with pytest.raises(repo.ExecutionLost):
        await repo.complete_task(db, task_id=task.id, attempt_id=token, chunk_count=4, now=now)
    assert run.chunk_count == 4


@pytest.mark.asyncio
async def test_cancel_revokes_attempt_then_allows_new_run_without_clearing_current(db):
    doc, run, task = await seed(db)
    now = datetime.now(UTC)
    await repo.claim_task(db, task.id, dispatch_seq=1, now=now)
    token = task.attempt_id
    await repo.request_generation_cancel(db, run.id)
    with pytest.raises(repo.ExecutionLost):
        await repo.complete_task(db, task_id=task.id, attempt_id=token, chunk_count=1, now=now)
    await repo.finish_cancellations(db)
    assert run.status == "CANCELLED" and task.status == "CANCELLED"
    assert doc.current_run_id == run.id
    assert await repo.claim_task(db, task.id, dispatch_seq=1, now=now) is None


@pytest.mark.asyncio
async def test_rollback_removes_entire_new_run_and_outbox(db):
    kb = KnowledgeBase(id="kb", name="test")
    db.add(kb)
    await db.flush()
    doc = Document(
        id="doc",
        knowledge_base_id="kb",
        filename="a.txt",
        object_key="a",
        file_hash="x",
        file_size=1,
        mime_type="text/plain",
    )
    db.add(doc)
    await db.commit()
    _run, tasks = await repo.create_generation(
        db,
        document=doc,
        parse_fingerprint="fp",
        config_snapshot={},
        task_ranges=[{"task_no": 1, "core_page_start": 0, "core_page_end": 0}],
    )
    await repo.create_outbox_records(db, tasks)
    await db.rollback()
    assert (await db.execute(select(Generation))).scalars().all() == []
    assert (await db.execute(select(Task))).scalars().all() == []


@pytest.mark.asyncio
async def test_lease_is_checked_against_time_after_waiting_for_document_lock(db, monkeypatch):
    from app.db import task_lifecycle

    _doc, _run, task = await seed(db)
    now = datetime.now(UTC)
    await repo.claim_task(db, task.id, dispatch_seq=1, now=now)
    token = task.attempt_id
    current = [now]
    monkeypatch.setattr(repo, "utc_now", lambda: current[0])
    original = task_lifecycle.lock_task

    async def wait_for_lock(*args):
        value = await original(*args)
        current[0] += timedelta(seconds=10000)
        return value

    monkeypatch.setattr(task_lifecycle, "lock_task", wait_for_lock)
    with pytest.raises(repo.ExecutionLost):
        await repo.complete_task(db, task_id=task.id, attempt_id=token, chunk_count=2)
