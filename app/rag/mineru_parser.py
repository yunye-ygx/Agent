import json
import math
import os
import shutil
import site
import subprocess
import sys
import sysconfig
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.rag.chunking_schemas import ParsedPdf, PdfBlock


class MinerUError(RuntimeError):
    """Base error for local MinerU integration failures."""


class MinerUExecutionError(MinerUError):
    """MinerU could not be started or did not produce a valid result."""


class MinerUUnavailableError(MinerUExecutionError):
    """The configured MinerU executable is not available."""


def normalize_content_list(payload: list[dict[str, Any]] | dict[str, Any]) -> list[PdfBlock]:
    blocks, _ = normalize_content_list_with_trace(payload)
    return blocks


def normalize_content_list_with_trace(
    payload: list[dict[str, Any]] | dict[str, Any],
) -> tuple[list[PdfBlock], list[dict[str, Any]]]:
    if isinstance(payload, dict):
        payload = payload.get("content_list", payload.get("blocks", []))
    if not isinstance(payload, list):
        raise MinerUExecutionError("MinerU 输出不是有效的 content-list 数组")

    blocks: list[PdfBlock] = []
    raw_items: list[tuple[int, dict[str, Any]]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            continue
        raw_items.append((index, dict(item)))
        raw_type = str(item.get("type") or item.get("block_type") or "unknown").lower()
        block_type = {
            "text": "text",
            "title": "title",
            "table": "table",
            "image": "image",
            "figure": "image",
            "equation": "equation",
            "list": "list",
            "header": "header",
            "footer": "footer",
        }.get(raw_type, "unknown")
        page_number = item.get("page_number")
        if page_number is None and item.get("page_idx") is not None:
            page_number = int(item["page_idx"]) + 1
        text = _extract_text(item, block_type)
        metadata = dict(item)
        if block_type == "image":
            metadata = _normalize_image_metadata(metadata, text)
        elif block_type == "table":
            metadata = _normalize_table_metadata(metadata, text)
        block = PdfBlock(
            block_id=f"block-{index:04d}",
            block_index=index,
            block_type=block_type,
            page_number=page_number,
            text=text,
            metadata=metadata,
        )
        blocks.append(block)
    if not blocks:
        raise MinerUExecutionError("MinerU 未生成可识别的 blocks")
    blocks = _annotate_image_relations(blocks)
    block_by_index = {block.block_index: block for block in blocks}
    trace = []
    for index, item in raw_items:
        block = block_by_index[index]
        trace.append(
            {
                "raw_index": index,
                "raw_item": item,
                "extracted": {
                    "block_id": block.block_id,
                    "block_index": block.block_index,
                    "block_type": block.block_type,
                    "page_number": block.page_number,
                    "text": block.text,
                    "metadata": block.metadata,
                },
                "field_lineage": {
                    "block_id": "后端生成",
                    "block_index": "后端根据数组顺序生成",
                    "block_type": "type/block_type 映射",
                    "page_number": "page_number 或 page_idx + 1",
                    "text": "text/md/content，table 时回退 table_body/html/table",
                    "metadata": "原始 MinerU item 的复制",
                    "image_relations": "后端根据同页 bbox 推断的候选关系，不是 MinerU 原始 OCR 结论",
                },
            }
        )
    return blocks, trace


class MinerUParser:
    def __init__(
        self,
        *,
        command: str = "mineru",
        timeout_seconds: float = 120.0,
        temp_root: str | Path | None = None,
        backend: str = "pipeline",
        method: str = "auto",
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.temp_root = Path(temp_root) if temp_root else None
        self.backend = backend
        self.method = method

    def parse_pdf(self, filename: str, content: bytes) -> ParsedPdf:
        with tempfile.TemporaryDirectory(dir=self.temp_root) as temp_dir:
            work_dir = Path(temp_dir)
            input_path = work_dir / "source.pdf"
            output_dir = work_dir / "output"
            output_dir.mkdir()
            input_path.write_bytes(content)
            resolved_command = _resolve_command(self.command)
            if resolved_command is None:
                raise MinerUUnavailableError("MinerU 命令不可用，请确认已安装并配置 PATH")
            command, environment = _build_command(
                resolved_command,
                input_path=input_path,
                output_dir=output_dir,
                backend=self.backend,
                method=self.method,
            )
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    env=environment,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise MinerUUnavailableError("MinerU 命令不可用，请确认已安装并配置 PATH") from exc
            except subprocess.TimeoutExpired as exc:
                raise MinerUExecutionError("MinerU 解析超时") from exc
            except OSError as exc:
                raise MinerUExecutionError(f"无法启动 MinerU：{exc}") from exc

            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "未知错误").strip()
                raise MinerUExecutionError(f"MinerU 执行失败：{detail[:500]}")
            result_path = _find_content_list(output_dir)
            if result_path is None:
                raise MinerUExecutionError("MinerU 未生成 content-list JSON")
            try:
                payload = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise MinerUExecutionError("MinerU content-list JSON 无法解析") from exc
            blocks, processing_trace = normalize_content_list_with_trace(payload)
            parsed_text = "\n".join(block.text for block in blocks if block.text.strip())
            return ParsedPdf(
                filename=filename,
                blocks=blocks,
                parsed_text=parsed_text,
                raw_outputs=_read_raw_outputs(output_dir),
                processing_trace=processing_trace,
            )


def _extract_text(item: dict[str, Any], block_type: str) -> str:
    keys = ("text", "md", "content")
    if block_type == "image":
        keys = (*keys, "ocr_text", "image_text", "ocr")
    for key in keys:
        value = item.get(key)
        if isinstance(value, str):
            return value
    if block_type == "table":
        for key in ("table_body", "html", "table"):
            value = item.get(key)
            if isinstance(value, str):
                return value
    return ""


def _normalize_image_metadata(metadata: dict[str, Any], text: str) -> dict[str, Any]:
    """Keep image provenance while exposing a small, stable demo-facing view."""

    metadata["img_caption_text"] = _join_field_values(
        metadata.get("image_caption", metadata.get("img_caption", metadata.get("caption")))
    )
    metadata["image_footnote_text"] = _join_field_values(
        metadata.get("image_footnote", metadata.get("img_footnote"))
    )
    metadata["image_text_status"] = "embedded_text" if text.strip() else "awaiting_relation_analysis"

    explicit_kind = metadata.get("image_kind")
    if isinstance(explicit_kind, str) and explicit_kind.strip():
        metadata["image_kind"] = explicit_kind.strip()
        return metadata

    lowered = text.lower()
    if "| ---" in lowered or ("<table" in lowered and "</table>" in lowered):
        metadata["image_kind"] = "table_image"
    elif text.strip():
        metadata["image_kind"] = "text_image"
    else:
        metadata["image_kind"] = "pure_image"
    metadata["related_text_block_indexes"] = []
    return metadata


def _normalize_table_metadata(metadata: dict[str, Any], text: str) -> dict[str, Any]:
    if metadata.get("img_path"):
        metadata["source_image_kind"] = "table_image"
        metadata["source_image_path"] = metadata["img_path"]
    metadata["table_caption_text"] = _join_field_values(
        metadata.get("table_caption", metadata.get("caption"))
    )
    metadata["table_footnote_text"] = _join_field_values(metadata.get("table_footnote"))
    metadata["table_representation"] = "structured_table_body" if text.strip() else "image_only"
    return metadata


def _join_field_values(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(item).strip() for item in value if str(item).strip())
    if value is None:
        return ""
    return str(value).strip()


def _annotate_image_relations(blocks: list[PdfBlock]) -> list[PdfBlock]:
    """Add conservative image/text candidates without claiming they are OCR."""

    metadata_by_index = {block.block_index: dict(block.metadata) for block in blocks}
    image_blocks = [block for block in blocks if block.block_type == "image"]
    text_candidates = [
        block
        for block in blocks
        if block.block_type in {"text", "title", "unknown", "header", "footer"}
        and block.text.strip()
    ]

    for image in image_blocks:
        image_metadata = metadata_by_index[image.block_index]
        image_box = _read_bbox(image.metadata.get("bbox"))
        related: list[int] = []
        nearby: list[int] = []
        if image_box is not None:
            for candidate in text_candidates:
                if candidate.page_number != image.page_number:
                    continue
                candidate_box = _read_bbox(candidate.metadata.get("bbox"))
                if candidate_box is None:
                    continue
                candidate_metadata = metadata_by_index[candidate.block_index]
                if _bbox_contains(image_box, candidate_box):
                    related.append(candidate.block_index)
                    candidate_metadata["image_relation"] = "inside_image_bbox_candidate"
                    candidate_metadata.setdefault("related_image_block_indexes", []).append(
                        image.block_index
                    )
                elif _bbox_gap_distance(image_box, candidate_box) <= 180:
                    nearby.append(candidate.block_index)
                    candidate_metadata.setdefault("image_relation", "nearby_image_bbox_candidate")
                    candidate_metadata.setdefault("related_image_block_indexes", []).append(
                        image.block_index
                    )

        image_metadata["related_text_block_indexes"] = related
        image_metadata["nearby_text_block_indexes"] = nearby
        if image_metadata.get("image_text_status") != "embedded_text":
            if related:
                image_metadata["image_text_status"] = "related_text_candidates"
            elif nearby:
                image_metadata["image_text_status"] = "nearby_text_candidates"
            else:
                image_metadata["image_text_status"] = "no_text_candidate"
        if related:
            image_metadata["relation_reason"] = "same_page_text_bbox_inside_image_bbox"
        elif nearby:
            image_metadata["relation_reason"] = "same_page_text_bbox_near_image_bbox"

    return [replace(block, metadata=metadata_by_index[block.block_index]) for block in blocks]


def _read_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        left, top, right, bottom = (float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _bbox_contains(outer: tuple[float, float, float, float], inner: tuple[float, float, float, float]) -> bool:
    outer_left, outer_top, outer_right, outer_bottom = outer
    inner_left, inner_top, inner_right, inner_bottom = inner
    center_x = (inner_left + inner_right) / 2
    center_y = (inner_top + inner_bottom) / 2
    return (
        outer_left <= center_x <= outer_right
        and outer_top <= center_y <= outer_bottom
    )


def _bbox_gap_distance(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    first_left, first_top, first_right, first_bottom = first
    second_left, second_top, second_right, second_bottom = second
    horizontal_gap = max(first_left - second_right, second_left - first_right, 0.0)
    vertical_gap = max(first_top - second_bottom, second_top - first_bottom, 0.0)
    return math.hypot(horizontal_gap, vertical_gap)


def _find_content_list(output_dir: Path) -> Path | None:
    matches = sorted(output_dir.rglob("*content_list*.json"))
    return matches[0] if matches else None


def _read_raw_outputs(output_dir: Path) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for path in sorted(output_dir.rglob("*.json")):
        name = path.name.lower()
        if "content_list" not in name and "middle" not in name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        kind = "content_list_v2" if "content_list_v2" in name else "content_list"
        if "middle" in name:
            kind = "middle"
        outputs.append({"filename": path.name, "kind": kind, "data": data})
    return outputs


def _resolve_command(command: str) -> str | None:
    found = shutil.which(command)
    if found:
        return found
    command_path = Path(command)
    if command_path.is_file():
        return str(command_path)
    scripts_dirs = [Path(sysconfig.get_path("scripts"))]
    if hasattr(site, "getuserbase"):
        userbase = Path(site.getuserbase())
        scripts_dirs.extend(
            [
                userbase / "Scripts",
                userbase / f"Python{sys.version_info.major}{sys.version_info.minor}" / "Scripts",
            ]
        )
    for scripts_dir in scripts_dirs:
        for candidate in (scripts_dir / command, scripts_dir / f"{command}.exe"):
            if candidate.is_file():
                return str(candidate)
    return None


def _build_command(
    resolved_command: str,
    *,
    input_path: Path,
    output_dir: Path,
    backend: str,
    method: str,
) -> tuple[list[str], dict[str, str] | None]:
    arguments = [
        "-p",
        str(input_path),
        "-o",
        str(output_dir),
        "-b",
        backend,
        "-m",
        method,
    ]
    if os.name != "nt" or Path(resolved_command).stem.lower() != "mineru":
        return [resolved_command, *arguments], None

    model_path, site_dir = _prepare_ascii_fasttext_model()
    wrapper = (
        "import sys\n"
        "from pathlib import Path\n"
        "import fast_langdetect.ft_detect.infer as infer\n"
        f"infer.LOCAL_SMALL_MODEL_PATH = Path({str(model_path)!r})\n"
        "from importlib.metadata import entry_points\n"
        "entry = next(item for item in entry_points(group='console_scripts') if item.name == 'mineru')\n"
        "entry.load()()\n"
    )
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(site_dir)
        if not existing_pythonpath
        else os.pathsep.join([str(site_dir), existing_pythonpath])
    )
    return [sys.executable, "-c", wrapper, *arguments], environment


def _prepare_ascii_fasttext_model() -> tuple[Path, Path]:
    target = Path("C:/rag_lab_fasttext/lid.176.bin")
    site_dir = target.parent
    sitecustomize = site_dir / "sitecustomize.py"
    if target.is_file() and sitecustomize.is_file():
        return target, site_dir
    try:
        from fast_langdetect.ft_detect.infer import LOCAL_SMALL_MODEL_PATH
    except ImportError as exc:
        raise MinerUUnavailableError("MinerU 的语言检测依赖不可用") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(LOCAL_SMALL_MODEL_PATH, target)
    sitecustomize.write_text(
        "from pathlib import Path\n"
        "import fast_langdetect.ft_detect.infer as infer\n"
        f"infer.LOCAL_SMALL_MODEL_PATH = Path({str(target)!r})\n",
        encoding="utf-8",
    )
    return target, site_dir
