"""Strict adapter for the /rerank results[index, relevance_score] protocol."""

import math

import httpx


class RerankError(ValueError):
    pass


class RerankClient:
    def __init__(self, base_url, api_key, model, *, transport=None, timeout=30):
        self.url = base_url.rstrip("/") + "/rerank"
        self.api_key, self.model = api_key, model
        self.transport, self.timeout = transport, timeout

    async def score(self, query, candidates):
        documents, details = [], []
        for row in candidates:
            full = (row.get("title", "") + "\n" + row["content"]).strip()
            documents.append(full[:2000])
            details.append(
                {
                    "original_chars": len(full),
                    "input_chars": min(len(full), 2000),
                    "truncated": len(full) > 2000,
                }
            )
        scores = []
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as client:
                # Batches bound provider input size; indices are local to each batch.
                for start in range(0, len(documents), 32):
                    batch = documents[start : start + 32]
                    response = await client.post(
                        self.url,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json={
                            "model": self.model,
                            "query": query,
                            "documents": batch,
                            "top_n": len(batch),
                            "return_documents": False,
                        },
                    )
                    response.raise_for_status()
                    results = response.json()["results"]
                    aligned = [None] * len(batch)
                    if not isinstance(results, list) or len(results) != len(batch):
                        raise ValueError()
                    for item in results:
                        index, score = item["index"], item["relevance_score"]
                        if (
                            type(index) is not int
                            or not 0 <= index < len(batch)
                            or aligned[index] is not None
                            or type(score) not in (int, float)
                            or not math.isfinite(score)
                            or not 0 <= score <= 1
                        ):
                            raise ValueError()
                        aligned[index] = float(score)
                    scores.extend(aligned)
        except httpx.TimeoutException as exc:
            raise RerankError("Rerank 请求超时；未自动降级") from exc
        except httpx.HTTPError as exc:
            raise RerankError("Rerank 接口请求失败，请检查连接与模型配置") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise RerankError(
                "Rerank 响应缺分或格式不兼容，需要完整的 [0,1] relevance_score"
            ) from exc
        return scores, details
