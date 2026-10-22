import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.retrieval.snapshots import SnapshotStore

OWNER = "a" * 64
ID = "11111111-1111-1111-1111-111111111111"


class Response(io.BytesIO):
    def release_conn(self):
        pass


@pytest.mark.asyncio
async def test_expiration_is_enforced_even_without_background_cleanup():
    storage = SimpleNamespace(client=Mock())
    storage.client.get_object.side_effect = lambda *args: Response(
        json.dumps({"report": {"expires_at": 100}}).encode()
    )
    store = SnapshotStore(storage, clock=lambda: 99)
    assert await store.get(OWNER, ID)
    store.clock = lambda: 100
    assert await store.get(OWNER, ID) is None
    assert store.key(OWNER, ID) != store.key("b" * 64, ID)
    with pytest.raises(ValueError):
        store.key("../", ID)


@pytest.mark.asyncio
async def test_snapshot_capacity_failure_never_uploads_or_touches_offline_objects():
    storage = SimpleNamespace(client=Mock(), put_bytes=AsyncMock())
    storage.client.list_objects.return_value = [
        SimpleNamespace(
            object_name="a2-experiments/existing",
            size=256 * 1024 * 1024,
            last_modified=SimpleNamespace(timestamp=lambda: 999),
        )
    ]
    store = SnapshotStore(storage, clock=lambda: 1000)
    with pytest.raises(ValueError, match="容量"):
        await store.put(OWNER, {"report": {"experiment_id": ID}})
    storage.put_bytes.assert_not_called()
    assert storage.client.list_objects.call_args.kwargs["prefix"] == "a2-experiments/"
    storage.client.remove_object.assert_not_called()


@pytest.mark.asyncio
async def test_knowledge_base_purge_removes_only_matching_experiment_snapshots():
    storage = SimpleNamespace(client=Mock())
    target = "a2-experiments/owner/target.json"
    other = "a2-experiments/owner/other.json"
    storage.client.list_objects.return_value = [
        SimpleNamespace(object_name=target),
        SimpleNamespace(object_name=other),
    ]
    storage.client.get_object.side_effect = lambda _bucket, key: Response(
        json.dumps(
            {"report": {"scope": {"kb_id": "kb-target" if key == target else "kb-other"}}}
        ).encode()
    )

    await SnapshotStore(storage).delete_knowledge_base_snapshots("kb-target")

    storage.client.remove_object.assert_called_once_with("rag-demo-artifacts", target)
