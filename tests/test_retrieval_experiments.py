from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.retrieval.schemas import ExperimentRequest
from app.retrieval.service import ExperimentService


class MemorySnapshots:
    def __init__(self):
        self.values = {}

    async def put(self, owner, value):
        self.values[owner, value["report"]["experiment_id"]] = deepcopy(value)

    async def get(self, owner, identity):
        return deepcopy(self.values.get((owner, identity)))


def setup_service():
    search = AsyncMock()
    search.search.return_value = [
        {
            "chunk_id": "c",
            "kb_id": "kb",
            "doc_id": "d",
            "run_id": "r",
            "content": "printer error",
            "embedding": [1.0, 0.0],
            "initial_rank": 1,
            "initial_score": 3.0,
        }
    ]
    embed = AsyncMock(return_value=[[1.0, 0.0]])
    return ExperimentService(MemorySnapshots(), search, embed), search, embed


def scope():
    return {
        "kb_id": "kb",
        "doc_runs": {"d": "r"},
        "titles": {"d": "printer"},
        "embedding": {"model": "test", "dimension": 2},
        "warnings": [],
    }


@pytest.mark.asyncio
async def test_success_exposes_diagnostics_without_vectors():
    service, search, embed = setup_service()
    report = await service.run(
        "owner", ExperimentRequest(query="printer", knowledge_base_id="kb"), scope()
    )
    assert report["status"] == "SUCCESS" and report["mode"] == "vector"
    assert report["candidates"][0]["final_score"] == pytest.approx(1.0)
    assert "embedding" not in report["candidates"][0]
    assert report["result_count"] == 1
    embed.assert_awaited_once_with(["printer"])
    search.search.assert_awaited_once()


@pytest.mark.asyncio
async def test_failure_retained_and_idempotent_fallback_reuses_exact_candidates():
    service, search, embed = setup_service()
    rerank = AsyncMock()
    rerank.score.side_effect = TimeoutError("secret")
    req = ExperimentRequest(query="printer", knowledge_base_id="kb")
    report = await service.run("owner", req, scope(), rerank=rerank)
    assert report["status"] == "FAILED" and report["error"]["stage"] == "rerank"
    assert report["fallback_available"] and report["candidates"][0]["final_score"] is None
    assert "secret" not in str(report)
    child = await service.fallback("owner", report["experiment_id"])
    again = await service.fallback("owner", report["experiment_id"])
    assert child == again and child["mode"] == "vector"
    assert child["parent_experiment_id"] == report["experiment_id"]
    assert child["candidates"][0]["chunk_id"] == "c"
    assert (await service.store.get("owner", report["experiment_id"]))["report"][
        "status"
    ] == "FAILED"
    embed.assert_awaited_once()
    search.search.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_recall_and_bad_vector_never_offer_fallback():
    service, search, _ = setup_service()
    search.search.side_effect = ValueError("partial shards")
    report = await service.run(
        "owner", ExperimentRequest(query="q", knowledge_base_id="kb"), scope()
    )
    assert report["status"] == "FAILED" and not report["fallback_available"]
    assert report["error"]["stage"] == "recall"


@pytest.mark.asyncio
async def test_snapshot_failure_does_not_claim_recoverability():
    service, _, _ = setup_service()
    service.store.put = AsyncMock(side_effect=RuntimeError("private endpoint"))
    report = await service.run(
        "owner", ExperimentRequest(query="q", knowledge_base_id="kb"), scope()
    )
    assert report["status"] == "FAILED" and report["error"]["stage"] == "snapshot"
    assert not report["fallback_available"] and not report["persisted"]


@pytest.mark.asyncio
async def test_empty_recall_skips_reranker_and_is_successful_empty_result():
    service, search, _ = setup_service()
    search.search.return_value = []
    rerank = AsyncMock()
    report = await service.run(
        "owner", ExperimentRequest(query="q", knowledge_base_id="kb"), scope(), rerank
    )
    assert report["status"] == "SUCCESS" and report["result_count"] == 0
    rerank.score.assert_not_called()


@pytest.mark.asyncio
async def test_bad_candidate_vector_keeps_recalled_content_for_diagnosis():
    service, search, _ = setup_service()
    search.search.return_value[0]["embedding"] = [0.0, 0.0]
    report = await service.run(
        "owner", ExperimentRequest(query="q", knowledge_base_id="kb"), scope()
    )
    assert report["error"]["stage"] == "candidate_scoring"
    assert report["candidate_count"] == 1
    assert report["candidates"][0]["content"] == "printer error"
    assert report["candidates"][0]["final_score"] is None
    assert not report["fallback_available"]
