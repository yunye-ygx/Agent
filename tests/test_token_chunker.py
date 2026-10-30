import pytest

from app.rag.chunking_schemas import PdfBlock
from app.rag.token_chunker import TokenChunker


def test_split_respects_token_window_and_overlap() -> None:
    result = TokenChunker(chunk_size=4, overlap=1).split(
        [
            PdfBlock(
                block_id="block-0000",
                block_index=0,
                block_type="text",
                page_number=1,
                text="one two three four five six",
            )
        ]
    )

    assert len(result) > 1
    assert all(item.token_count <= 4 for item in result)
    assert result[0].end_token - result[1].start_token == 1
    assert result[0].start_char == 0
    assert result[0].end_char > result[0].start_char


def test_split_keeps_tail_and_block_types() -> None:
    result = TokenChunker(chunk_size=100, overlap=0).split(
        [
            PdfBlock(
                block_id="block-0000",
                block_index=0,
                block_type="title",
                page_number=1,
                text="Title",
            ),
            PdfBlock(
                block_id="block-0001",
                block_index=1,
                block_type="table",
                page_number=1,
                text="A | B",
            ),
        ]
    )

    assert result[-1].token_count > 0
    assert result[0].block_types == ["title", "table"]


def test_split_marks_image_text_and_pure_image_sources() -> None:
    result = TokenChunker(chunk_size=100, overlap=0).split(
        [
            PdfBlock(
                block_id="block-0000",
                block_index=0,
                block_type="image",
                page_number=1,
                text="OCR flow text",
                metadata={"image_kind": "text_image", "img_path": "flow.png"},
            ),
            PdfBlock(
                block_id="block-0001",
                block_index=1,
                block_type="image",
                page_number=1,
                text="",
                metadata={"image_kind": "pure_image", "img_path": "pure.png"},
            ),
        ]
    )

    assert result[0].metadata["image_text_blocks"] == [0]
    assert result[0].metadata["pure_image_blocks"] == []


def test_invalid_chunk_configuration_is_rejected() -> None:
    with pytest.raises(ValueError, match="overlap"):
        TokenChunker(chunk_size=4, overlap=4)


def test_empty_blocks_do_not_create_chunks() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        TokenChunker(chunk_size=4, overlap=0).split(
            [
                PdfBlock(
                    block_id="block-0000",
                    block_index=0,
                    block_type="image",
                    page_number=1,
                    text="",
                )
            ]
        )
