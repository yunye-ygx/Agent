from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

import tiktoken

from app.rag.chunking_schemas import PdfBlock, TokenChunk
from app.rag.errors import DocumentValidationError
from app.rag.table_chunker import split_table_text


@dataclass(frozen=True)
class _AnalyzedBlock:
    block: PdfBlock
    kind: str
    level: int | None = None
    marker: str | None = None


@dataclass(frozen=True)
class _ListItem:
    analyzed: _AnalyzedBlock
    path: list[str]


class StructuredChunker:
    """Chunk normalized MinerU blocks while preserving document structure."""

    encoding_name = "cl100k_base"
    strategy_version = "structured-v1"
    _numbered_pattern = re.compile(r"^\s*((?:\d+\.)*\d+)(?:[.)、]|\s+)(.*)$")
    _bullet_pattern = re.compile(r"^\s*([-*+•●◦▪])\s+(.*)$")
    _sentence_pattern = re.compile(r"(?<=[。！？!?；;])\s+|(?<=[。！？!?；;])")
    _punctuation_pattern = re.compile(r"(?<=[，,、：:；;。！？!?（）()【】\[\]])\s*")

    def __init__(self, chunk_size: int = 500, overlap: int = 80) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须大于 0")
        if overlap < 0 or overlap >= chunk_size:
            raise ValueError("overlap 必须大于等于 0 且小于 chunk_size")
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.encoding = tiktoken.get_encoding(self.encoding_name)

    def split(self, blocks: list[PdfBlock]) -> list[TokenChunk]:
        analyzed = [self._analyze(block) for block in blocks if block.text.strip() or block.block_type == "image"]
        if not analyzed:
            raise DocumentValidationError("可切块的文本不能为空")

        by_index = {item.block.block_index: item.block for item in analyzed}
        chunks: list[TokenChunk] = []
        heading_stack: list[tuple[int, PdfBlock]] = []
        text_blocks: list[PdfBlock] = []
        list_items: list[_ListItem] = []
        list_stack: list[_ListItem] = []
        consumed_relative: set[int] = set()

        def section_path() -> list[str]:
            return [item.text.strip() for _, item in heading_stack]

        def flush_text(reason: str) -> None:
            if text_blocks:
                chunks.extend(
                    self._text_chunks(
                        text_blocks,
                        section_path(),
                        reason,
                        [heading for _, heading in heading_stack],
                    )
                )
                text_blocks.clear()

        def flush_list(reason: str) -> None:
            if list_items:
                chunks.extend(self._list_chunks(list_items, section_path(), reason))
                list_items.clear()
                list_stack.clear()

        for item in analyzed:
            block = item.block
            if block.block_index in consumed_relative:
                continue

            if item.kind == "title":
                flush_text("title_boundary")
                flush_list("title_boundary")
                level = item.level or 1
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, block))
                continue

            if item.kind == "table":
                flush_text("table_boundary")
                flush_list("table_boundary")
                chunks.extend(self._table_chunks(block, section_path()))
                continue

            if item.kind == "image":
                flush_text("image_boundary")
                flush_list("image_boundary")
                relative_indexes = self._relation_indexes(block, "relative")
                consumed_relative.update(index for index in relative_indexes if index > block.block_index)
                chunks.extend(self._image_chunks(block, section_path(), by_index))
                continue

            if item.kind == "list":
                flush_text("list_boundary") if text_blocks else None
                path = self._list_path(list_stack, item)
                list_items.append(_ListItem(item, path))
                list_stack = [*list_stack[: max((item.level or 1) - 1, 0)], _ListItem(item, path)]
                continue

            if list_items:
                flush_list("structure_boundary")
            text_blocks.append(block)

        flush_text("document_end")
        flush_list("document_end")

        if not chunks and heading_stack:
            chunks.append(self._title_only_chunk(heading_stack[-1][1], section_path()))
        if not chunks:
            raise DocumentValidationError("可切块的文本不能为空")
        return [self._renumber(chunk, index) for index, chunk in enumerate(chunks)]

    def _analyze(self, block: PdfBlock) -> _AnalyzedBlock:
        raw_type = str(block.metadata.get("type") or block.metadata.get("block_type") or "").lower()
        if block.block_type == "table" or raw_type == "table":
            return _AnalyzedBlock(block, "table")
        if block.block_type == "image" or raw_type in {"image", "figure"}:
            return _AnalyzedBlock(block, "image")
        if block.block_type == "title" or raw_type in {"title", "doc_title", "heading"}:
            return _AnalyzedBlock(block, "title", self._heading_level(block))
        if block.block_type == "list" or block.metadata.get("is_list") is True:
            return _AnalyzedBlock(block, "list", self._list_level(block), self._list_marker(block))
        if (
            (self._bullet_pattern.match(block.text) or self._numbered_pattern.match(block.text))
            and block.metadata.get("text_level") is None
        ):
            return _AnalyzedBlock(block, "list", self._list_level(block), self._list_marker(block))
        if block.metadata.get("text_level") is not None:
            return _AnalyzedBlock(block, "title", self._heading_level(block))
        return _AnalyzedBlock(block, "text")

    def _heading_level(self, block: PdfBlock) -> int:
        match = self._numbered_pattern.match(block.text)
        if match:
            return len(match.group(1).rstrip(".").split("."))
        raw_level = block.metadata.get("text_level")
        try:
            if raw_level is not None and int(raw_level) > 0:
                return int(raw_level)
        except (TypeError, ValueError):
            pass
        return 1

    def _list_level(self, block: PdfBlock) -> int:
        raw_level = block.metadata.get("list_level")
        try:
            if raw_level is not None and int(raw_level) > 0:
                return int(raw_level)
        except (TypeError, ValueError):
            pass
        match = self._numbered_pattern.match(block.text)
        if match:
            return len(match.group(1).rstrip(".").split("."))
        bullet = self._bullet_pattern.match(block.text)
        if bullet:
            indentation = len(block.text) - len(block.text.lstrip())
            return max(1, indentation // 2 + 1)
        return 1

    def _list_marker(self, block: PdfBlock) -> str | None:
        match = self._numbered_pattern.match(block.text)
        if match:
            return match.group(1)
        match = self._bullet_pattern.match(block.text)
        return match.group(1) if match else None

    @staticmethod
    def _list_path(stack: list[_ListItem], item: _AnalyzedBlock) -> list[str]:
        level = item.level or 1
        return [entry.analyzed.block.text.strip() for entry in stack[: level - 1]] + [item.block.text.strip()]

    def _text_chunks(
        self,
        blocks: list[PdfBlock],
        section_path: list[str],
        boundary_reason: str,
        heading_blocks: list[PdfBlock],
    ) -> list[TokenChunk]:
        prefix = self._context_prefix(section_path)
        capacity = max(self.chunk_size - len(self.encoding.encode(prefix)), 1)
        chunks: list[TokenChunk] = []
        pending: list[tuple[str, PdfBlock]] = []

        def flush_pending(reason: str) -> None:
            if not pending:
                return
            text = "\n\n".join(value for value, _ in pending)
            source = [*heading_blocks, *(block for _, block in pending)]
            chunks.extend(
                self._make_text_chunks(
                    text,
                    source,
                    prefix,
                    capacity,
                    reason,
                    "paragraph",
                    [block.block_index for block in heading_blocks],
                )
            )
            pending.clear()

        for block in blocks:
            paragraphs = [part.strip() for part in re.split(r"\n\s*\n", block.text) if part.strip()]
            for paragraph in paragraphs:
                candidate = "\n\n".join([value for value, _ in pending] + [paragraph])
                if len(self.encoding.encode(candidate)) <= capacity:
                    pending.append((paragraph, block))
                    continue
                flush_pending("paragraph_boundary")
                chunks.extend(
                    self._make_text_chunks(
                        paragraph,
                        [*heading_blocks, block],
                        prefix,
                        capacity,
                        boundary_reason,
                        "sentence",
                        [item.block_index for item in heading_blocks],
                    )
                )
        flush_pending(boundary_reason)
        return chunks

    def _make_text_chunks(
        self,
        text: str,
        source_blocks: list[PdfBlock],
        prefix: str,
        capacity: int,
        boundary_reason: str,
        preferred_level: str,
        heading_block_indexes: list[int],
    ) -> list[TokenChunk]:
        fragments = self._recursive_fragments(text, capacity, preferred_level)
        result: list[TokenChunk] = []
        for index, (fragment, split_level) in enumerate(fragments):
            content = f"{prefix}{fragment}" if prefix else fragment
            result.append(
                self._chunk(
                    content,
                    source_blocks,
                    metadata={
                        "strategy": "structured",
                        "chunk_region": "text",
                        "section_path": self._path_text(prefix),
                        "heading_block_indexes": heading_block_indexes,
                        "split_level": split_level,
                        "boundary_reason": boundary_reason if index == len(fragments) - 1 else split_level,
                    },
                )
            )
        return result

    def _recursive_fragments(self, text: str, capacity: int, preferred_level: str) -> list[tuple[str, str]]:
        if len(self.encoding.encode(text)) <= capacity:
            return [(text, preferred_level)]
        for pattern, level in (
            (self._sentence_pattern, "sentence"),
            (self._punctuation_pattern, "punctuation"),
            (re.compile(r"\s+"), "space"),
        ):
            pieces = [piece.strip() for piece in pattern.split(text) if piece.strip()]
            if len(pieces) <= 1:
                continue
            packed = self._pack_pieces(pieces, capacity)
            if packed:
                result: list[tuple[str, str]] = []
                for item in packed:
                    if len(self.encoding.encode(item)) <= capacity:
                        result.append((item, level))
                    else:
                        result.extend(self._recursive_fragments(item, capacity, level))
                return result
        return self._token_fragments(text, capacity)

    def _pack_pieces(self, pieces: list[str], capacity: int) -> list[str]:
        packed: list[str] = []
        current = ""
        for piece in pieces:
            candidate = f"{current} {piece}".strip()
            if current and len(self.encoding.encode(candidate)) > capacity:
                packed.append(current)
                current = piece
            else:
                current = candidate
        if current:
            packed.append(current)
        return packed

    def _token_fragments(self, text: str, capacity: int) -> list[tuple[str, str]]:
        tokens = self.encoding.encode(text)
        result: list[tuple[str, str]] = []
        start = 0
        while start < len(tokens):
            end = min(start + capacity, len(tokens))
            result.append((self.encoding.decode(tokens[start:end]), "token"))
            start = end
        return result

    def _list_chunks(
        self, items: list[_ListItem], section_path: list[str], boundary_reason: str
    ) -> list[TokenChunk]:
        prefix = self._context_prefix(section_path)
        capacity = max(self.chunk_size - len(self.encoding.encode(prefix)), 1)
        chunks: list[TokenChunk] = []
        current: list[_ListItem] = []

        def flush(reason: str) -> None:
            if not current:
                return
            text = "\n".join(item.analyzed.block.text.strip() for item in current)
            content = f"{prefix}{text}" if prefix else text
            chunks.append(
                self._chunk(
                    content,
                    [item.analyzed.block for item in current],
                    metadata={
                        "strategy": "structured",
                        "chunk_region": "list",
                        "section_path": section_path,
                        "list_levels": [item.analyzed.level for item in current],
                        "list_paths": [item.path for item in current],
                        "boundary_reason": reason,
                    },
                )
            )
            current.clear()

        for item in items:
            candidate = "\n".join([entry.analyzed.block.text.strip() for entry in current] + [item.analyzed.block.text.strip()])
            if current and len(self.encoding.encode(candidate)) > capacity:
                flush("list_item_boundary")
            if len(self.encoding.encode(item.analyzed.block.text)) > capacity:
                for fragment, split_level in self._recursive_fragments(item.analyzed.block.text, capacity, "list_item"):
                    if current:
                        flush("list_item_boundary")
                    current.append(_ListItem(_AnalyzedBlock(item.analyzed.block, "list", item.analyzed.level, item.analyzed.marker), item.path))
                    current[-1] = _ListItem(
                        _AnalyzedBlock(
                            PdfBlock(
                                item.analyzed.block.block_id,
                                item.analyzed.block.block_index,
                                item.analyzed.block.block_type,
                                item.analyzed.block.page_number,
                                fragment,
                                item.analyzed.block.metadata,
                            ),
                            "list",
                            item.analyzed.level,
                            item.analyzed.marker,
                        ),
                        item.path,
                    )
                    flush("list_item_boundary" if split_level != "token" else "token_limit")
                continue
            current.append(item)
        flush(boundary_reason)
        return chunks

    def _table_chunks(self, block: PdfBlock, section_path: list[str]) -> list[TokenChunk]:
        prefix = self._context_prefix(section_path)
        capacity = max(self.chunk_size - len(self.encoding.encode(prefix)), 1)
        slices = split_table_text(block.text, self.encoding, capacity)
        result: list[TokenChunk] = []
        for index, table_slice in enumerate(slices):
            content = f"{prefix}{table_slice.text}" if prefix else table_slice.text
            result.append(
                self._chunk(
                    content,
                    [block],
                    metadata={
                        "strategy": "structured",
                        "chunk_region": "table",
                        "section_path": section_path,
                        "boundary_reason": "table_row_boundary" if len(slices) > 1 else "table_hard_boundary",
                        "table_header_repeated": table_slice.header_repeated,
                        "table_row_range": [table_slice.row_start, table_slice.row_end],
                        "table_chunk_index": index,
                        "table_chunk_count": len(slices),
                    },
                )
            )
        return result

    def _image_chunks(
        self, block: PdfBlock, section_path: list[str], by_index: dict[int, PdfBlock]
    ) -> list[TokenChunk]:
        relative_indexes = self._relation_indexes(block, "relative")
        nearby_indexes = self._relation_indexes(block, "nearby")
        related_blocks = [by_index[index] for index in relative_indexes if index in by_index and by_index[index].text.strip()]
        parts = [block.text.strip(), *(item.text.strip() for item in related_blocks)]
        caption = block.metadata.get("img_caption_text") or block.metadata.get("table_caption_text")
        if caption:
            parts.insert(1, str(caption).strip())
        text = "\n".join(part for part in parts if part)
        if not text:
            return []
        prefix = self._context_prefix(section_path)
        capacity = max(self.chunk_size - len(self.encoding.encode(prefix)), 1)
        result: list[TokenChunk] = []
        for fragment, split_level in self._recursive_fragments(text, capacity, "image"):
            content = f"{prefix}{fragment}" if prefix else fragment
            result.append(
                self._chunk(
                    content,
                    [block, *related_blocks],
                    metadata={
                        "strategy": "structured",
                        "chunk_region": "image",
                        "section_path": section_path,
                        "split_level": split_level,
                        "image_blocks": [block.block_index],
                        "image_text_blocks": [block.block_index] if block.text.strip() else [],
                        "pure_image_blocks": [] if text else [block.block_index],
                        "relative_block_indexes": relative_indexes,
                        "nearby_block_indexes": nearby_indexes,
                        "boundary_reason": "image_boundary",
                    },
                )
            )
        return result

    @staticmethod
    def _relation_indexes(block: PdfBlock, relation: str) -> list[int]:
        keys = {
            "relative": ("relative_block_indexes", "related_text_block_indexes"),
            "nearby": ("nearby_block_indexes", "nearby_text_block_indexes"),
        }[relation]
        for key in keys:
            value = block.metadata.get(key)
            if isinstance(value, list):
                return [int(item) for item in value if str(item).lstrip("-").isdigit()]
        return []

    def _chunk(self, text: str, blocks: Iterable[PdfBlock], metadata: dict) -> TokenChunk:
        source_blocks = list(blocks)
        tokens = self.encoding.encode(text)
        return TokenChunk(
            chunk_index=0,
            text=text,
            token_count=len(tokens),
            start_token=0,
            end_token=len(tokens),
            start_char=0,
            end_char=len(text),
            block_types=self._unique(block.block_type for block in source_blocks),
            block_indexes=self._unique(block.block_index for block in source_blocks),
            metadata=metadata,
        )

    def _title_only_chunk(self, block: PdfBlock, section_path: list[str]) -> TokenChunk:
        return self._chunk(
            block.text.strip(),
            [block],
            {
                "strategy": "structured",
                "chunk_region": "text",
                "section_path": section_path,
                "split_level": "title",
                "boundary_reason": "document_end",
            },
        )

    def _context_prefix(self, section_path: list[str]) -> str:
        return "\n".join(section_path) + "\n" if section_path else ""

    @staticmethod
    def _path_text(prefix: str) -> list[str]:
        return [line for line in prefix.strip().splitlines() if line]

    @staticmethod
    def _unique(values: Iterable) -> list:
        result = []
        for value in values:
            if value not in result:
                result.append(value)
        return result

    @staticmethod
    def _renumber(chunk: TokenChunk, index: int) -> TokenChunk:
        return TokenChunk(
            chunk_index=index,
            text=chunk.text,
            token_count=chunk.token_count,
            start_token=chunk.start_token,
            end_token=chunk.end_token,
            start_char=chunk.start_char,
            end_char=chunk.end_char,
            block_types=chunk.block_types,
            block_indexes=chunk.block_indexes,
            metadata=chunk.metadata,
        )
