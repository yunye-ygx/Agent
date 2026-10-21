from unittest.mock import AsyncMock

import pytest

from app.storage.elasticsearch_chunks import ChunkStore, build_chunk_source


def test_chunk_source_has_single_task_and_run_ownership():
    source = build_chunk_source(
        chunk_id="c",
        document_id="doc",
        content="hello",
        embedding=[1.0, 2.0],
        chunk_index=0,
        page_start=1,
        page_end=1,
        source_task_id="task",
        generation_id="run",
        kb_id="kb",
        metadata={},
    )
    assert source["run_id"] == "run" and source["task_id"] == "task" and source["kb_id"] == "kb"
    assert source["chunk_key"] == "0" and source["embedding"] == [1.0, 2.0]
    assert "generation_ids" not in source


def test_chunk_store_mapping_uses_dense_vector_and_explicit_ownership():
    props = ChunkStore.mapping_for_dimension(3)["mappings"]["properties"]
    assert props["embedding"] == {
        "type": "dense_vector",
        "dims": 3,
        "index": True,
        "similarity": "cosine",
    }
    for field in ("kb_id", "doc_id", "run_id", "task_id", "chunk_key"):
        assert props[field] == {"type": "keyword"}
    assert "generation_ids" not in props


@pytest.mark.asyncio
async def test_cleanup_uses_scoped_run_deletion_and_surfaces_partial_failure():
    client = AsyncMock()
    client.delete_by_query.return_value = {"failures": [], "version_conflicts": 0}
    store = ChunkStore(client=client)
    await store.delete_run_chunks(kb_id="kb", doc_id="doc", run_id="run")
    filters = client.delete_by_query.call_args.kwargs["query"]["bool"]["filter"]
    assert filters == [
        {"term": {"kb_id": "kb"}},
        {"term": {"doc_id": "doc"}},
        {"term": {"run_id": "run"}},
    ]
    client.delete_by_query.return_value = {"failures": ["failed shard"]}
    with pytest.raises(RuntimeError):
        await store.delete_run_chunks(kb_id="kb", doc_id="doc", run_id="run")
