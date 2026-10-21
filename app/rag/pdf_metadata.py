from __future__ import annotations

from io import BytesIO


def get_pdf_page_count(content: bytes) -> int | None:
    try:
        from pypdf import PdfReader

        return len(PdfReader(BytesIO(content)).pages)
    except Exception:
        return None
