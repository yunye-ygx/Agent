import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def module():
    spec = importlib.util.spec_from_file_location(
        "migrate_a1", Path(__file__).parents[1] / "tools" / "migrate_a1_artifacts.py"
    )
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_legacy_shared_chunk_converts_to_independent_target_record():
    migrate = module()
    old = {
        "document_id": "doc",
        "generation_ids": ["run"],
        "content": "text",
        "embedding": [1.0, 2.0],
        "metadata": {},
    }
    task = SimpleNamespace(id="task")
    run = SimpleNamespace(id="run", config_snapshot={"embedding": {"dimension": 2}})
    doc = SimpleNamespace(id="doc", knowledge_base_id="kb")
    key, source = migrate.convert_legacy_chunk(old, task=task, run=run, doc=doc, chunk_index=0)
    assert key and source["task_id"] == "task" and source["run_id"] == "run"
    assert source["kb_id"] == "kb" and source["embedding"] == [1.0, 2.0]
    assert "generation_ids" not in source


def test_legacy_conversion_rejects_missing_vector_and_wrong_ownership():
    migrate = module()
    run = SimpleNamespace(id="run", config_snapshot={"embedding": {"dimension": 2}})
    doc, task = SimpleNamespace(id="doc", knowledge_base_id="kb"), SimpleNamespace(id="task")
    with pytest.raises(ValueError):
        migrate.convert_legacy_chunk(
            {"content": "text", "embedding": [1.0]}, task=task, run=run, doc=doc, chunk_index=0
        )


def test_migration_default_is_local_plan_without_connections(capsys):
    migrate = module()
    migrate.main([])
    assert "--apply" in capsys.readouterr().out
