from io import BytesIO

from pypdf import PdfWriter

from app.rag.pdf_metadata import get_pdf_page_count


def test_pdf_page_count_reads_generated_document() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    output = BytesIO()
    writer.write(output)

    assert get_pdf_page_count(output.getvalue()) == 2


def test_non_pdf_returns_none() -> None:
    assert get_pdf_page_count(b"not a pdf") is None
