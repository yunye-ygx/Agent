import tiktoken

from app.rag.chunking_schemas import PdfBlock, TokenChunk
from app.rag.errors import DocumentValidationError
from app.rag.table_chunker import split_table_text
from app.rag.token_chunker import TokenChunker


class StructureAwareChunker:
    """Keep table blocks as hard boundaries around token-based text chunks."""

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
        chunks: list[TokenChunk] = []
        text_run: list[PdfBlock] = []

        def flush_text_run() -> None:
            if not text_run:
                return
            chunks.extend(TokenChunker(self.chunk_size, self.overlap).split(text_run))
            text_run.clear()

        for block in blocks:
            if not block.text.strip():
                continue
            if block.block_type == "table":
                flush_text_run()
                chunks.extend(self._table_chunks(block))
            else:
                text_run.append(block)
        flush_text_run()

        if not chunks:
            raise DocumentValidationError("可切块的文本不能为空")
        return [
            TokenChunk(chunk_index=index, **{key: value for key, value in chunk.to_dict().items() if key != "chunk_index"})
            for index, chunk in enumerate(chunks)
        ]

    def _table_chunks(self, block: PdfBlock) -> list[TokenChunk]:
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
