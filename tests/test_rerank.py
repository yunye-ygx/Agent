import httpx
import pytest

from app.retrieval.rerank import RerankClient, RerankError


@pytest.mark.asyncio
async def test_rerank_scores_are_aligned_and_full_candidates_requested():
    async def respond(request):
        import json

        payload = json.loads(request.content)
        assert payload["top_n"] == 2
        assert payload["query"] == "NX100 不打印"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.2},
                    {"index": 0, "relevance_score": 0.9},
                ]
            },
        )

    client = RerankClient(
        "https://provider.test/v1", "key", "ranker", transport=httpx.MockTransport(respond)
    )
    scores, details = await client.score(
        "NX100 不打印", [{"title": "title", "content": "a" * 3000}, {"title": "", "content": "b"}]
    )
    assert scores == [0.9, 0.2]
    assert details[0]["truncated"] is True
    assert details[1]["truncated"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "results",
    [
        [],
        [{"index": 0, "relevance_score": 0.8}] * 2,
        [{"index": 5, "relevance_score": 0.8}],
        [{"index": 0, "relevance_score": 80}],
        [{"index": 0, "relevance_score": "0.9"}],
        [{"index": True, "relevance_score": 0.9}],
    ],
)
async def test_invalid_rerank_response_fails_instead_of_filling_zero(results):
    client = RerankClient(
        "https://provider.test/v1",
        "secret",
        "ranker",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"results": results})),
    )
    with pytest.raises(RerankError):
        await client.score("q", [{"content": "c", "title": ""}])


@pytest.mark.asyncio
async def test_rerank_timeout_has_safe_error_message():
    def timeout(request):
        raise httpx.ReadTimeout("secret https://private-host", request=request)

    client = RerankClient(
        "https://provider.test/v1", "secret", "ranker", transport=httpx.MockTransport(timeout)
    )
    with pytest.raises(RerankError, match="超时") as exc:
        await client.score("q", [{"content": "c", "title": ""}])
    assert "secret" not in str(exc.value)
