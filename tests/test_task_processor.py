from app.db.mysql_models import TaskStatus
from app.rag.chunking_schemas import TokenChunk
from app.rag.task_processor import chunk_source_payloads, should_skip_task
from app.rag.task_processor_service import TaskProcessor


def test_terminal_task_is_skipped_on_duplicate_delivery() -> None:
    assert should_skip_task(TaskStatus.SUCCESS) is True
    assert should_skip_task(TaskStatus.FAILED) is True
    assert should_skip_task(TaskStatus.CANCELED) is True
    assert should_skip_task(TaskStatus.RUNNING) is False


def test_chunk_payloads_use_deterministic_ids_and_page_metadata() -> None:
    blocks = [
        {
            "block_type": "text",
            "block_index": 0,
            "page_number": 3,
            "text": "A useful paragraph.",
            "metadata": {"title": "Intro"},
        }
    ]

    payloads = chunk_source_payloads(
        blocks,
        document_id="document-1",
        task_id="task-1",
        generation_id="generation-1",
        parse_fingerprint="fingerprint-1",
        embedding=[[0.1, 0.2]],
    )

    assert len(payloads) == 1
    chunk_id, source = payloads[0]
    assert chunk_id
    assert source["page_start"] == 3
    assert source["page_end"] == 3
    assert source["run_id"] == "generation-1"


def test_chunk_payloads_accept_prebuilt_chunks_without_resplitting() -> None:
    blocks = [
        {
            "block_type": "text",
            "block_index": 0,
            "page_number": 1,
            "text": "original source text",
            "metadata": {},
        }
    ]
    prebuilt = [
        TokenChunk(
            chunk_index=0,
            text="already structured",
            token_count=2,
            start_token=0,
            end_token=2,
            start_char=0,
            end_char=18,
            block_types=["text"],
            block_indexes=[0],
            metadata={"strategy": "structured", "split_level": "sentence"},
        )
    ]

    payloads = chunk_source_payloads(
        blocks,
        document_id="document-1",
        task_id="task-1",
        generation_id="generation-1",
        parse_fingerprint="fingerprint-1",
        embedding=[[0.1, 0.2]],
        chunks=prebuilt,
    )

    assert payloads[0][1]["content"] == "already structured"
    assert payloads[0][1]["metadata"]["strategy"] == "structured"


def test_task_processor_defers_chunker_creation_until_run_snapshot_is_loaded() -> None:
    processor = TaskProcessor()

    assert processor.chunker is None
