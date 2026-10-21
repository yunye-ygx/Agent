from dataclasses import dataclass

import tiktoken

from app.rag.chunking_schemas import PdfBlock, TokenChunk
from app.rag.errors import DocumentValidationError


@dataclass(frozen=True)
class _BlockSpan:
    block: PdfBlock
    start_char: int
    end_char: int
    start_token: int
    end_token: int


class TokenChunker:
    """Split normalized PDF blocks into deterministic token windows."""

    encoding_name = "cl100k_base"

    def __init__(self, chunk_size: int = 500, overlap: int = 80) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须大于 0")
        if overlap < 0 or overlap >= chunk_size:
            raise ValueError("overlap 必须大于等于 0 且小于 chunk_size")
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.encoding = tiktoken.get_encoding(self.encoding_name)

    def split(self, blocks: list[PdfBlock]) -> list[TokenChunk]:
        non_empty = [block for block in blocks if block.text.strip()]
        if not non_empty:
            raise DocumentValidationError("可切块的文本不能为空")

        combined, char_spans = self._join_blocks(non_empty)
        tokens = self.encoding.encode(combined)
        token_bytes = [self.encoding.decode_single_token_bytes(token) for token in tokens]
        byte_offsets = self._byte_offsets(token_bytes)
        block_spans = self._block_spans(combined, char_spans, byte_offsets)

        chunks: list[TokenChunk] = []
        start_token = 0
        chunk_index = 0
        step = self.chunk_size - self.overlap
        while start_token < len(tokens):
            end_token = min(start_token + self.chunk_size, len(tokens))
            chunk_tokens = tokens[start_token:end_token]
            chunk_text = self.encoding.decode(chunk_tokens)
            start_byte = byte_offsets[start_token]
            end_byte = byte_offsets[end_token]
            start_char = _byte_to_char(combined, start_byte)
            end_char = _byte_to_char(combined, end_byte)
            matching_spans = [
                span
                for span in block_spans
                if span.start_token < end_token and span.end_token > start_token
            ]
            block_types = _unique_in_order(span.block.block_type for span in matching_spans)
            block_indexes = [span.block.block_index for span in matching_spans]
            image_text_blocks = [
                span.block.block_index
                for span in matching_spans
                if span.block.block_type == "image" and span.block.text.strip()
            ]
            chunks.append(
                TokenChunk(
                    chunk_index=chunk_index,
                    text=chunk_text,
                    token_count=len(chunk_tokens),
                    start_token=start_token,
                    end_token=end_token,
                    start_char=start_char,
                    end_char=end_char,
                    block_types=block_types,
                    block_indexes=block_indexes,
                    metadata={
                        "image_text_blocks": image_text_blocks,
                        "pure_image_blocks": [],
                    },
                )
            )
            chunk_index += 1
            if end_token == len(tokens):
                break
            start_token += step
        return chunks

    def _join_blocks(self, blocks: list[PdfBlock]) -> tuple[str, list[tuple[PdfBlock, int, int]]]:
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

    def _block_spans(
        self,
        combined: str,
        char_spans: list[tuple[PdfBlock, int, int]],
        byte_offsets: list[int],
    ) -> list[_BlockSpan]:
        result: list[_BlockSpan] = []
        for block, start_char, end_char in char_spans:
            start_byte = len(combined[:start_char].encode("utf-8"))
            end_byte = len(combined[:end_char].encode("utf-8"))
            start_token = _offset_to_token(byte_offsets, start_byte, round_up=False)
            end_token = _offset_to_token(byte_offsets, end_byte, round_up=True)
            result.append(_BlockSpan(block, start_char, end_char, start_token, end_token))
        return result

    @staticmethod
    def _byte_offsets(token_bytes: list[bytes]) -> list[int]:
        offsets = [0]
        for token in token_bytes:
            offsets.append(offsets[-1] + len(token))
        return offsets


def _offset_to_token(offsets: list[int], target: int, *, round_up: bool) -> int:
    for index, offset in enumerate(offsets):
        if offset == target:
            return index
        if offset > target:
            return index if round_up else max(index - 1, 0)
    return len(offsets) - 1


def _byte_to_char(text: str, byte_offset: int) -> int:
    return len(text.encode("utf-8")[:byte_offset].decode("utf-8", errors="ignore"))


def _unique_in_order(values):
    result = []
    for value in values:
        if value not in result:
            result.append(value)
    return result
