from app.db.mysql_models import (
    Document,
    Generation,
    GenerationStatus,
    Task,
    TaskChunkRef,
    TaskCompletionType,
    TaskStatus,
)


def test_generation_and_task_use_explicit_lifecycle_states() -> None:
    generation = Generation(document_id="document-1", version_no=1)
    task = Task(
        generation_id="generation-1",
        document_id="document-1",
        task_no=1,
        core_page_start=1,
        core_page_end=10,
    )

    assert generation.status == GenerationStatus.BUILDING
    assert task.status == TaskStatus.WAITING_PUBLISH
    assert task.completion_type is None
    assert set(GenerationStatus) == {
        GenerationStatus.BUILDING,
        GenerationStatus.CANCELING,
        GenerationStatus.SUCCESS,
        GenerationStatus.FAILED,
        GenerationStatus.CANCELED,
    }


def test_reused_task_is_success_with_reused_completion_type() -> None:
    task = Task(
        generation_id="generation-1",
        document_id="document-1",
        task_no=1,
        core_page_start=1,
        core_page_end=10,
        status=TaskStatus.SUCCESS,
        completion_type=TaskCompletionType.REUSED,
    )
    relation = TaskChunkRef(
        task_id="task-1",
        generation_id="generation-1",
        chunk_id="chunk-1",
        chunk_index=0,
        relation_type="REUSED",
    )

    assert task.status == TaskStatus.SUCCESS
    assert task.completion_type == TaskCompletionType.REUSED
    assert relation.relation_type == "REUSED"
    assert not hasattr(relation, "content")
    assert not hasattr(relation, "embedding")


def test_document_tracks_current_and_building_generation_separately() -> None:
    document = Document(
        knowledge_base_id="kb-1",
        filename="guide.pdf",
        object_key="knowledge-bases/kb-1/documents/doc-1/source/guide.pdf",
        file_hash="a" * 64,
        file_size=42,
        mime_type="application/pdf",
    )

    document.current_generation_id = "generation-1"
    document.building_generation_id = "generation-2"

    assert document.current_generation_id == "generation-1"
    assert document.building_generation_id == "generation-2"
