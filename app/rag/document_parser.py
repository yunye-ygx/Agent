"""Bounded TXT/MD input and PDF slicing; page ranges are 0-based [start,end)."""

import asyncio
from dataclasses import replace
from io import BytesIO

import httpx
from pypdf import PdfReader, PdfWriter

from app.core.config import settings
from app.rag.chunking_schemas import ParsedPdf, PdfBlock
from app.rag.mineru_parser import MinerUExecutionError, normalize_content_list


def file_type_for(filename):
    extension = filename.rsplit(".", 1)[-1].lower()
    if extension not in {"pdf", "txt", "md"}:
        raise ValueError("仅支持 PDF、TXT、MD 文件")
    return extension


def slice_pdf(content, start, end):
    reader = PdfReader(BytesIO(content))
    if not 0 <= start < end <= len(reader.pages):
        raise ValueError("PDF 页范围无效")
    writer = PdfWriter()
    for page in reader.pages[start:end]:
        writer.add_page(page)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


async def parse_task_document(*, filename, content, file_type, range_start, range_end, parser=None):
    if len(content) > settings.chunking_max_file_bytes:
        raise ValueError("文件超过单任务资源上限")
    if file_type in {"txt", "md"}:
        text = content.decode("utf-8-sig")
        blocks = [
            PdfBlock(f"text-{i}", i, "text", None, paragraph, {})
            for i, paragraph in enumerate(text.split("\n\n"))
            if paragraph.strip()
        ]
        return ParsedPdf(filename, blocks, text)
    if file_type != "pdf" or parser is None:
        raise ValueError("缺少 PDF 解析适配器")
    data = await asyncio.to_thread(slice_pdf, content, range_start, range_end)
    if hasattr(parser, "parse_pdf_async"):
        parsed = await parser.parse_pdf_async(filename, data)
    else:
        parsed = await asyncio.to_thread(parser.parse_pdf, filename, data)
    blocks = []
    for block in parsed.blocks:
        if block.page_number is None or not 1 <= block.page_number <= range_end - range_start:
            raise MinerUExecutionError("MinerU 页码缺失或超出子 PDF 范围，不能可靠映射来源")
        page = block.page_number + range_start
        blocks.append(
            replace(
                block,
                page_number=page,
                metadata={
                    **block.metadata,
                    "original_page_number": page,
                    "task_page_offset": range_start,
                },
            )
        )
    return replace(parsed, blocks=blocks)


class RemoteMinerUParser:
    """Explicit file_parse-v2 adapter; verify the deployed service version before use.

    POST multipart files + parse_method/backend/return_content_list. Expected
    JSON: {results: {<filename>: {content_list: <array or JSON string>}}}.
    No remote cancellation API is assumed. PDF slicing happens locally.
    """

    def __init__(self, *, base_url, timeout_seconds, backend, method, transport=None):
        self.base_url, self.timeout = base_url, timeout_seconds
        self.backend, self.method, self.transport = backend, method, transport

    async def parse_pdf_async(self, filename, content):
        import json

        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.post(
                    self.base_url.rstrip("/") + "/file_parse",
                    files={"files": (filename, content, "application/pdf")},
                    data={
                        "backend": self.backend,
                        "parse_method": self.method,
                        "return_content_list": "true",
                        "return_md": "false",
                        "return_images": "false",
                        "return_middle_json": "false",
                    },
                )
                response.raise_for_status()
                results = response.json()["results"]
                if len(results) != 1:
                    raise ValueError("期望一个子 PDF 的解析结果")
                items = next(iter(results.values()))["content_list"]
                if isinstance(items, str):
                    items = json.loads(items)
                blocks = normalize_content_list(items)
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise MinerUExecutionError(f"远端 MinerU 响应不符合 file_parse-v2 契约：{exc}") from exc
        return ParsedPdf(filename, blocks, "\n".join(b.text for b in blocks))
