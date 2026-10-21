from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class PdfBlock:
    block_id: str
    block_index: int
    block_type: str
    page_number: int | None
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ParsedPdf:
    filename: str
    blocks: list[PdfBlock]
    parsed_text: str
    warnings: list[str] = field(default_factory=list)
    raw_outputs: list[dict[str, Any]] = field(default_factory=list)
    processing_trace: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TokenChunk:
    chunk_index: int
    text: str
    token_count: int
    start_token: int
    end_token: int
    start_char: int
    end_char: int
    block_types: list[str]
    block_indexes: list[int]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
