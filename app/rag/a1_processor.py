from __future__ import annotations

import asyncio
import json
import logging
from copy import deepcopy
from dataclasses import dataclass

from app.core.config import settings
from app.db import mysql_repository as repository
from app.db.mysql_models import TaskKind, TaskStatus
from app.db.mysql_session import AsyncSessionFactory
from app.rag.document_parser import RemoteMinerUParser, file_type_for, parse_task_document
from app.rag.embedding import OpenAICompatibleEmbeddingClient
from app.rag.fingerprint import build_parse_fingerprint, build_task_chunk_id
from app.rag.mineru_parser import MinerUParser
from app.rag.structured_chunker import StructuredChunker
from app.rag.task_processor import chunk_source_payloads
from app.storage.elasticsearch_chunks import ChunkStore
from app.storage.minio_storage import MinioStorage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessResult:
    task_id: str
    status: TaskStatus
    acknowledged: bool


class TaskProcessor:
    def __init__(
        self,
        *,
        storage=None,
        chunk_store=None,
        embedding_client=None,
        parser=None,
        chunker=None,
        session_factory=None,
    ):
        self.storage = storage or MinioStorage()
        self.chunk_store = chunk_store or ChunkStore()
        self.embedding_client = embedding_client
        self.parser = parser
        self.chunker = chunker
        self.session_factory = session_factory or AsyncSessionFactory

    async def process(self, task_id, *, dispatch_seq, doc_id=None, run_id=None):
        async with self.session_factory() as session:
            notification = await repository.get_task(session, task_id)
            if notification is None:
                return ProcessResult(task_id, TaskStatus.CANCELLED, True)
            if (doc_id and notification.document_id != doc_id) or (
                run_id and notification.generation_id != run_id
            ):
                return ProcessResult(task_id, notification.status, True)
            task = await repository.claim_task(session, task_id, dispatch_seq=dispatch_seq)
            if task is None:
                # Durable scans recover an expired RUNNING task; duplicate notifications
                # do not increase attempts or spin in a tight RabbitMQ requeue loop.
                return ProcessResult(task_id, notification.status, True)
            generation = await repository.get_generation(session, task.generation_id)
            document = await repository.get_document(session, task.document_id)
            attempt_id = task.attempt_id
            snapshot = deepcopy(generation.config_snapshot)
            await session.commit()

        lost = asyncio.Event()

        async def check_execution():
            if lost.is_set():
                raise repository.ExecutionLost("续约失败，停止推进当前执行")
            async with self.session_factory() as session:
                await repository.assert_execution(session, task_id, attempt_id)

        heartbeat = asyncio.create_task(self._heartbeat_loop(task_id, attempt_id, lost))
        try:
            async with asyncio.timeout(settings.task_execution_timeout_seconds):
                if task.kind == TaskKind.COPY:
                    payloads = await self._copy_payloads(
                        task, generation, document, check_execution
                    )
                else:
                    payloads = await self._parse_payloads(
                        task, generation, document, snapshot, check_execution
                    )
                await check_execution()
                # A stable ID cannot clean up remnants if the parser returns a
                # different chunk collection on retry. Detect drift before ES I/O.
                manifest = [
                    {
                        key: value[key]
                        for key in ("chunk_key", "content", "page_start", "page_end", "metadata")
                    }
                    for _id, value in sorted(payloads)
                ]
                async with self.session_factory() as session:
                    await repository.pin_task_output(
                        session, task_id, attempt_id, build_parse_fingerprint({"chunks": manifest})
                    )
                    await session.commit()
                await self.chunk_store.write_chunks(payloads, check_execution=check_execution)
                await check_execution()
                async with self.session_factory() as session:
                    completed = await repository.complete_task(
                        session, task_id=task_id, attempt_id=attempt_id, chunk_count=len(payloads)
                    )
                    await session.commit()
                    return ProcessResult(task_id, completed.status, True)
        except repository.ExecutionLost:
            return await self._current_result(task_id)
        except Exception as exc:
            # COPY and PARSE share the same finite retry policy, including ES/storage errors.
            async with self.session_factory() as session:
                try:
                    task = await repository.fail_task(
                        session,
                        task_id=task_id,
                        attempt_id=attempt_id,
                        code="COPY_FAILED" if task.kind == TaskKind.COPY else "PARSE_FAILED",
                        message=str(exc),
                        retry=True,
                    )
                    await session.commit()
                except repository.ExecutionLost:
                    return await self._current_result(task_id)
                return ProcessResult(task_id, task.status, True)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _parse_payloads(self, task, run, doc, snapshot, check):
        await check()
        content = await self.storage.get_bytes(
            settings.minio_files_bucket, run.file_location_snapshot
        )
        # Pinning a location alone is insufficient if an operator overwrote its contents.
        import hashlib

        if hashlib.sha256(content).hexdigest() != run.file_version:
            raise ValueError("原文件内容与批次固定版本不一致")
        parser_config = snapshot.get("parser", {})
        parser = self.parser
        if parser is None:
            parser_args = {
                "timeout_seconds": parser_config.get(
                    "timeout_seconds", settings.mineru_timeout_seconds
                ),
                "backend": parser_config.get("backend", "pipeline"),
                "method": parser_config.get("method", "auto"),
            }
            if parser_config.get("mode") == "remote":
                if parser_config.get("api_version") != "file_parse-v2":
                    raise ValueError("尚未支持该远端 MinerU API 版本")
                parser = RemoteMinerUParser(base_url=parser_config["base_url"], **parser_args)
            else:
                parser = MinerUParser(command=parser_config.get("command", "mineru"), **parser_args)
        parsed = await parse_task_document(
            filename=doc.filename,
            content=content,
            file_type=snapshot.get("file_type") or file_type_for(doc.filename),
            range_start=task.core_page_start,
            range_end=task.core_page_end,
            parser=parser,
        )
        await check()
        chunk_config = snapshot.get("chunker", {})
        if (
            chunk_config.get("version", StructuredChunker.strategy_version)
            != StructuredChunker.strategy_version
        ):
            raise ValueError("当前 Worker 不支持快照中的切块策略版本")
        chunker = self.chunker or StructuredChunker(
            chunk_size=chunk_config.get("size", 500), overlap=chunk_config.get("overlap", 80)
        )
        chunks = await asyncio.to_thread(chunker.split, parsed.blocks)
        if not chunks:
            raise ValueError("任务没有可检索文本")
        artifact_key = (
            f"knowledge-bases/{doc.knowledge_base_id}/documents/{doc.id}/"
            f"generations/{run.id}/mineru/{task.id}/{task.attempt_id}/blocks.json"
        )
        await self.storage.put_bytes(
            settings.minio_artifacts_bucket,
            artifact_key,
            json.dumps({"blocks": [b.to_dict() for b in parsed.blocks]}, ensure_ascii=False).encode(
                "utf-8"
            ),
            "application/json",
        )
        await check()
        embedding = snapshot.get("embedding", {})
        client = self.embedding_client or OpenAICompatibleEmbeddingClient(
            settings.model_copy(
                update={
                    "embedding_model": embedding["model"],
                    "embedding_dimension": embedding["dimension"],
                    "llm_base_url": embedding.get("base_url", settings.llm_base_url),
                    "embedding_batch_size": embedding.get(
                        "batch_size", settings.embedding_batch_size
                    ),
                }
            )
        )
        # Chunker and embedding I/O are task-local and snapshot-driven. Credentials
        # remain server-side; they are deliberately not persisted in the snapshot.
        vectors = []
        batch_size = max(1, embedding.get("batch_size", settings.embedding_batch_size))
        for start in range(0, len(chunks), batch_size):
            await check()
            vectors.extend(
                await client.embed_texts([c.text for c in chunks[start : start + batch_size]])
            )
        await check()
        return chunk_source_payloads(
            [b.to_dict() for b in parsed.blocks],
            document_id=doc.id,
            task_id=task.id,
            generation_id=run.id,
            kb_id=doc.knowledge_base_id,
            parse_fingerprint=run.parse_fingerprint,
            embedding=vectors,
            chunks=chunks,
        )

    async def _copy_payloads(self, task, run, doc, check):
        await check()
        async with self.session_factory() as session:
            source = await repository.get_task(session, task.source_task_id)
            source_run = (
                await repository.get_generation(session, source.generation_id) if source else None
            )
            if (
                not source
                or not source_run
                or source_run.artifacts_available is False
                or source.status != TaskStatus.SUCCESS
                or source.document_id != doc.id
            ):
                raise ValueError("COPY 源任务不存在或未完整成功")
            if (
                source_run.file_version != run.file_version
                or source_run.config_snapshot != run.config_snapshot
                or source.parse_fingerprint != task.parse_fingerprint
                or (
                    source.core_page_start,
                    source.core_page_end,
                    source.context_page_start,
                    source.context_page_end,
                )
                != (
                    task.core_page_start,
                    task.core_page_end,
                    task.context_page_start,
                    task.context_page_end,
                )
            ):
                raise ValueError("COPY 源任务的文件、配置或范围不兼容")
            expected = source.result_chunk_count
        rows = await self.chunk_store.read_task_chunks(
            kb_id=doc.knowledge_base_id,
            doc_id=doc.id,
            run_id=source.generation_id,
            task_id=source.id,
        )
        if len(rows) != expected or expected <= 0:
            raise ValueError(f"COPY 源产物不完整：期望 {expected}，实际 {len(rows)}")
        keys = set()
        result = []
        dimension = run.config_snapshot["embedding"]["dimension"]
        for _old_id, value in rows:
            await check()
            key = value.get("chunk_key")
            if (
                key is None
                or key in keys
                or len(value.get("embedding", [])) != dimension
                or not value.get("content")
            ):
                raise ValueError("COPY 源产物缺少完整文本/向量或具有重复 chunk_key")
            keys.add(key)
            copied = deepcopy(value)
            copied.update(
                kb_id=doc.knowledge_base_id,
                doc_id=doc.id,
                document_id=doc.id,
                run_id=run.id,
                task_id=task.id,
            )
            result.append((build_task_chunk_id(task.id, key), copied))
        return result

    async def _current_result(self, task_id):
        async with self.session_factory() as session:
            task = await repository.get_task(session, task_id)
            return ProcessResult(task_id, task.status if task else TaskStatus.CANCELLED, True)

    async def _heartbeat_loop(self, task_id, attempt_id, lost):
        try:
            while True:
                await asyncio.sleep(max(0.05, settings.task_heartbeat_interval_seconds))
                async with self.session_factory() as session:
                    task = await repository.touch_task_heartbeat(
                        session, task_id, attempt_id=attempt_id
                    )
                    if task is None:
                        lost.set()
                        return
                    await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            lost.set()
            logger.exception(
                "Task %s heartbeat failed; current attempt is no longer trusted", task_id
            )
