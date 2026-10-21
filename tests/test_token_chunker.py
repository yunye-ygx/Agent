import pytest

from app.rag.chunking_schemas import PdfBlock
from app.rag.section_chunker import SectionAwareChunker
from app.rag.structure_chunker import StructureAwareChunker
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


def test_structure_chunker_flushes_text_before_table() -> None:
    result = StructureAwareChunker(chunk_size=100, overlap=0).split(
        [
            PdfBlock("block-0000", 0, "text", 1, "正文内容"),
            PdfBlock("block-0001", 1, "table", 1, "A | B"),
            PdfBlock("block-0002", 2, "text", 1, "表格之后的正文"),
        ]
    )

    assert [item.block_types for item in result] == [["text"], ["table"], ["text"]]
    assert [item.block_indexes for item in result] == [[0], [1], [2]]
    assert result[1].text == "A | B"


def test_structure_chunker_does_not_overlap_across_table() -> None:
    result = StructureAwareChunker(chunk_size=4, overlap=1).split(
        [
            PdfBlock("block-0000", 0, "text", 1, "one two three four five"),
            PdfBlock("block-0001", 1, "table", 1, "A | B"),
            PdfBlock("block-0002", 2, "text", 1, "six seven eight nine"),
        ]
    )

    table_index = next(index for index, item in enumerate(result) if item.block_types == ["table"])
    assert result[table_index - 1].block_types == ["text"]
    assert result[table_index + 1].block_types == ["text"]
    assert "A | B" not in result[table_index - 1].text
    assert "A | B" not in result[table_index + 1].text


def test_structure_chunker_splits_large_table_on_rows_and_repeats_header() -> None:
    table = "| Name | Description |\n| --- | --- |\n| A | first row |\n| B | second row |\n| C | third row |"
    result = StructureAwareChunker(chunk_size=2, overlap=0).split(
        [PdfBlock("block-0000", 0, "table", 1, table)]
    )

    assert len(result) > 1
    assert all(item.block_types == ["table"] for item in result)
    assert all(item.block_indexes == [0] for item in result)
    assert all(item.metadata["table_header_repeated"] for item in result)
    assert all(item.text.splitlines()[:2] == ["| Name | Description |", "| --- | --- |"] for item in result)
    assert "| A | first row |" in "\n".join(item.text for item in result)
    assert "| B | second row |" in "\n".join(item.text for item in result)
    assert "| C | third row |" in "\n".join(item.text for item in result)


def test_section_chunker_splits_large_table_without_losing_section_context() -> None:
    table = "| Name | Description |\n| --- | --- |\n| A | first row |\n| B | second row |\n| C | third row |"
    result = SectionAwareChunker(chunk_size=2, overlap=0).split(
        [
            PdfBlock("block-0000", 0, "text", 1, "1. 数据", {"text_level": 2}),
            PdfBlock("block-0001", 1, "table", 1, table),
        ]
    )

    assert len(result) > 1
    assert all(item.metadata["section_path"] == ["1. 数据"] for item in result)
    assert all(item.metadata["table_header_repeated"] for item in result)
    assert all(item.metadata["boundary_reason"] == "table_row_boundary" for item in result)


def test_section_chunker_repeats_heading_for_each_large_body_chunk() -> None:
    result = SectionAwareChunker(chunk_size=12, overlap=2).split(
        [
            PdfBlock("block-0000", 0, "text", 1, "RAG Chunking 综合测试文档", {"text_level": 1}),
            PdfBlock("block-0001", 1, "text", 1, "数据库连接配置", {"text_level": 2}),
            PdfBlock(
                "block-0002",
                2,
                "text",
                1,
                "系统启动时读取数据库地址。服务会检查连接是否可用。连接失败时记录错误日志。",
            ),
        ]
    )

    assert len(result) > 1
    assert all("数据库连接配置" in item.text for item in result)
    assert all("RAG Chunking 综合测试文档" not in item.text for item in result)
    assert all(item.metadata["section_path"] == ["数据库连接配置"] for item in result)
    assert all(item.metadata["heading_block_indexes"] == [1] for item in result)


def test_section_chunker_prefers_paragraph_boundary() -> None:
    result = SectionAwareChunker(chunk_size=12, overlap=0).split(
        [
            PdfBlock("block-0000", 0, "text", 1, "文档标题", {"text_level": 1}),
            PdfBlock("block-0001", 1, "title", 1, "使用说明"),
            PdfBlock("block-0002", 2, "text", 1, "第一段说明内容。"),
            PdfBlock("block-0003", 3, "text", 1, "第二段说明内容。"),
        ]
    )

    assert len(result) == 2
    assert result[0].metadata["boundary_reason"] == "paragraph_boundary"
    assert result[0].block_indexes == [1, 2]
    assert result[1].block_indexes == [1, 3]


def test_section_chunker_keeps_document_title_out_of_heading_context() -> None:
    chunker = SectionAwareChunker(chunk_size=100, overlap=0)
    result = chunker.split(
        [
            PdfBlock("block-0000", 0, "text", 1, "RAG Chunking 综合测试文档", {"text_level": 1}),
            PdfBlock("block-0001", 1, "text", 1, "1. 普通连续文本", {"text_level": 2}),
            PdfBlock("block-0002", 2, "text", 1, "这里是正文。"),
        ]
    )

    assert chunker.document_title == "RAG Chunking 综合测试文档"
    assert all("RAG Chunking 综合测试文档" not in item.text for item in result)
    assert result[0].text.startswith("1. 普通连续文本")
    assert result[0].block_indexes == [1, 2]
    assert result[0].metadata["document_title"] == "RAG Chunking 综合测试文档"


def test_section_chunker_uses_numbering_to_preserve_nested_heading_context() -> None:
    result = SectionAwareChunker(chunk_size=100, overlap=0).split(
        [
            PdfBlock("block-0000", 0, "text", 1, "文档标题", {"text_level": 1}),
            PdfBlock("block-0001", 1, "text", 2, "5. 多级标题", {"text_level": 2}),
            PdfBlock("block-0002", 2, "text", 2, "5.1 文档解析", {"text_level": 2}),
            PdfBlock("block-0003", 3, "text", 2, "5.1.1 PDF 解析", {"text_level": 2}),
            PdfBlock("block-0004", 4, "text", 2, "PDF 解析正文。"),
        ]
    )

    assert result[0].metadata["section_path"] == [
        "5. 多级标题",
        "5.1 文档解析",
        "5.1.1 PDF 解析",
    ]
    assert result[0].metadata["heading_levels"] == [1, 2, 3]
    assert result[0].metadata["raw_text_levels"] == [2, 2, 2]
    assert result[0].block_indexes == [1, 2, 3, 4]


def test_section_chunker_drops_only_the_same_logical_heading_level() -> None:
    result = SectionAwareChunker(chunk_size=100, overlap=0).split(
        [
            PdfBlock("block-0000", 0, "text", 1, "文档标题", {"text_level": 1}),
            PdfBlock("block-0001", 1, "text", 1, "5. 多级标题", {"text_level": 2}),
            PdfBlock("block-0007", 7, "text", 1, "第 5 节正文。"),
            PdfBlock("block-0002", 2, "text", 1, "5.1 文档解析", {"text_level": 2}),
            PdfBlock("block-0008", 8, "text", 1, "第 5.1 节正文。"),
            PdfBlock("block-0003", 3, "text", 1, "5.1.1 PDF 解析", {"text_level": 2}),
            PdfBlock("block-0009", 9, "text", 1, "第 5.1.1 节正文。"),
            PdfBlock("block-0004", 4, "text", 1, "5.1.2 Word 解析", {"text_level": 2}),
            PdfBlock("block-0010", 10, "text", 1, "第 5.1.2 节正文。"),
            PdfBlock("block-0005", 5, "text", 1, "5.2 文档切块", {"text_level": 2}),
            PdfBlock("block-0006", 6, "text", 1, "正文内容。"),
        ]
    )

    assert [item.metadata["section_path"] for item in result] == [
        ["5. 多级标题"],
        ["5. 多级标题", "5.1 文档解析"],
        ["5. 多级标题", "5.1 文档解析", "5.1.1 PDF 解析"],
        ["5. 多级标题", "5.1 文档解析", "5.1.2 Word 解析"],
        ["5. 多级标题", "5.2 文档切块"],
    ]
