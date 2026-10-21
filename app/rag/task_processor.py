from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.db.mysql_models import TaskStatus
from app.rag.chunking_schemas import PdfBlock, TokenChunk
from app.rag.fingerprint import build_task_chunk_id
from app.storage.elasticsearch_chunks import build_chunk_source


def should_skip_task(status: TaskStatus | str) -> bool:
    value = status.value if isinstance(status, TaskStatus) else status
    return value in {TaskStatus.SUCCESS.value, TaskStatus.FAILED.value, TaskStatus.CANCELED.value}


def chunk_source_payloads(
    blocks: Iterable[dict[str, Any]],
    *,
    document_id: str,
    task_id: str,
    generation_id: str,
    parse_fingerprint: str,
    embedding: list[list[float]],
    chunks: list[TokenChunk] | None = None,
    kb_id: str = "",
) -> list[tuple[str, dict[str, Any]]]:
    normalized = [
        item
        if isinstance(item, PdfBlock)
        else PdfBlock(
            block_id=str(item.get("block_id", f"block-{item['block_index']:04d}")),
            block_index=item["block_index"],
            block_type=item["block_type"],
            page_number=item.get("page_number"),
            text=item["text"],
            metadata=item.get("metadata", {}),
        )
        for item in blocks
    ]
    resolved_chunks = chunks if chunks is not None else _token_chunks(normalized)
    if len(resolved_chunks) != len(embedding):
        raise ValueError("Embedding 数量必须与 Chunk 数量一致")
    result: list[tuple[str, dict[str, Any]]] = []
    for chunk, vector in zip(resolved_chunks, embedding, strict=True):
        page_numbers = [
            block.page_number
            for block in normalized
            if block.block_index in chunk.block_indexes and block.page_number is not None
        ]
        page_start = min(page_numbers) if page_numbers else None
        page_end = max(page_numbers) if page_numbers else None
        chunk_key = str(chunk.chunk_index)
        chunk_id = build_task_chunk_id(task_id, chunk_key)
        source = build_chunk_source(
            chunk_id=chunk_id,
            document_id=document_id,
            content=chunk.text,
            embedding=vector,
            chunk_index=chunk.chunk_index,
            page_start=page_start,
            page_end=page_end,
            source_task_id=task_id,
            generation_id=generation_id,
            kb_id=kb_id,
            chunk_key=chunk_key,
            metadata={
                **chunk.metadata,
                "block_types": chunk.block_types,
                "block_indexes": chunk.block_indexes,
            },
        )
        result.append((chunk_id, source))
    return result


def _token_chunks(blocks: list[PdfBlock]) -> list[TokenChunk]:
    from app.rag.token_chunker import TokenChunker

    return TokenChunker().split(blocks)
