import hashlib
import json
from typing import Any


def build_parse_fingerprint(config: dict[str, Any]) -> str:
    canonical = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_chunk_id(values: dict[str, Any]) -> str:
    content_hash = hashlib.sha256(str(values["content"]).encode("utf-8")).hexdigest()
    canonical = "|".join(
        [
            str(values["document_id"]),
            str(values["parse_fingerprint"]),
            str(values["task_page_range"]),
            str(values["chunk_index"]),
            content_hash,
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_task_chunk_id(task_id: str, chunk_key: str) -> str:
    return hashlib.sha256(f"{task_id}\x00{chunk_key}".encode()).hexdigest()
