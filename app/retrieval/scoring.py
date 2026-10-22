"""RAGFlow-inspired query coverage + cosine; explicit field weighting.

No corpus/batch min-max normalization. The deterministic tokenizer is a local
baseline, not a claim to reproduce RAGFlow's dictionary-based tokenizer.
"""

import math
import re
import unicodedata

from app.retrieval.schemas import ExperimentRequest

TOKENIZER_VERSION = "cjk-char-alnum-v1"
SCORING_VERSION = "field-coverage-uniform-v1"
_TOKEN = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*|[\u3400-\u9fff]", re.IGNORECASE)


def tokenize(text: str) -> list[str]:
    return list(dict.fromkeys(_TOKEN.findall(unicodedata.normalize("NFKC", text).lower())))


def token_scores(query_tokens, title, content, title_weight=2, content_weight=1):
    query = set(query_tokens)
    title_tokens, content_tokens = tokenize(title), tokenize(content)
    title_matches = sorted(query & set(title_tokens))
    content_matches = sorted(query & set(content_tokens))
    title_score = len(title_matches) / len(query) if query else 0.0
    content_score = len(content_matches) / len(query) if query else 0.0
    return {
        "title_score": title_score,
        "content_score": content_score,
        "token_score": (title_weight * title_score + content_weight * content_score)
        / (title_weight + content_weight),
        "title_tokens": title_tokens,
        "content_tokens": content_tokens,
        "title_matches": title_matches,
        "content_matches": content_matches,
    }


def cosine(query, candidate) -> float:
    if not query or len(query) != len(candidate):
        raise ValueError("向量缺失或维度不兼容")
    if any(
        isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
        for x in [*query, *candidate]
    ):
        raise ValueError("向量包含无效数值")
    qnorm, cnorm = math.hypot(*query), math.hypot(*candidate)
    if not qnorm or not cnorm or not math.isfinite(qnorm + cnorm):
        raise ValueError("向量范数无效")
    value = math.fsum((q / qnorm) * (c / cnorm) for q, c in zip(query, candidate, strict=True))
    return max(-1.0, min(1.0, value))


def finish_scores(candidates, scores, request: ExperimentRequest, mode):
    if len(candidates) != len(scores):
        raise ValueError("候选与评分数量不一致")
    rows = []
    for candidate, score in zip(candidates, scores, strict=True):
        if not math.isfinite(score):
            raise ValueError("模型评分不是有限数值")
        row = {k: v for k, v in candidate.items() if k != "embedding"}
        row.update(
            cosine_score=score if mode == "vector" else None,
            rerank_score=score if mode == "rerank" else None,
            keyword_contribution=(1 - request.model_weight) * row["token_score"],
            model_contribution=request.model_weight * score,
        )
        row["final_score"] = row["keyword_contribution"] + row["model_contribution"]
        rows.append(row)
    rows.sort(key=lambda row: (-row["final_score"], row["initial_rank"], row["chunk_id"]))
    selected = 0
    for row in rows:
        row["final_rank"] = None
        if row["final_score"] < request.threshold:
            row["disposition"] = "BELOW_THRESHOLD"
        elif selected < request.top_n:
            selected += 1
            row.update(disposition="FINAL_SELECTED", final_rank=selected)
        else:
            row["disposition"] = "OUTSIDE_TOP_N"
    return rows
