import httpx
import pytest

from app.core.config import Settings
from app.rag.embedding import EmbeddingError, ZhipuEmbeddingClient


def _config() -> Settings:
    return Settings(
        llm_base_url="https://example.test/v4",
        llm_api_key="test-key",
        embedding_model="embedding-3",
        embedding_dimension=3,
    )


@pytest.mark.asyncio
async def test_embedding_client_preserves_provider_order() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v4/embeddings"
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.4, 0.5, 0.6]},
                    {"index": 0, "embedding": [0.1, 0.2, 0.3]},
                ]
            },
        )

    client = ZhipuEmbeddingClient(_config(), httpx.MockTransport(handler))

    assert await client.embed_texts(["a", "b"]) == [
        [0.1, 0.2, 0.3],
        [0.4, 0.5, 0.6],
    ]


@pytest.mark.asyncio
async def test_embedding_client_rejects_wrong_dimension() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 2]}]})

    client = ZhipuEmbeddingClient(_config(), httpx.MockTransport(handler))

    with pytest.raises(EmbeddingError, match="维度不正确"):
        await client.embed_texts(["a"])
