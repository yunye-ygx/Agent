import re
from dataclasses import dataclass

import tiktoken

from app.rag.chunking_schemas import PdfBlock, TokenChunk
from app.rag.errors import DocumentValidationError
from app.rag.table_chunker import split_table_text


@dataclass(frozen=True)
class _BodySpan:
    block: PdfBlock
    start_char: int
    end_char: int
    start_token: int
    end_token: int


class SectionAwareChunker:
    """Chunk by paragraph boundaries while repeating the active heading context."""

    encoding_name = "cl100k_base"
    _numbered_heading_pattern = re.compile(
        r"^\s*(\d+(?:\.\d+)*)(?:[.)、]|\s)"
    )

    def __init__(self, chunk_size: int = 500, overlap: int = 80) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须大于 0")
        if overlap < 0 or overlap >= chunk_size:
            raise ValueError("overlap 必须大于等于 0 且小于 chunk_size")
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.encoding = tiktoken.get_encoding(self.encoding_name)
        self.document_title: str | None = None

    def split(self, blocks: list[PdfBlock]) -> list[TokenChunk]:
        self.document_title = None
        chunks: list[TokenChunk] = []
        heading_stack: list[tuple[int, PdfBlock]] = []
        body_blocks: list[PdfBlock] = []

        def flush_body() -> None:
            if body_blocks:
                chunks.extend(self._body_chunks(body_blocks, heading_stack))
                body_blocks.clear()

        for block in blocks:
            if not block.text.strip():
                continue
            heading_level = self._heading_level(block)
            if block.block_type == "table":
                flush_body()
                chunks.extend(self._table_chunks(block, heading_stack))
            elif heading_level is not None:
                if (
                    self.document_title is None
                    and not heading_stack
                    and not body_blocks
                    and not chunks
                    and self._is_document_title(block, heading_level)
                ):
                    self.document_title = block.text.strip()
                    continue
                flush_body()
                while heading_stack and heading_stack[-1][0] >= heading_level:
                    heading_stack.pop()
                heading_stack.append((heading_level, block))
            else:
                body_blocks.append(block)
        flush_body()

        if not chunks:
            raise DocumentValidationError("可切块的文本不能为空")
        return [
            TokenChunk(
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
            for index, chunk in enumerate(chunks)
        ]

    def _body_chunks(
        self,
        blocks: list[PdfBlock],
        heading_stack: list[tuple[int, PdfBlock]],
    ) -> list[TokenChunk]:
        combined, spans = self._join_body_blocks(blocks)
        body_tokens = self.encoding.encode(combined)
        if not body_tokens:
            return []

        headings = [block for _, block in heading_stack]
        section_path = [block.text.strip() for block in headings]
        prefix = "\n".join(section_path)
        prefix_text = f"{prefix}\n" if prefix else ""
        prefix_token_count = len(self.encoding.encode(prefix_text))
        body_capacity = max(self.chunk_size - prefix_token_count, 1)
        effective_overlap = min(self.overlap, max(body_capacity - 1, 0))
        byte_offsets = self._byte_offsets(
            [self.encoding.decode_single_token_bytes(token) for token in body_tokens]
        )
        body_spans = self._token_spans(combined, spans, byte_offsets)

        chunks: list[TokenChunk] = []
        start = 0
        while start < len(body_tokens):
            target_end = min(start + body_capacity, len(body_tokens))
            paragraph_ends = [
                span.end_token
                for span in body_spans
                if start < span.end_token <= target_end
            ]
            end = max(paragraph_ends, default=target_end)
            boundary_reason = "paragraph_boundary" if paragraph_ends else "token_limit"
            if end <= start:
                end = min(start + body_capacity, len(body_tokens))
                boundary_reason = "token_limit"

            body_text = self.encoding.decode(body_tokens[start:end])
            chunk_text = f"{prefix_text}{body_text}"
            matching_spans = [
                span
                for span in body_spans
                if span.start_token < end and span.end_token > start
            ]
            source_blocks = self._unique_blocks(
                [block for _, block in heading_stack]
                + [span.block for span in matching_spans]
            )
            if end == len(body_tokens):
                boundary_reason = "document_end" if not paragraph_ends else boundary_reason
            chunks.append(
                TokenChunk(
                    chunk_index=0,
                    text=chunk_text,
                    token_count=len(self.encoding.encode(chunk_text)),
                    start_token=start,
                    end_token=end,
                    start_char=_token_to_char(combined, byte_offsets[start]),
                    end_char=_token_to_char(combined, byte_offsets[end]),
                    block_types=self._unique_values(block.block_type for block in source_blocks),
                    block_indexes=[block.block_index for block in source_blocks],
                    metadata={
                        "strategy": "section",
                        "document_title": self.document_title,
                        "section_path": section_path,
                        "heading_block_indexes": [block.block_index for block in headings],
                        "heading_levels": [level for level, _ in heading_stack],
                        "raw_text_levels": [self._raw_text_level(block) for block in headings],
                        "prefix_token_count": prefix_token_count,
                        "effective_overlap": effective_overlap,
                        "body_token_range": [start, end],
                        "boundary_reason": boundary_reason,
                    },
                )
            )
            if end == len(body_tokens):
                break
            next_start = max(end - effective_overlap, start + 1)
            start = next_start
        return chunks

    def _table_chunks(
        self,
        block: PdfBlock,
        heading_stack: list[tuple[int, PdfBlock]],
    ) -> list[TokenChunk]:
        section_path = [heading.text.strip() for _, heading in heading_stack]
        table_slices = split_table_text(block.text, self.encoding, self.chunk_size)
        chunks = []
        for table_slice in table_slices:
            tokens = self.encoding.encode(table_slice.text)
            chunks.append(
                TokenChunk(
                    chunk_index=0,
                    text=table_slice.text,
                    token_count=len(tokens),
                    start_token=0,
                    end_token=len(tokens),
                    start_char=0,
                    end_char=len(table_slice.text),
                    block_types=[block.block_type],
                    block_indexes=[block.block_index],
                    metadata={
                        "strategy": "section",
                        "document_title": self.document_title,
                        "section_path": section_path,
                        "heading_block_indexes": [block.block_index for _, block in heading_stack],
                        "heading_levels": [level for level, _ in heading_stack],
                        "raw_text_levels": [self._raw_text_level(block) for _, block in heading_stack],
                        "boundary_reason": (
                            "table_row_boundary"
                            if len(table_slices) > 1
                            else "table_hard_boundary"
                        ),
                        "table_header_repeated": table_slice.header_repeated,
                        "table_row_range": [table_slice.row_start, table_slice.row_end],
                        "table_chunk_index": len(chunks),
                        "table_chunk_count": len(table_slices),
                    },
                )
            )
        return chunks

    @classmethod
    def _heading_level(cls, block: PdfBlock) -> int | None:
        if block.block_type == "table":
            return None
        raw_level = cls._raw_text_level(block)
        is_heading = block.block_type == "title" or raw_level is not None
        if not is_heading:
            return None

        numbered_heading = cls._numbered_heading_pattern.match(block.text)
        if numbered_heading:
            return len(numbered_heading.group(1).split("."))
        if raw_level is not None:
            return raw_level
        return 1

    @staticmethod
    def _raw_text_level(block: PdfBlock) -> int | None:
        level = block.metadata.get("text_level")
        if level is None:
            return None
        try:
            parsed_level = int(level)
        except (TypeError, ValueError):
            return None
        return parsed_level if parsed_level > 0 else None

    @staticmethod
    def _is_document_title(block: PdfBlock, heading_level: int) -> bool:
        raw_type = str(block.metadata.get("type") or "").lower()
        if raw_type == "doc_title":
            return True
        return heading_level == 1 and not re.match(r"^\d+(?:\.\d+)*[.、\s]", block.text.strip())

    @staticmethod
    def _join_body_blocks(
        blocks: list[PdfBlock],
    ) -> tuple[str, list[tuple[PdfBlock, int, int]]]:
        parts: list[str] = []
        spans: list[tuple[PdfBlock, int, int]] = []
        cursor = 0
        for index, block in enumerate(blocks):
            if index:
                parts.append("\n")
                cursor += 1
            start = cursor
            parts.append(block.text)
            cursor += len(block.text)
            spans.append((block, start, cursor))
        return "".join(parts), spans

    def _token_spans(
        self,
        combined: str,
        spans: list[tuple[PdfBlock, int, int]],
        byte_offsets: list[int],
    ) -> list[_BodySpan]:
        return [
            _BodySpan(
                block=block,
                start_char=start_char,
                end_char=end_char,
                start_token=_offset_to_token(
                    byte_offsets,
                    len(combined[:start_char].encode("utf-8")),
                    round_up=False,
                ),
                end_token=_offset_to_token(
                    byte_offsets,
                    len(combined[:end_char].encode("utf-8")),
                    round_up=True,
                ),
            )
            for block, start_char, end_char in spans
        ]

    @staticmethod
    def _byte_offsets(token_bytes: list[bytes]) -> list[int]:
        offsets = [0]
        for token in token_bytes:
            offsets.append(offsets[-1] + len(token))
        return offsets

    @staticmethod
    def _unique_blocks(blocks: list[PdfBlock]) -> list[PdfBlock]:
        result: list[PdfBlock] = []
        seen: set[int] = set()
        for block in blocks:
            if block.block_index not in seen:
                seen.add(block.block_index)
                result.append(block)
        return result

    @staticmethod
    def _unique_values(values) -> list[str]:
        result: list[str] = []
        for value in values:
            if value not in result:
                result.append(value)
        return result


def _offset_to_token(offsets: list[int], target: int, *, round_up: bool) -> int:
    for index, offset in enumerate(offsets):
        if offset == target:
            return index
        if offset > target:
            return index if round_up else max(index - 1, 0)
    return len(offsets) - 1


def _token_to_char(text: str, byte_offset: int) -> int:
    return len(text.encode("utf-8")[:byte_offset].decode("utf-8", errors="ignore"))
