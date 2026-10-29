"""Generate the image-only PDF used by the RAG chunking workbench demo."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "docs" / "samples" / "assets"
PDF_PATH = ROOT / "docs" / "samples" / "image-pdf-chunking-demo.pdf"
CANVAS_SIZE = (1400, 990)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def _draw_pure_image() -> Image.Image:
    image = Image.new("RGB", CANVAS_SIZE, "#162238")
    draw = ImageDraw.Draw(image)
    for index in range(12):
        left = 60 + index * 100
        top = 80 + (index % 4) * 170
        right = left + 250
        bottom = top + 150
        color = (40 + index * 9, 110 + index * 5, 190 - index * 5)
        draw.rounded_rectangle((left, top, right, bottom), radius=26, fill=color)
        draw.ellipse((left + 70, top + 35, left + 180, top + 145), fill="#f7c873")
    draw.arc((230, 160, 1180, 820), 20, 320, fill="#f3f7ff", width=18)
    draw.line((250, 730, 1170, 230), fill="#ff8f70", width=15)
    return image


def _draw_text_diagram() -> Image.Image:
    image = Image.new("RGB", CANVAS_SIZE, "#f4f7fb")
    draw = ImageDraw.Draw(image)
    title_font = _font(52, bold=True)
    label_font = _font(34, bold=True)
    small_font = _font(26)
    draw.text((65, 45), "RAG document flow", fill="#182230", font=title_font)
    labels = ["PDF", "MinerU", "Chunker", "Embedding", "Vector DB"]
    colors = ["#d9e9ff", "#d7f4e7", "#fff0c8", "#fbe0e8", "#e5dcff"]
    left = 70
    top = 360
    width = 220
    height = 130
    for index, label in enumerate(labels):
        x = left + index * 260
        draw.rounded_rectangle((x, top, x + width, top + height), radius=24, fill=colors[index], outline="#617083", width=4)
        bbox = draw.textbbox((0, 0), label, font=label_font)
        draw.text((x + (width - bbox[2]) / 2, top + 42), label, fill="#182230", font=label_font)
        if index < len(labels) - 1:
            draw.line((x + width + 15, top + height / 2, x + 250, top + height / 2), fill="#2f6fed", width=8)
            draw.polygon([(x + 250, top + height / 2), (x + 225, top + height / 2 - 18), (x + 225, top + height / 2 + 18)], fill="#2f6fed")
    draw.text((75, 650), "Image text is intentionally rasterized for OCR experiments.", fill="#617083", font=small_font)
    return image


def _draw_table_image() -> Image.Image:
    image = Image.new("RGB", CANVAS_SIZE, "white")
    draw = ImageDraw.Draw(image)
    title_font = _font(46, bold=True)
    cell_font = _font(30)
    draw.text((70, 55), "Vector document fields", fill="#182230", font=title_font)
    headers = ["Field", "Type", "Meaning"]
    rows = [
        ["id", "bigint", "primary key"],
        ["content", "text", "chunk text"],
        ["embedding", "vector", "semantic vector"],
        ["page_no", "integer", "source page"],
    ]
    x0, y0 = 80, 210
    widths = [260, 260, 620]
    row_height = 125
    for row_index, row in enumerate([headers, *rows]):
        x = x0
        fill = "#dceaff" if row_index == 0 else ("#ffffff" if row_index % 2 else "#f3f7fb")
        for column_index, value in enumerate(row):
            width = widths[column_index]
            draw.rectangle((x, y0 + row_index * row_height, x + width, y0 + (row_index + 1) * row_height), fill=fill, outline="#617083", width=3)
            draw.text((x + 22, y0 + row_index * row_height + 40), value, fill="#182230", font=cell_font)
            x += width
    return image


def main() -> None:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    images = {
        "pure-architecture.png": _draw_pure_image(),
        "text-diagram.png": _draw_text_diagram(),
        "table-image.png": _draw_table_image(),
    }
    for filename, image in images.items():
        image.save(ASSET_DIR / filename, format="PNG", optimize=True)

    page_width, page_height = A4
    pdf = canvas.Canvas(str(PDF_PATH), pagesize=A4)
    captions = [
        "Case 1: pure image with no embedded text",
        "Case 2: raster image containing text",
        "Case 3: raster image containing a table",
    ]
    for page_index, filename in enumerate(images):
        image_path = ASSET_DIR / filename
        image = Image.open(image_path)
        image_ratio = image.width / image.height
        page_ratio = page_width / page_height
        if image_ratio > page_ratio:
            width = page_width - 48
            height = width / image_ratio
        else:
            height = page_height - 78
            width = height * image_ratio
        x = (page_width - width) / 2
        y = 42 + (page_height - 78 - height) / 2
        pdf.drawImage(ImageReader(str(image_path)), x, y, width=width, height=height, preserveAspectRatio=True, mask="auto")
        pdf.setFont("Helvetica", 9)
        pdf.drawString(24, 22, captions[page_index])
        pdf.showPage()
    pdf.save()
    print(PDF_PATH)


if __name__ == "__main__":
    main()
