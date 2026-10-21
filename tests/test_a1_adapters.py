from unittest.mock import AsyncMock

import httpx
import pytest

from app.rag.document_parser import RemoteMinerUParser
from app.rag.mineru_parser import MinerUExecutionError
from app.storage.elasticsearch_chunks import ChunkStore


@pytest.mark.asyncio
async def test_remote_adapter_has_an_explicit_multipart_contract():
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.path == "/file_parse"
        assert b"return_content_list" in request.content and b"PDF" in request.content
        return httpx.Response(
            200,
            json={
                "results": {
                    "a": {"content_list": [{"type": "text", "page_idx": 0, "text": "remote text"}]}
                }
            },
        )

    parser = RemoteMinerUParser(
        base_url="http://isolated.invalid",
        timeout_seconds=1,
        backend="pipeline",
        method="auto",
        transport=httpx.MockTransport(handle),
    )
    result = await parser.parse_pdf_async("a.pdf", b"PDF")
    assert result.blocks[0].page_number == 1 and result.blocks[0].text == "remote text"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_remote_adapter_rejects_other_service_contracts():
    parser = RemoteMinerUParser(
        base_url="http://isolated.invalid",
        timeout_seconds=1,
        backend="pipeline",
        method="auto",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"arbitrary_result": "unknown"})
        ),
    )
    with pytest.raises(MinerUExecutionError):
        await parser.parse_pdf_async("a.pdf", b"PDF")


@pytest.mark.asyncio
async def test_existing_shared_chunk_index_requires_explicit_migration():
    client = AsyncMock()
    client.indices.exists.return_value = True
    store = ChunkStore(client=client)
    client.indices.get_mapping.return_value = {
        store.index: {
            "mappings": {
                "properties": {"generation_ids": {"type": "keyword"}, "embedding": {"dims": 2048}}
            }
        }
    }
    with pytest.raises(RuntimeError, match="迁移"):
        await store.ensure_index()
    client.indices.put_mapping.assert_not_awaited()
