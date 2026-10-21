from typing import Any

import httpx

from app.core.config import Settings, settings


class EmbeddingError(RuntimeError):
    """Raised when the embedding provider cannot return usable vectors."""


class ZhipuEmbeddingClient:
    """Small async client for Zhipu's OpenAI-compatible embeddings endpoint."""

    def __init__(
        self,
        config: Settings = settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.config.llm_base_url or not self.config.llm_api_key:
            raise EmbeddingError("未配置 LLM_BASE_URL 或 LLM_API_KEY")
        if not self.config.embedding_model:
            raise EmbeddingError("未配置 EMBEDDING_MODEL")

        vectors: list[list[float]] = []
        batch_size = max(1, self.config.embedding_batch_size)
        async with httpx.AsyncClient(
            base_url=self.config.llm_base_url.rstrip("/"),
            timeout=self.config.model_timeout_seconds,
            transport=self.transport,
        ) as client:
            for start in range(0, len(texts), batch_size):
                batch = texts[start : start + batch_size]
                try:
                    response = await client.post(
                        "/embeddings",
                        headers={"Authorization": f"Bearer {self.config.llm_api_key}"},
                        json={"model": self.config.embedding_model, "input": batch},
                    )
                    response.raise_for_status()
                    payload: dict[str, Any] = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    raise EmbeddingError("Embedding 服务请求失败，请检查网络、模型和 Key") from exc

                data = payload.get("data")
                if not isinstance(data, list) or len(data) != len(batch):
                    raise EmbeddingError("Embedding 服务返回的向量数量与文本数量不一致")
                ordered = sorted(data, key=lambda item: item.get("index", 0))
                for item in ordered:
                    vector = item.get("embedding") if isinstance(item, dict) else None
                    if not isinstance(vector, list) or len(vector) != self.config.embedding_dimension:
                        raise EmbeddingError(
                            f"Embedding 维度不正确，期望 {self.config.embedding_dimension}"
                        )
                    vectors.append([float(value) for value in vector])
        return vectors
