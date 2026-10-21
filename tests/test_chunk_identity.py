from app.rag.fingerprint import build_chunk_id, build_parse_fingerprint


def test_parse_fingerprint_changes_when_chunk_or_embedding_config_changes() -> None:
    base = {
        "file_hash": "a" * 64,
        "parser": {"name": "mineru", "version": "3.4.5", "config": {"method": "auto"}},
        "chunker": {"version": "v1", "config": {"size": 500, "overlap": 80}},
        "embedding": {"model": "embedding-3", "version": "v1", "dimension": 2048},
        "page_range": {"start": 1, "end": 10, "context_start": 1, "context_end": 10},
    }

    same = build_parse_fingerprint(base)
    changed = build_parse_fingerprint({**base, "chunker": {**base["chunker"], "config": {"size": 600}}})

    assert same == build_parse_fingerprint(base)
    assert same != changed


def test_chunk_id_is_stable_for_same_document_and_content() -> None:
    values = {
        "document_id": "document-1",
        "parse_fingerprint": "fingerprint-1",
        "task_page_range": "1-10",
        "chunk_index": 0,
        "content": "hello",
    }

    assert build_chunk_id(values) == build_chunk_id(values)
    assert build_chunk_id(values) != build_chunk_id({**values, "content": "changed"})
