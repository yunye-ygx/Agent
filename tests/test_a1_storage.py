from unittest.mock import AsyncMock

import pytest

from app.rag.task_processor import chunk_source_payloads
from app.storage.elasticsearch_chunks import ChunkStore


def payload(task="task", run="run"):
    return chunk_source_payloads(
        [{"block_index": 0, "block_type": "text", "text": "fixed content", "page_number": 1}],
        document_id="doc",
        task_id=task,
        generation_id=run,
        parse_fingerprint="fp",
        embedding=[[1.0, 2.0]],
    )


def test_new_run_gets_independent_id_but_same_task_retry_overwrites():
    a = payload()
    b = payload()
    c = payload("new-task", "new-run")
    assert a[0][0] == b[0][0]
    assert a[0][0] != c[0][0]
    assert a[0][1]["run_id"] == "run" and a[0][1]["task_id"] == "task"
    assert "generation_ids" not in a[0][1]


@pytest.mark.asyncio
async def test_bulk_write_checks_every_result_even_on_http_success():
    client = AsyncMock()
    client.bulk.return_value = {
        "errors": True,
        "items": [{"index": {"status": 201}}, {"index": {"status": 429, "error": "full"}}],
    }
    store = ChunkStore(client=client)
    with pytest.raises(RuntimeError):
        await store.write_chunks([("one", {}), ("two", {})])


@pytest.mark.asyncio
async def test_source_task_read_returns_all_pages_and_cleans_scroll():
    client = AsyncMock()
    client.search.return_value = {
        "_scroll_id": "s1",
        "hits": {"hits": [{"_id": "one", "_source": {"embedding": [1, 2]}}]},
    }
    client.scroll.side_effect = [
        {"_scroll_id": "s2", "hits": {"hits": [{"_id": "two", "_source": {"embedding": [3, 4]}}]}},
        {"_scroll_id": "s3", "hits": {"hits": []}},
    ]
    store = ChunkStore(client=client)
    rows = await store.read_task_chunks(kb_id="kb", doc_id="doc", run_id="run", task_id="task")
    assert len(rows) == 2 and rows[1][1]["embedding"] == [3, 4]
    assert client.scroll.await_count == 2
    client.clear_scroll.assert_awaited_once_with(scroll_id="s3")
    filters = client.search.call_args.kwargs["query"]["bool"]["filter"]
    assert {"term": {"run_id": "run"}} in filters
    assert {"term": {"task_id": "task"}} in filters


@pytest.mark.asyncio
async def test_search_filters_all_active_runs_and_kb():
    client = AsyncMock()
    client.search.return_value = {"hits": {"hits": []}}
    store = ChunkStore(client=client)
    await store.search([1, 2], run_ids=["run-1", "run-2"], kb_id="kb", limit=5)
    filters = client.search.call_args.kwargs["knn"]["filter"]
    assert {"terms": {"run_id": ["run-1", "run-2"]}} in filters
    assert {"term": {"kb_id": "kb"}} in filters


@pytest.mark.asyncio
async def test_bulk_write_rejects_missing_item_acknowledgements():
    client = AsyncMock()
    client.bulk.return_value = {"errors": False, "items": []}
    with pytest.raises(RuntimeError):
        await ChunkStore(client=client).write_chunks([("one", {})])
