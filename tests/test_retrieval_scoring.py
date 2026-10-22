import math

import pytest
from pydantic import ValidationError

from app.retrieval.schemas import ExperimentRequest
from app.retrieval.scoring import cosine, finish_scores, token_scores, tokenize


def test_parameter_validation_and_original_query():
    request = ExperimentRequest(query="  NX100 不打印 E17  ", knowledge_base_id="kb")
    assert request.query == "NX100 不打印 E17"
    for values in (
        {"query": "  "},
        {"top_n": 51},
        {"top_k": 2, "top_n": 3},
        {"model_weight": float("nan")},
        {"title_weight": 0, "content_weight": 0},
    ):
        with pytest.raises(ValidationError):
            ExperimentRequest(**{"query": "test", "knowledge_base_id": "kb", **values})


def test_tokens_preserve_negation_identifiers_and_versions():
    tokens = tokenize("NX100 不打印 E17，版本 1.5.0，NX100")
    assert {"nx100", "不", "e17", "1.5.0"} <= set(tokens)
    assert tokens.count("nx100") == 1


def test_title_weight_is_real_and_empty_query_is_zero():
    q = tokenize("E17")
    title = token_scores(q, "E17", "unrelated", 2, 1)
    body = token_scores(q, "unrelated", "E17 E17 E17", 2, 1)
    assert title["token_score"] == pytest.approx(2 / 3)
    assert body["token_score"] == pytest.approx(1 / 3)
    assert token_scores([], "", "", 2, 1)["token_score"] == 0


def test_cosine_handles_geometry_and_rejects_damaged_vectors():
    assert cosine([1, 0], [1, 0]) == 1
    assert cosine([1, 0], [0, 1]) == 0
    assert cosine([1, 0], [-1, 0]) == -1
    for vector in ([], [0, 0], [1], [math.nan, 1], [math.inf, 1]):
        with pytest.raises(ValueError):
            cosine([1, 0], vector)


def test_filter_before_top_n_and_score_not_batch_normalized():
    request = ExperimentRequest(
        query="test", knowledge_base_id="kb", top_n=1, threshold=0.5, model_weight=1
    )
    candidates = [
        {"chunk_id": "b", "initial_rank": 2, "token_score": 0},
        {"chunk_id": "a", "initial_rank": 1, "token_score": 0},
    ]
    rows = finish_scores(candidates, [0.5, 0.5], request, "vector")
    assert [r["chunk_id"] for r in rows] == ["a", "b"]
    assert rows[0]["disposition"] == "FINAL_SELECTED"
    assert rows[1]["disposition"] == "OUTSIDE_TOP_N"
    assert rows[0]["final_score"] == 0.5
    more = finish_scores(
        candidates + [{"chunk_id": "c", "initial_rank": 3, "token_score": 0}],
        [0.5, 0.5, 0.1],
        request,
        "vector",
    )
    assert more[0]["final_score"] == 0.5
    assert more[2]["disposition"] == "BELOW_THRESHOLD"


def test_model_branch_scores_are_not_mislabeled():
    request = ExperimentRequest(query="q", knowledge_base_id="kb", model_weight=0.7)
    row = finish_scores(
        [{"chunk_id": "a", "initial_rank": 1, "token_score": 0.5}], [0.8], request, "rerank"
    )[0]
    assert row["cosine_score"] is None
    assert row["rerank_score"] == 0.8
    assert row["final_score"] == pytest.approx(0.71)
    assert row["keyword_contribution"] + row["model_contribution"] == row["final_score"]
