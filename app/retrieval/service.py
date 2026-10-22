"""Experiment orchestration. Snapshots are internal; reports contain no vectors/keys."""

import asyncio
import logging
import time
import uuid
from copy import deepcopy

from app.retrieval.rerank import RerankError
from app.retrieval.schemas import ExperimentRequest
from app.retrieval.scoring import (
    SCORING_VERSION,
    TOKENIZER_VERSION,
    cosine,
    finish_scores,
    token_scores,
    tokenize,
)

TTL_SECONDS = 1800
logger = logging.getLogger(__name__)
_FALLBACK_LOCKS = [asyncio.Lock() for _ in range(32)]


class ExperimentService:
    def __init__(self, store, searcher=None, embed=None):
        self.store, self.searcher, self.embed = store, searcher, embed

    async def run(self, owner, request, scope, rerank=None, rerank_identity=None):
        now = time.time()
        mode = "rerank" if rerank is not None else "vector"
        report = {
            "experiment_id": str(uuid.uuid4()),
            "parent_experiment_id": None,
            "created_at": now,
            "expires_at": now + TTL_SECONDS,
            "status": "RUNNING",
            "mode": mode,
            "request": request.model_dump(),
            "scope": scope,
            "query_tokens": [],
            "tokenizer_version": TOKENIZER_VERSION,
            "scoring_version": SCORING_VERSION,
            "recall_profile": {
                "keyword_boost": 0.05,
                "vector_boost": 0.95,
                "num_candidates": max(50, request.top_k * 5),
            },
            "rerank": rerank_identity,
            "candidates": [],
            "result_count": 0,
            "candidate_count": 0,
            "timings_ms": {},
            "error": None,
            "warnings": list(scope.get("warnings", [])),
            "fallback_available": False,
            "persisted": False,
        }
        snapshot = {"report": report, "candidates": [], "query_vector": None}
        stage, started = "preprocess", time.perf_counter()

        def advance(next_stage):
            nonlocal stage, started
            report["timings_ms"][stage] = round((time.perf_counter() - started) * 1000, 2)
            stage, started = next_stage, time.perf_counter()

        try:
            tokens = tokenize(request.query)
            if len(tokens) > 512:
                raise ValueError("查询词数量超过 512，请缩短问题")
            report["query_tokens"] = tokens
            advance("embedding")
            vectors = await self.embed([request.query])
            if len(vectors) != 1 or len(vectors[0]) != scope["embedding"]["dimension"]:
                raise ValueError("查询向量维度错误")
            vector = vectors[0]
            cosine(vector, vector)
            snapshot["query_vector"] = vector
            advance("recall")
            raw = await self.searcher.search(
                tokens,
                vector,
                kb_id=scope["kb_id"],
                run_ids=list(scope["doc_runs"].values()),
                document_ids=list(scope["doc_runs"]),
                titles=scope["titles"],
                top_k=request.top_k,
            )
            advance("candidate_scoring")
            rows, similarities = snapshot["candidates"], []
            for item in raw:
                if scope["doc_runs"].get(item["doc_id"]) != item["run_id"]:
                    raise ValueError("候选文档与版本不匹配")
                section = item.get("metadata", {}).get("section_path", "")
                title = " / ".join(section) if isinstance(section, list) else str(section or "")
                title = title or scope["titles"][item["doc_id"]]
                row = {
                    key: item.get(key)
                    for key in (
                        "chunk_id",
                        "doc_id",
                        "run_id",
                        "task_id",
                        "content",
                        "embedding",
                        "initial_rank",
                        "initial_score",
                        "page_start",
                        "page_end",
                    )
                }
                row["title"] = title
                rows.append(row)
            for row in rows:
                row.update(
                    **token_scores(
                        tokens,
                        row["title"],
                        row["content"],
                        request.title_weight,
                        request.content_weight,
                    ),
                )
                similarities.append(cosine(vector, row["embedding"]))
            report["candidate_count"] = len(rows)
            advance("rerank" if mode == "rerank" else "vector_scoring")
            scores = similarities
            if mode == "rerank" and rows:
                # Bound total latency even for a multi-batch provider adapter.
                async with asyncio.timeout(60):
                    scores, details = await rerank.score(request.query, rows)
                for row, detail in zip(rows, details, strict=True):
                    row["rerank_input"] = detail
            advance("fusion")
            report["candidates"] = finish_scores(rows, scores, request, mode)
            report["status"] = "SUCCESS"
            report["result_count"] = sum(
                r["disposition"] == "FINAL_SELECTED" for r in report["candidates"]
            )
        except Exception as exc:
            # Exception text from external clients can include credentials or content.
            logger.error(
                "Retrieval experiment %s failed at %s (%s)",
                report["experiment_id"],
                stage,
                type(exc).__name__,
            )
            message = (
                str(exc)
                if isinstance(exc, RerankError)
                else {
                    "preprocess": "查询词过多或预处理失败，请缩短问题",
                    "embedding": "查询向量生成失败或维度不匹配",
                    "recall": "混合召回失败或 ES 返回不完整结果",
                    "candidate_scoring": "候选字段、向量或版本不符合要求",
                    "rerank": "Rerank 失败或超时，未自动降级",
                }.get(stage, "候选评分失败")
            )
            report.update(
                status="FAILED",
                error={"stage": stage, "code": stage.upper() + "_FAILED", "message": message},
            )
            report["candidates"] = [
                dict(
                    {k: v for k, v in row.items() if k != "embedding"},
                    final_score=None,
                    cosine_score=None,
                    rerank_score=None,
                    disposition="NOT_SCORED",
                    final_rank=None,
                )
                for row in snapshot["candidates"]
            ]
            report["candidate_count"] = len(snapshot["candidates"])
            report["fallback_available"] = stage == "rerank" and bool(snapshot["candidates"])
        advance("snapshot")
        try:
            report["persisted"] = True
            await self.store.put(owner, snapshot)
        except Exception:
            report["persisted"] = False
            report["fallback_available"] = False
            report["prior_error"] = report["error"]
            report.update(
                status="FAILED",
                error={
                    "stage": "snapshot",
                    "code": "SNAPSHOT_UNAVAILABLE",
                    "message": "实验快照保存失败，当前结果仅可在页面查看或导出，无法手动降级",
                },
            )
        return report

    async def fallback(self, owner, identity):
        # Same parent always yields the same child, including concurrent/repeated clicks.
        child_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "a2-fallback:" + identity))
        async with _FALLBACK_LOCKS[int(uuid.UUID(child_id)) % len(_FALLBACK_LOCKS)]:
            snapshot = await self.store.get(owner, identity)
            if snapshot is None or not snapshot["report"]["fallback_available"]:
                raise ValueError("快照不可降级或已经过期")
            existing = await self.store.get(owner, child_id)
            if existing:
                return existing["report"]
            child = deepcopy(snapshot)
            report = child["report"]
            request = ExperimentRequest(**report["request"])
            started = time.perf_counter()
            scores = [cosine(child["query_vector"], r["embedding"]) for r in child["candidates"]]
            report.update(
                experiment_id=child_id,
                parent_experiment_id=identity,
                mode="vector",
                status="SUCCESS",
                error=None,
                fallback_available=False,
                persisted=True,
                created_at=time.time(),
                candidates=finish_scores(child["candidates"], scores, request, "vector"),
            )
            report["result_count"] = sum(
                r["disposition"] == "FINAL_SELECTED" for r in report["candidates"]
            )
            report["timings_ms"] = {
                "fallback_scoring": round((time.perf_counter() - started) * 1000, 2)
            }
            report["warnings"].append("手动降级：复用原始候选与向量；原失败实验保持不变")
            await self.store.put(owner, child)
            return report
