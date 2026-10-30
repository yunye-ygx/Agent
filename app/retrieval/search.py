"""Single ES query+knn recall without changing existing index mappings."""

from app.retrieval.scoring import tokenize


class HybridSearcher:
    def __init__(self, client, index):
        self.client, self.index = client, index

    async def search(self, tokens, vector, *, kb_id, run_ids, document_ids, titles, top_k):
        filters = [
            {"term": {"kb_id": kb_id}},
            {"terms": {"run_id": run_ids}},
            {"terms": {"doc_id": document_ids}},
        ]
        should = []
        if tokens:
            should.append(
                {
                    "multi_match": {
                        "query": " ".join(tokens),
                        "fields": ["metadata.section_path^2", "content"],
                        "type": "most_fields",
                        "operator": "or",
                    }
                }
            )
            # Filenames already live in MySQL. Score those documents without
            # reindexing existing chunks or injecting titles into their vectors.
            for token in tokens:
                matched = [doc_id for doc_id, title in titles.items() if token in tokenize(title)]
                if matched:
                    should.append(
                        {
                            "constant_score": {
                                "filter": {"terms": {"doc_id": matched}},
                                "boost": 2 / len(tokens),
                            }
                        }
                    )
        lexical = {
            "bool": {
                "filter": filters,
                "should": should or [{"match_none": {}}],
                "minimum_should_match": 1,
                "boost": 0.05,
            }
        }
        response = await self.client.options(
            max_retries=0, retry_on_timeout=False, request_timeout=15
        ).search(
            index=self.index,
            query=lexical,
            knn={
                "field": "embedding",
                "query_vector": vector,
                "k": top_k,
                "num_candidates": max(50, 5 * top_k),
                "filter": filters,
                "boost": 0.95,
            },
            size=top_k,
            allow_partial_search_results=False,
            source=[
                "kb_id",
                "doc_id",
                "document_id",
                "run_id",
                "task_id",
                "content",
                "embedding",
                "metadata.section_path",
                "page_start",
                "page_end",
            ],
        )
        if response.get("timed_out") or response.get("_shards", {}).get("failed", 0):
            raise ValueError("ES 召回不完整，存在超时或失败分片")
        rows, seen = [], set()
        for hit in response["hits"]["hits"]:
            if hit["_id"] in seen:
                continue
            seen.add(hit["_id"])
            source = hit["_source"]
            if (
                source.get("kb_id") != kb_id
                or source.get("run_id") not in run_ids
                or source.get("doc_id") not in document_ids
            ):
                raise ValueError("ES 返回超出本次检索范围的候选")
            rows.append(
                {
                    **source,
                    "chunk_id": hit["_id"],
                    "initial_score": hit["_score"],
                    "initial_rank": len(rows) + 1,
                }
            )
        if len(rows) > top_k:
            raise ValueError("ES 返回候选数量超出限制")
        return rows
