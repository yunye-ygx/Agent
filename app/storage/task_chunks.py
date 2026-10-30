"""ES operations for independent task/run artifacts (no shared memberships)."""

from typing import Any


class TaskChunkOperations:
    async def write_chunks(self, payloads, *, check_execution=None):
        batch_size = max(1, self.config.es_batch_size)
        for start in range(0, len(payloads), batch_size):
            if check_execution:
                await check_execution()
            batch = payloads[start : start + batch_size]
            operations = []
            for chunk_id, source in batch:
                operations.extend([{"index": {"_index": self.index, "_id": chunk_id}}, source])
            response = await self.client.bulk(operations=operations, refresh="wait_for")
            items = response.get("items", [])
            failures = [
                item
                for item in items
                if not 200 <= item.get("index", {}).get("status", 0) < 300
                or item.get("index", {}).get("error")
            ]
            if response.get("errors") or failures or len(items) != len(batch):
                raise RuntimeError(f"ES 批量写入未完整成功：{failures[:3]}")

    async def read_task_chunks(self, *, kb_id, doc_id, run_id, task_id):
        filters = [
            {"term": {key: value}}
            for key, value in (
                ("kb_id", kb_id),
                ("doc_id", doc_id),
                ("run_id", run_id),
                ("task_id", task_id),
            )
        ]
        return await self._scroll_chunks(filters)

    async def read_run_chunks(self, *, kb_id, doc_id, run_id):
        return await self._scroll_chunks(
            [
                {"term": {key: value}}
                for key, value in (("kb_id", kb_id), ("doc_id", doc_id), ("run_id", run_id))
            ]
        )

    async def _scroll_chunks(self, filters):
        rows = []
        scroll_id = None
        try:
            response = await self.client.search(
                index=self.index,
                query={"bool": {"filter": filters}},
                size=max(1, self.config.es_batch_size),
                sort=["_doc"],
                scroll="1m",
                source=True,
                allow_partial_search_results=False,
            )
            while True:
                scroll_id = response.get("_scroll_id", scroll_id)
                self._check_shards(response)
                hits = response["hits"]["hits"]
                if not hits:
                    break
                rows.extend((hit["_id"], hit["_source"]) for hit in hits)
                if not scroll_id:
                    raise RuntimeError("ES 全量读取缺少 scroll_id")
                response = await self.client.scroll(scroll_id=scroll_id, scroll="1m")
        finally:
            if scroll_id:
                await self.client.clear_scroll(scroll_id=scroll_id)
        return rows

    @staticmethod
    def _check_shards(response):
        if response.get("timed_out") or response.get("_shards", {}).get("failed", 0):
            raise RuntimeError("ES 读取超时或存在失败分片，不能视为完整产物")

    async def refresh(self):
        response = await self.client.indices.refresh(index=self.index)
        self._check_shards(response)

    async def delete_run_chunks(self, *, kb_id, doc_id, run_id):
        response = await self.client.delete_by_query(
            index=self.index,
            query={
                "bool": {
                    "filter": [
                        {"term": {key: value}}
                        for key, value in (("kb_id", kb_id), ("doc_id", doc_id), ("run_id", run_id))
                    ]
                }
            },
            refresh=True,
            conflicts="proceed",
        )
        if (
            response.get("failures")
            or response.get("timed_out")
            or response.get("version_conflicts")
        ):
            raise RuntimeError("ES 清理未完整完成，应在下次扫描重试")

    async def delete_knowledge_base_chunks(self, *, kb_id):
        response = await self.client.delete_by_query(
            index=self.index,
            query={"term": {"kb_id": kb_id}},
            refresh=True,
            conflicts="proceed",
        )
        if (
            response.get("failures")
            or response.get("timed_out")
            or response.get("version_conflicts")
        ):
            raise RuntimeError("ES 清理未完整完成，应重试删除知识库")

    async def search(
        self,
        query_vector: list[float],
        *,
        run_ids: list[str] | None = None,
        kb_id: str | None = None,
        document_id: str | None = None,
        document_ids: list[str] | None = None,
        limit: int = 5,
        generation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        # generation_id is an API compatibility alias, not an ES membership field.
        ids = run_ids or ([generation_id] if generation_id else [])
        if not ids:
            return []
        filters = [{"terms": {"run_id": ids}}]
        if kb_id:
            filters.append({"term": {"kb_id": kb_id}})
        if document_id:
            filters.append({"term": {"doc_id": document_id}})
        if document_ids:
            filters.append({"terms": {"doc_id": list(document_ids)}})
        response = await self.client.search(
            index=self.index,
            knn={
                "field": "embedding",
                "query_vector": query_vector,
                "k": limit,
                "num_candidates": max(limit * 5, 20),
                "filter": filters,
            },
            size=limit,
            allow_partial_search_results=False,
        )
        self._check_shards(response)
        return [
            {"chunk_id": hit["_id"], "score": hit["_score"], **hit["_source"]}
            for hit in response["hits"]["hits"]
        ]
