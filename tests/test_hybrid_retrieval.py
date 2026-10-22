from unittest.mock import AsyncMock, MagicMock

import pytest

from app.retrieval.search import HybridSearcher


@pytest.mark.asyncio
async def test_hybrid_is_one_request_with_filters_in_both_routes():
    client = MagicMock()
    client.options.return_value = client
    client.search = AsyncMock(
        return_value={
            "hits": {
                "hits": [
                    {
                        "_id": "c",
                        "_score": 2.5,
                        "_source": {
                            "content": "E17",
                            "doc_id": "d",
                            "run_id": "r",
                            "kb_id": "kb",
                            "embedding": [1, 0],
                        },
                    }
                ]
            }
        }
    )
    searcher = HybridSearcher(client, "chunks")
    rows = await searcher.search(
        ["e17"],
        [1, 0],
        kb_id="kb",
        run_ids=["r"],
        document_ids=["d"],
        titles={"d": "E17 manual"},
        top_k=50,
    )
    assert len(rows) == 1
    assert client.search.await_count == 1
    args = client.search.call_args.kwargs
    assert args["size"] == 50 and args["knn"]["k"] == 50
    assert args["query"]["bool"]["filter"] == args["knn"]["filter"]
    assert {"terms": {"run_id": ["r"]}} in args["knn"]["filter"]
    assert {"terms": {"doc_id": ["d"]}} in args["knn"]["filter"]
    assert args["allow_partial_search_results"] is False
    assert client.options.call_args.kwargs["max_retries"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{"timed_out": True}, {"_shards": {"failed": 1}}])
async def test_partial_es_results_are_failures(response):
    client = MagicMock()
    client.options.return_value = client
    client.search = AsyncMock(return_value={**response, "hits": {"hits": []}})
    with pytest.raises(ValueError, match="不完整"):
        await HybridSearcher(client, "chunks").search(
            ["q"], [1, 0], kb_id="kb", run_ids=["r"], document_ids=["d"], titles={}, top_k=5
        )
