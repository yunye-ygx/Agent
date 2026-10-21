import pytest

from app.rag.chunking_schemas import PdfBlock
from app.rag.structured_chunker import StructuredChunker


def block(index: int, kind: str, text: str, **metadata: object) -> PdfBlock:
    return PdfBlock(
        block_id=f"block-{index:04d}",
        block_index=index,
        block_type=kind,
        page_number=1,
        text=text,
        metadata=dict(metadata),
    )


def test_title_is_context_for_following_body_not_an_independent_chunk() -> None:
    result = StructuredChunker(chunk_size=100, overlap=0).split(
        [
            block(0, "title", "5. 数据库设计"),
            block(1, "text", "MySQL 保存业务状态。"),
            block(2, "text", "RabbitMQ 负责异步任务。"),
        ]
    )

    assert len(result) == 1
    assert result[0].text.startswith("5. 数据库设计\n")
    assert result[0].block_indexes == [0, 1, 2]
    assert result[0].metadata["section_path"] == ["5. 数据库设计"]
    assert result[0].metadata["boundary_reason"] == "document_end"


def test_long_body_prefers_sentence_boundary_before_token_fallback() -> None:
    result = StructuredChunker(chunk_size=12, overlap=0).split(
        [
            block(
                0,
                "text",
                "第一句说明系统启动流程。第二句说明数据库连接。第三句说明失败处理。",
            )
        ]
    )

    assert len(result) > 1
    assert all(item.text.rstrip().endswith(("。", ".")) for item in result[:-1])
    assert all(item.metadata["split_level"] in {"sentence", "token"} for item in result)


def test_numbered_list_is_inferred_and_keeps_hierarchy_path() -> None:
    result = StructuredChunker(chunk_size=100, overlap=0).split(
        [
            block(0, "text", "5 数据库设计"),
            block(1, "text", "5.1 MySQL"),
            block(2, "text", "5.1.1 documents"),
            block(3, "text", "5.1.2 generations"),
            block(4, "text", "5.2 Elasticsearch"),
        ]
    )

    assert len(result) == 1
    assert result[0].metadata["list_levels"] == [1, 2, 3, 3, 2]
    assert result[0].metadata["list_paths"] == [
        ["5 数据库设计"],
        ["5 数据库设计", "5.1 MySQL"],
        ["5 数据库设计", "5.1 MySQL", "5.1.1 documents"],
        ["5 数据库设计", "5.1 MySQL", "5.1.2 generations"],
        ["5 数据库设计", "5.2 Elasticsearch"],
    ]


def test_list_overflow_splits_at_item_boundary_and_preserves_path() -> None:
    result = StructuredChunker(chunk_size=12, overlap=0).split(
        [
            block(0, "list", "1. 第一项包含较长的说明内容。"),
            block(1, "list", "1.1 子项包含较长的说明内容。"),
            block(2, "list", "1.2 另一子项包含较长的说明内容。"),
        ]
    )

    assert len(result) > 1
    assert all(item.metadata["chunk_region"] == "list" for item in result)
    assert all(item.metadata["list_paths"] for item in result)
    assert all(item.block_indexes for item in result)


def test_large_table_keeps_rows_together_and_repeats_header() -> None:
    table = "| 名称 | 说明 |\n| --- | --- |\n| A | 第一行 |\n| B | 第二行 |\n| C | 第三行 |"

    result = StructuredChunker(chunk_size=2, overlap=0).split([block(0, "table", table)])

    assert len(result) > 1
    assert all(item.block_types == ["table"] for item in result)
    assert all(item.metadata["table_header_repeated"] for item in result)
    assert all(item.text.splitlines()[:2] == ["| 名称 | 说明 |", "| --- | --- |"] for item in result)


def test_image_keeps_relative_text_and_records_nearby_candidates() -> None:
    result = StructuredChunker(chunk_size=100, overlap=0).split(
        [
            block(
                0,
                "image",
                "",
                img_path="flow.png",
                related_text_block_indexes=[1],
                nearby_text_block_indexes=[2],
            ),
            block(1, "text", "图中展示审批流。"),
            block(2, "text", "这是同页的旁栏文字。"),
        ]
    )

    assert len(result) == 2
    assert result[0].metadata["image_blocks"] == [0]
    assert result[0].metadata["relative_block_indexes"] == [1]
    assert result[0].metadata["nearby_block_indexes"] == [2]
    assert "图中展示审批流。" in result[0].text
    assert "旁栏文字" not in result[0].text
    assert result[1].block_indexes == [2]


def test_only_pure_image_without_text_does_not_create_empty_chunk() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        StructuredChunker(chunk_size=100, overlap=0).split(
            [block(0, "image", "", img_path="pure.png")]
        )


def test_structured_strategy_has_a_distinct_fingerprint_version() -> None:
    assert StructuredChunker.strategy_version == "structured-v1"
