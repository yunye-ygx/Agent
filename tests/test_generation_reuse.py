from datetime import UTC, datetime, timedelta

import pytest

from app.api.routes import documents_v2
from app.db.mysql_models import Generation, GenerationStatus, Task
from app.db.mysql_repository import is_reusable_generation, list_reusable_generations


def make_generation(
    *,
    status: GenerationStatus,
    fingerprint: str = "fingerprint-1",
    reuse_expires_at: datetime | None = None,
) -> Generation:
    return Generation(
        id="generation-1",
        document_id="document-1",
        version_no=1,
        status=status,
        parse_fingerprint=fingerprint,
        reuse_expires_at=reuse_expires_at,
    )


def test_failed_generation_is_reusable_before_its_retention_window_expires() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    generation = make_generation(
        status=GenerationStatus.FAILED,
        reuse_expires_at=now + timedelta(minutes=5),
    )

    assert is_reusable_generation(generation, "fingerprint-1", now=now) is True


def test_failed_generation_is_not_reusable_after_its_retention_window_expires() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    generation = make_generation(
        status=GenerationStatus.FAILED,
        reuse_expires_at=now,
    )

    assert is_reusable_generation(generation, "fingerprint-1", now=now) is False


def test_failed_generation_with_a_different_fingerprint_is_not_reusable() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    generation = make_generation(
        status=GenerationStatus.FAILED,
        fingerprint="old-fingerprint",
        reuse_expires_at=now + timedelta(minutes=5),
    )

    assert is_reusable_generation(generation, "new-fingerprint", now=now) is False


def test_successful_generation_is_reusable_without_a_retention_expiry() -> None:
    generation = make_generation(status=GenerationStatus.SUCCESS)

    assert is_reusable_generation(generation, "fingerprint-1") is True


class GenerationResult:
    def __init__(self, generations: list[Generation]) -> None:
        self.generations = generations

    def scalars(self) -> "GenerationResult":
        return self

    def all(self) -> list[Generation]:
        return self.generations


class GenerationSession:
    def __init__(self, generations: list[Generation]) -> None:
        self.result = GenerationResult(generations)

    async def execute(self, _query: object) -> GenerationResult:
        return self.result


@pytest.mark.asyncio
async def test_reusable_generation_query_keeps_failed_results_inside_the_window() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    reusable = make_generation(
        status=GenerationStatus.FAILED,
        reuse_expires_at=now + timedelta(minutes=5),
    )
    expired = make_generation(
        status=GenerationStatus.FAILED,
        reuse_expires_at=now,
    )
    different_fingerprint = make_generation(
        status=GenerationStatus.FAILED,
        fingerprint="different",
        reuse_expires_at=now + timedelta(minutes=5),
    )

    result = await list_reusable_generations(
        GenerationSession([reusable, expired, different_fingerprint]),
        "document-1",
        "fingerprint-1",
        now=now,
    )

    assert result == [reusable]


@pytest.mark.asyncio
async def test_tombstoned_artifacts_are_never_reuse_candidates():
    retained = make_generation(status=GenerationStatus.SUCCESS)
    retained.artifacts_available = False
    assert not is_reusable_generation(retained, "fingerprint-1")


@pytest.mark.asyncio
async def test_incompatible_run_does_not_select_copy(db):
    from sqlalchemy import select
    from test_a1_lifecycle import seed

    doc, run, source = await seed(db)
    run.status, source.status, source.chunk_count = "SUCCESS", "SUCCESS", 1
    doc.active_run_id = run.id
    await db.commit()
    result = await documents_v2._create_generation(doc.id, db, force_new=True)
    target = (
        await db.execute(select(Task).where(Task.generation_id == result["run_id"]))
    ).scalar_one()
    assert target.kind == "PARSE" and target.source_task_id is None
    assert target.status == "WAITING_PUBLISH"
