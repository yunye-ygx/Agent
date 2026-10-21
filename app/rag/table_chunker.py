import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TableSlice:
    text: str
    row_start: int
    row_end: int
    header_repeated: bool


def split_table_text(text: str, encoding: Any, chunk_size: int) -> list[TableSlice]:
    """Split a row-oriented table while repeating its header rows.

    MinerU commonly exposes table_body as Markdown. Plain HTML tables with
    explicit ``tr`` rows are supported as a conservative fallback. Unknown
    formats stay whole rather than being cut through the middle of a cell.
    """
    rows = _markdown_rows(text)
    if rows is not None:
        header_rows, data_rows, render = rows
    else:
        html_rows = _html_rows(text)
        if html_rows is None:
            return [TableSlice(text=text, row_start=0, row_end=0, header_repeated=False)]
        header_rows, data_rows, render = html_rows

    if not data_rows:
        return [
            TableSlice(
                text=render(header_rows),
                row_start=0,
                row_end=len(header_rows),
                header_repeated=False,
            )
        ]

    chunks: list[TableSlice] = []
    current_rows = list(header_rows)
    current_data_start = 0

    for data_index, data_row in enumerate(data_rows):
        candidate_rows = [*current_rows, data_row]
        candidate_text = render(candidate_rows)
        if len(encoding.encode(candidate_text)) <= chunk_size or len(current_rows) == len(header_rows):
            current_rows.append(data_row)
            if len(encoding.encode(candidate_text)) > chunk_size:
                chunks.append(
                    TableSlice(
                        text=render(current_rows),
                        row_start=current_data_start,
                        row_end=data_index + 1,
                        header_repeated=True,
                    )
                )
                current_rows = list(header_rows)
                current_data_start = data_index + 1
            continue

        chunks.append(
            TableSlice(
                text=render(current_rows),
                row_start=current_data_start,
                row_end=data_index,
                header_repeated=True,
            )
        )
        current_rows = [*header_rows, data_row]
        current_data_start = data_index

    if len(current_rows) > len(header_rows):
        chunks.append(
            TableSlice(
                text=render(current_rows),
                row_start=current_data_start,
                row_end=len(data_rows),
                header_repeated=True,
            )
        )
    return chunks


def _markdown_rows(text: str):
    if "<tr" in text.lower() or "</table" in text.lower():
        return None
    rows = [line.strip() for line in text.splitlines() if line.strip()]
    if len(rows) < 2 or not all(_looks_like_pipe_row(row) for row in rows):
        return None

    header_count = 2 if _is_markdown_separator(rows[1]) else 1
    header_rows = rows[:header_count]
    data_rows = rows[header_count:]

    def render(selected_rows: list[str]) -> str:
        return "\n".join(selected_rows)

    return header_rows, data_rows, render


def _html_rows(text: str):
    matches = list(re.finditer(r"<tr\b[^>]*>.*?</tr\s*>", text, flags=re.IGNORECASE | re.DOTALL))
    if len(matches) < 2:
        return None
    opening = re.search(r"<table\b[^>]*>", text, flags=re.IGNORECASE)
    closing = re.search(r"</table\s*>", text, flags=re.IGNORECASE)
    if opening is None or closing is None:
        return None

    rows = [match.group(0).strip() for match in matches]
    header_rows = rows[:1]
    data_rows = rows[1:]
    table_open = opening.group(0)
    table_close = closing.group(0)

    def render(selected_rows: list[str]) -> str:
        return "\n".join([table_open, *selected_rows, table_close])

    return header_rows, data_rows, render


def _looks_like_pipe_row(row: str) -> bool:
    return row.count("|") >= 1


def _is_markdown_separator(row: str) -> bool:
    cells = row.strip().strip("|").split("|")
    return bool(cells) and all(
        re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in cells
    )
