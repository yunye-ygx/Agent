import hashlib
import secrets
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from app.rag.chunking_schemas import ParsedPdf, PdfBlock
from app.rag.mineru_parser import MinerUParser
from app.rag.section_chunker import SectionAwareChunker
from app.rag.structure_chunker import StructureAwareChunker
from app.rag.token_chunker import TokenChunker


@dataclass
class StoredPdf:
    content: bytes
    filename: str
    expires_at: float


class ChunkingService:
    def __init__(
        self,
        *,
        parser: MinerUParser | Any | None = None,
        temp_root: str | Path | None = None,
        max_file_bytes: int = 50 * 1024 * 1024,
        file_ttl_seconds: int = 3600,
        max_tokens: int = 8192,
        default_chunk_size: int = 500,
        default_overlap: int = 80,
    ) -> None:
        self.parser = parser or MinerUParser(temp_root=temp_root)
        self.max_file_bytes = max_file_bytes
        self.file_ttl_seconds = file_ttl_seconds
        self.max_tokens = max_tokens
        self.default_chunk_size = default_chunk_size
        self.default_overlap = default_overlap
        self._documents: dict[str, StoredPdf] = {}
        self._parsed_cache: dict[str, tuple[ParsedPdf, float]] = {}

    def parse_upload(self, filename: str, content: bytes) -> dict[str, Any]:
        safe_filename = Path(filename or "").name
        if not safe_filename or Path(safe_filename).suffix.lower() != ".pdf":
            raise ValueError("仅支持 PDF 文件")
        if not content:
            raise ValueError("文件不能为空")
        if len(content) > self.max_file_bytes:
            raise ValueError(f"PDF 文件不能超过 {self.max_file_bytes / 1024 / 1024:g} MB")

        cache_key = hashlib.sha256(content).hexdigest()
        cached = self._parsed_cache.get(cache_key)
        if cached is not None and cached[1] > time.time():
            parsed = replace(cached[0], filename=safe_filename)
            parsed_cache_hit = True
        else:
            parsed = self.parser.parse_pdf(safe_filename, content)
            parsed = _add_parse_warnings(parsed)
            self._parsed_cache[cache_key] = (
                parsed,
                time.time() + self.file_ttl_seconds,
            )
            parsed_cache_hit = False
        self._cleanup_expired()
        document_id = secrets.token_urlsafe(12)
        self._documents[document_id] = StoredPdf(
            content=content,
            filename=safe_filename,
            expires_at=time.time() + self.file_ttl_seconds,
        )
        return {
            "document_id": document_id,
            "filename": parsed.filename,
            "file_type": "pdf",
            "blocks": [block.to_dict() for block in parsed.blocks],
            "parsed_text": parsed.parsed_text,
            "warnings": parsed.warnings,
            "parsed_cache_hit": parsed_cache_hit,
        }

    def debug_parse_upload(
        self,
        filename: str,
        content: bytes,
        *,
        chunk_size: int,
        overlap: int,
        delimiter: str | None = None,
        strategy: str = "token",
    ) -> dict[str, Any]:
        result = self.parse_upload(filename, content)
        preview = self.preview(
            result["blocks"],
            chunk_size=chunk_size,
            overlap=overlap,
            delimiter=delimiter,
            strategy=strategy,
        )
        cache_key = hashlib.sha256(content).hexdigest()
        parsed, _ = self._parsed_cache[cache_key]
        result["debug"] = _build_debug_payload(parsed, preview)
        return result

    def get_original_file(self, document_id: str) -> StoredPdf | None:
        self._cleanup_expired()
        document = self._documents.get(document_id)
        if document is None or document.expires_at <= time.time():
            return None
        return document

    def preview(
        self,
        blocks: list[dict[str, Any]],
        *,
        chunk_size: int,
        overlap: int,
        delimiter: str | None,
        strategy: str = "token",
    ) -> dict[str, Any]:
        if delimiter not in (None, ""):
            raise ValueError("第一版暂不支持 delimiter 切块")
        if chunk_size > self.max_tokens:
            raise ValueError(f"chunk_size 不能超过 {self.max_tokens}")
        if strategy not in {"token", "structure", "section"}:
            raise ValueError("strategy 只能是 token、structure 或 section")
        domain_blocks = [PdfBlock(**block) for block in blocks]
        chunker_by_strategy = {
            "token": TokenChunker,
            "structure": StructureAwareChunker,
            "section": SectionAwareChunker,
        }
        chunker = chunker_by_strategy[strategy](chunk_size=chunk_size, overlap=overlap)
        chunks = chunker.split(domain_blocks)
        return {
            "chunk_size": chunk_size,
            "overlap": overlap,
            "delimiter": None,
            "strategy": strategy,
            "document_title": getattr(chunker, "document_title", None),
            "chunk_count": len(chunks),
            "chunks": [chunk.to_dict() for chunk in chunks],
        }

    def _cleanup_expired(self) -> None:
        now = time.time()
        expired = [key for key, value in self._documents.items() if value.expires_at <= now]
        for key in expired:
            del self._documents[key]
        expired_cached = [key for key, value in self._parsed_cache.items() if value[1] <= now]
        for key in expired_cached:
            del self._parsed_cache[key]


def _add_parse_warnings(parsed: ParsedPdf) -> ParsedPdf:
    replacement_count = parsed.parsed_text.count("\ufffd")
    if not replacement_count:
        return parsed
    warning = (
        f"解析文本包含 {replacement_count} 个 Unicode 替换字符 U+FFFD；"
        "通常表示 PDF 字体映射或原始字节无法还原，请核对原始 PDF。"
    )
    return replace(parsed, warnings=[*parsed.warnings, warning])


def _build_debug_payload(parsed: ParsedPdf, preview: dict[str, Any]) -> dict[str, Any]:
    block_items = [block.to_dict() for block in parsed.blocks]
    block_by_index = {block.block_index: block for block in parsed.blocks}
    chunk_items = []
    for chunk in preview["chunks"]:
        source_blocks = []
        for block_index in chunk["block_indexes"]:
            block = block_by_index.get(block_index)
            if block is None:
                continue
            source_blocks.append(
                {
                    "block_id": block.block_id,
                    "block_index": block.block_index,
                    "block_type": block.block_type,
                    "page_number": block.page_number,
                    "raw_type": block.metadata.get("type", block.metadata.get("block_type")),
                    "raw_page_idx": block.metadata.get("page_idx"),
                    "raw_metadata": block.metadata,
                }
            )
        chunk_items.append(
            {
                **chunk,
                "source_block_indexes": chunk["block_indexes"],
                "source_blocks": source_blocks,
            }
        )

    return {
        "stages": {
            "mineru_raw": {
                "label": "这是 MinerU 原始结果",
                "available": bool(parsed.raw_outputs),
                "files": parsed.raw_outputs,
                "source": {
                    "class_name": "MinerUParser",
                    "method_name": "parse_pdf",
                    "source_file": "app/rag/mineru_parser.py",
                },
                "note": "只展示本次 MinerU 实际生成且被项目读取的 JSON；没有生成的文件不会伪造。",
            },
            "backend_first_processing": {
                "label": "MinerU → 后端第一次处理",
                "independent_layer": False,
                "class_name": "MinerUParser",
                "method": "normalize_content_list",
                "source_file": "app/rag/mineru_parser.py",
                "note": "当前项目没有独立的中间 DTO；下面是同一方法逐项提取字段时的审计记录。",
                "records": parsed.processing_trace,
            },
            "blocks": {
                "label": "这个 Block 是后端自己创建的，不是 MinerU 原始对象",
                "class_name": "PdfBlock",
                "source_file": "app/rag/chunking_schemas.py",
                "created_by_class": "MinerUParser",
                "created_by_method": "normalize_content_list",
                "constructor_source_file": "app/rag/mineru_parser.py",
                "field_lineage": {
                    "block_id": "后端生成 block-{index:04d}",
                    "block_index": "后端根据原始数组顺序生成",
                    "block_type": "MinerU type/block_type 映射",
                    "page_number": "MinerU page_number 或 page_idx + 1",
                    "text": "MinerU text/md/content/table_body/html/table",
                    "metadata": "完整保留原始 MinerU item",
                },
                "items": block_items,
            },
            "chunk_input": {
                "label": "Chunker 输入",
                "kind": "list[PdfBlock]",
                "class_name": "ChunkingService",
                "method": "preview",
                "source_file": "app/rag/chunking_service.py",
                "next_method": {
                    "token": "TokenChunker.split",
                    "structure": "StructureAwareChunker.split",
                    "section": "SectionAwareChunker.split",
                }[preview["strategy"]],
                "next_source_file": {
                    "token": "app/rag/token_chunker.py",
                    "structure": "app/rag/structure_chunker.py",
                    "section": "app/rag/section_chunker.py",
                }[preview["strategy"]],
                "items": block_items,
            },
            "chunk_result": {
                "label": "Chunk 后结果",
                "class_name": "TokenChunk",
                "source_file": "app/rag/chunking_schemas.py",
                "created_by_class": {
                    "token": "TokenChunker",
                    "structure": "StructureAwareChunker",
                    "section": "SectionAwareChunker",
                }[preview["strategy"]],
                "created_by_method": "split",
                "chunker_source_file": {
                    "token": "app/rag/token_chunker.py",
                    "structure": "app/rag/structure_chunker.py",
                    "section": "app/rag/section_chunker.py",
                }[preview["strategy"]],
                "chunk_size": preview["chunk_size"],
                "overlap": preview["overlap"],
                "delimiter": preview["delimiter"],
                "strategy": preview["strategy"],
                "strategy_note": {
                    "token": "原有纯 token 窗口切块。",
                    "structure": "table 作为硬边界；长 table 按完整行切分并重复表头；普通文本区域继续使用 token + overlap。",
                    "section": "标题作为上下文前缀；优先在段落边界切分；长 table 按完整行切分并重复表头。",
                }[preview["strategy"]],
                "chunks": chunk_items,
            },
        }
    }
