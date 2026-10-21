import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.rag.mineru_parser import (
    MinerUExecutionError,
    MinerUParser,
    normalize_content_list,
)

FIXTURE = Path(__file__).parent / "fixtures" / "mineru" / "content_list.json"


def test_normalize_content_list_maps_common_mineru_blocks() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    blocks = normalize_content_list(payload)

    assert [block.block_type for block in blocks] == ["title", "text", "table", "image"]
    assert blocks[0].block_id == "block-0000"
    assert blocks[0].page_number == 1
    assert blocks[2].text.startswith("| Type | Notice |")
    assert blocks[3].text == ""
    assert blocks[3].metadata["img_path"] == "images/figure-1.png"
    assert blocks[3].metadata["img_caption"] == ["Approval flow"]


def test_normalize_image_block_keeps_ocr_and_classifies_image_kind() -> None:
    blocks = normalize_content_list(
        [
            {
                "type": "image",
                "img_path": "images/table-screenshot.png",
                "image_caption": ["Database fields"],
                "image_footnote": ["OCR candidate"],
                "ocr_text": "| id | content |\n| --- | --- |\n| 1 | PDF |",
                "image_kind": "table_image",
                "page_idx": 2,
            }
        ]
    )

    assert blocks[0].block_type == "image"
    assert blocks[0].text.startswith("| id | content |")
    assert blocks[0].metadata["ocr_text"].startswith("| id | content |")
    assert blocks[0].metadata["image_kind"] == "table_image"
    assert blocks[0].metadata["img_caption_text"] == "Database fields"
    assert blocks[0].metadata["image_footnote_text"] == "OCR candidate"


def test_normalize_links_text_inside_image_bbox_without_claiming_ocr() -> None:
    blocks = normalize_content_list(
        [
            {
                "type": "image",
                "img_path": "images/diagram.jpg",
                "page_idx": 0,
                "bbox": [100, 100, 500, 500],
            },
            {
                "type": "text",
                "text": "MinerU",
                "page_idx": 0,
                "bbox": [200, 200, 300, 230],
            },
            {
                "type": "text",
                "text": "正文标题",
                "page_idx": 0,
                "bbox": [100, 700, 400, 730],
            },
        ]
    )

    image = blocks[0]
    related = blocks[1]
    outside = blocks[2]

    assert image.metadata["image_text_status"] == "related_text_candidates"
    assert image.metadata["related_text_block_indexes"] == [1]
    assert related.metadata["image_relation"] == "inside_image_bbox_candidate"
    assert related.metadata["related_image_block_indexes"] == [0]
    assert outside.metadata.get("image_relation") is None


def test_normalize_records_nearby_text_as_a_candidate_when_image_bbox_is_partial() -> None:
    blocks = normalize_content_list(
        [
            {
                "type": "image",
                "img_path": "images/diagram.jpg",
                "page_idx": 0,
                "bbox": [100, 400, 500, 500],
            },
            {
                "type": "text",
                "text": "RAG document flow",
                "page_idx": 0,
                "bbox": [120, 300, 350, 330],
            },
        ]
    )

    image = blocks[0]
    text = blocks[1]

    assert image.metadata["image_text_status"] == "nearby_text_candidates"
    assert image.metadata["nearby_text_block_indexes"] == [1]
    assert text.metadata["image_relation"] == "nearby_image_bbox_candidate"


def test_normalize_table_keeps_structured_content_and_visual_source() -> None:
    blocks = normalize_content_list(
        [
            {
                "type": "table",
                "table_body": "<table><tr><td>id</td></tr></table>",
                "img_path": "images/table.jpg",
                "table_caption": ["Fields"],
                "page_idx": 1,
            }
        ]
    )

    table = blocks[0]
    assert table.block_type == "table"
    assert table.text.startswith("<table>")
    assert table.metadata["source_image_kind"] == "table_image"
    assert table.metadata["source_image_path"] == "images/table.jpg"
    assert table.metadata["table_caption_text"] == "Fields"


def test_normalize_unknown_type_and_text_fallback() -> None:
    blocks = normalize_content_list(
        [{"type": "custom", "content": "unknown content", "page_idx": 2}]
    )

    assert blocks[0].block_id == "block-0000"
    assert blocks[0].block_type == "unknown"
    assert blocks[0].page_number == 3
    assert blocks[0].text == "unknown content"
    assert blocks[0].metadata["type"] == "custom"


def test_parser_maps_missing_executable_to_typed_error(tmp_path: Path) -> None:
    parser = MinerUParser(command=str(tmp_path / "missing-mineru"), temp_root=tmp_path)

    with pytest.raises(MinerUExecutionError, match="MinerU 命令不可用"):
        parser.parse_pdf("manual.pdf", b"%PDF-1.7")


def test_parser_reads_content_list_from_mineru_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def fake_run(args, **kwargs):
        output_dir = Path(args[args.index("-o") + 1])
        output_path = output_dir / "manual_content_list.json"
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("app.rag.mineru_parser.subprocess.run", fake_run)
    parser = MinerUParser(command="mineru", temp_root=tmp_path)

    result = parser.parse_pdf("manual.pdf", b"%PDF-1.7")

    assert result.filename == "manual.pdf"
    assert result.parsed_text.startswith("Leave Policy")
    assert len(result.blocks) == 4


def test_parser_maps_timeout_and_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="mineru", timeout=1)

    monkeypatch.setattr("app.rag.mineru_parser.subprocess.run", raise_timeout)
    with pytest.raises(MinerUExecutionError, match="超时"):
        MinerUParser(temp_root=tmp_path).parse_pdf("manual.pdf", b"%PDF-1.7")

    monkeypatch.setattr(
        "app.rag.mineru_parser.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="bad input"),
    )
    with pytest.raises(MinerUExecutionError, match="bad input"):
        MinerUParser(temp_root=tmp_path).parse_pdf("manual.pdf", b"%PDF-1.7")


def test_parser_rejects_missing_content_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.rag.mineru_parser.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    with pytest.raises(MinerUExecutionError, match="content-list"):
        MinerUParser(temp_root=tmp_path).parse_pdf("manual.pdf", b"%PDF-1.7")
