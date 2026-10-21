from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from elasticsearch import AsyncElasticsearch

from app.core.config import Settings, settings
from app.storage.task_chunks import TaskChunkOperations


def build_chunk_source(
    *,
    chunk_id: str,
    document_id: str,
    content: str,
    embedding: list[float],
    chunk_index: int,
    page_start: int | None,
    page_end: int | None,
    source_task_id: str,
    generation_id: str,
    metadata: dict[str, Any],
    kb_id: str = "",
    chunk_key: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "document_id": document_id,
        "doc_id": document_id,
        "kb_id": kb_id,
        "content": content,
        "embedding": embedding,
        "chunk_index": chunk_index,
        "page_start": page_start,
        "page_end": page_end,
        "task_id": source_task_id,
        "run_id": generation_id,
        "chunk_key": chunk_key if chunk_key is not None else str(chunk_index),
        "metadata": metadata,
        "content_hash": _sha256(content),
        "chunk_fingerprint": _sha256(f"{chunk_id}|{content}"),
        "created_at": now,
        "updated_at": now,
    }


class ChunkStore(TaskChunkOperations):
    def __init__(
        self, config: Settings = settings, client: AsyncElasticsearch | None = None
    ) -> None:
        self.config = config
        self.client = client or AsyncElasticsearch(
            config.elasticsearch_url,
            basic_auth=(config.elasticsearch_username, config.elasticsearch_password)
            if config.elasticsearch_username and config.elasticsearch_password
            else None,
        )
        self.index = config.elasticsearch_index

    @staticmethod
    def mapping_for_dimension(dimension: int) -> dict[str, Any]:
        return {
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            "mappings": {
                "dynamic": "strict",
                "properties": {
                    "document_id": {"type": "keyword"},
                    "doc_id": {"type": "keyword"},
                    "kb_id": {"type": "keyword"},
                    "content": {"type": "text"},
                    "embedding": {
                        "type": "dense_vector",
                        "dims": dimension,
                        "index": True,
                        "similarity": "cosine",
                    },
                    "chunk_index": {"type": "integer"},
                    "page_start": {"type": "integer"},
                    "page_end": {"type": "integer"},
                    "task_id": {"type": "keyword"},
                    "run_id": {"type": "keyword"},
                    "chunk_key": {"type": "keyword"},
                    # MinerU adds parser-specific fields (OCR status, image
                    # relations, table details, etc.). Keep this nested object
                    # extensible without making the top-level Chunk dynamic.
                    "metadata": {"type": "object", "dynamic": True},
                    "content_hash": {"type": "keyword"},
                    "chunk_fingerprint": {"type": "keyword"},
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"},
                },
            },
        }

    async def ensure_index(self) -> None:
        if not await self.client.indices.exists(index=self.index):
            await self.client.indices.create(
                index=self.index,
                **self.mapping_for_dimension(self.config.embedding_dimension),
            )
        else:
            mapping = await self.client.indices.get_mapping(index=self.index)
            for actual in mapping.values():
                properties = actual["mappings"].get("properties", {})
                if (
                    not all(
                        key in properties
                        for key in ("kb_id", "doc_id", "run_id", "task_id", "chunk_key")
                    )
                    or properties.get("embedding", {}).get("dims")
                    != self.config.embedding_dimension
                ):
                    raise RuntimeError(
                        "现有 ES 索引尚未完成 A1 迁移或向量维度不兼容；请先审核迁移说明"
                    )

    async def close(self) -> None:
        await self.client.close()

    async def index_chunk(self, chunk_id: str, source: dict[str, Any]) -> None:
        await self.client.index(
            index=self.index, id=chunk_id, document=source, op_type="index", refresh="wait_for"
        )

    async def get_chunk(self, chunk_id: str) -> dict[str, Any] | None:
        response = await self.client.get(index=self.index, id=chunk_id, ignore=[404])
        if not response or not response.get("found"):
            return None
        return response.get("_source", {})


def _sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()
