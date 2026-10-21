"""Opt-in legacy ES conversion. Default invocation performs NO connections.

Use `python -m tools.migrate_a1_artifacts` for the local plan. --apply requires
an explicitly named, separate target index and a stopped application. No index
or file is deleted and .env is never changed by this script.
"""

import argparse
import asyncio

from sqlalchemy import select


def convert_legacy_chunk(source, *, task, run, doc, chunk_index):
    from app.rag.fingerprint import build_task_chunk_id
    from app.storage.elasticsearch_chunks import build_chunk_source

    dimension = run.config_snapshot["embedding"]["dimension"]
    vector = source.get("embedding", [])
    if (
        source.get("document_id") != doc.id
        or run.id not in source.get("generation_ids", [])
        or not source.get("content")
        or len(vector) != dimension
    ):
        raise ValueError(f"Legacy source incomplete or has wrong ownership: task {task.id}")
    key = str(chunk_index)
    chunk_id = build_task_chunk_id(task.id, key)
    return chunk_id, build_chunk_source(
        chunk_id=chunk_id,
        document_id=doc.id,
        content=source["content"],
        embedding=vector,
        chunk_index=chunk_index,
        page_start=source.get("page_start"),
        page_end=source.get("page_end"),
        source_task_id=task.id,
        generation_id=run.id,
        kb_id=doc.knowledge_base_id,
        chunk_key=key,
        metadata=source.get("metadata", {}),
    )


async def migrate(target_index):
    from app.core.config import settings
    from app.db.mysql_models import Document, Generation, KnowledgeBase, Task, TaskChunkRef
    from app.db.mysql_session import AsyncSessionFactory, engine
    from app.rag.run_creation import embedding_identity
    from app.storage.elasticsearch_chunks import ChunkStore

    if target_index == settings.elasticsearch_index:
        raise ValueError("Target must differ from the configured legacy source index")
    legacy = ChunkStore()
    target = ChunkStore(config=settings.model_copy(update={"elasticsearch_index": target_index}))
    try:
        # Snapshot the stopped application in a read transaction; no DB locks are
        # held while making ES calls. Abort on mismatched active embedding spaces.
        async with AsyncSessionFactory() as session:
            docs = {d.id: d for d in (await session.execute(select(Document))).scalars().all()}
            runs = {r.id: r for r in (await session.execute(select(Generation))).scalars().all()}
            tasks = list(
                (await session.execute(select(Task).where(Task.status == "SUCCESS")))
                .scalars()
                .all()
            )
            refs = list(
                (
                    await session.execute(
                        select(TaskChunkRef).order_by(
                            TaskChunkRef.task_id, TaskChunkRef.chunk_index
                        )
                    )
                )
                .scalars()
                .all()
            )
            kb_models = {}
            for doc in docs.values():
                if not doc.active_run_id:
                    continue
                run = runs[doc.active_run_id]
                identity = embedding_identity(run.config_snapshot)
                if identity[1] != settings.embedding_dimension:
                    raise ValueError("Active run has incompatible target index dimensions")
                if (
                    doc.knowledge_base_id in kb_models
                    and kb_models[doc.knowledge_base_id] != identity
                ):
                    raise ValueError(
                        "Active documents use incompatible embedding spaces; design a full model migration first"
                    )
                kb_models[doc.knowledge_base_id] = identity
        for run in runs.values():
            if run.artifacts_available:
                raise ValueError(
                    "Conversion expects the reviewed SQL migration with legacy artifacts marked unavailable"
                )
        by_task = {}
        for ref in refs:
            by_task.setdefault(ref.task_id, []).append(ref)
        eligible = {
            r.id
            for r in runs.values()
            if r.config_snapshot.get("embedding", {}).get("dimension")
            == settings.embedding_dimension
        }
        # Preflight complete reference lists before creating the target index.
        for task in tasks:
            if task.generation_id in eligible and (
                task.chunk_count <= 0 or len(by_task.get(task.id, [])) != task.chunk_count
            ):
                raise ValueError(f"Legacy references incomplete: task {task.id}")
        await target.ensure_index()
        counts = {}
        for task in tasks:
            if task.generation_id not in eligible:
                continue
            run, doc = runs[task.generation_id], docs[task.document_id]
            payloads = []
            for ref in by_task[task.id]:
                source = await legacy.get_chunk(ref.chunk_id)
                if source is None:
                    raise ValueError(f"Missing legacy chunk: {ref.chunk_id}")
                payloads.append(
                    convert_legacy_chunk(
                        source, task=task, run=run, doc=doc, chunk_index=ref.chunk_index
                    )
                )
            if len({key for key, _ in payloads}) != task.chunk_count:
                raise ValueError("Legacy chunk keys are not unique")
            await target.write_chunks(payloads)
            counts[task.id] = len(payloads)
        await target.refresh()
        # Verify the destination through search before recording availability.
        for run_id in eligible:
            run, expected = (
                runs[run_id],
                {t.id: counts[t.id] for t in tasks if t.generation_id == run_id},
            )
            if not expected:
                continue
            doc = docs[run.document_id]
            rows = await target.read_run_chunks(
                kb_id=doc.knowledge_base_id, doc_id=doc.id, run_id=run_id
            )
            from collections import Counter

            actual = Counter(value["task_id"] for _id, value in rows)
            if dict(actual) != expected:
                raise ValueError(f"Target verification failed: {run_id}")
        async with AsyncSessionFactory() as session:
            for run_id in eligible:
                run = await session.get(Generation, run_id)
                run.artifacts_available = any(t.generation_id == run_id for t in tasks)
            for kb_id, identity in kb_models.items():
                kb = await session.get(KnowledgeBase, kb_id)
                kb.embedding_model, kb.embedding_dimension, kb.embedding_base_url = identity
            await session.commit()
        print(
            f"Converted {sum(counts.values())} task artifacts to {target_index}. Source index retained; configuration unchanged."
        )
    finally:
        await legacy.close()
        await target.close()
        await engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--target-index")
    args = parser.parse_args(argv)
    if not args.apply:
        print(
            "Offline plan only; no connections. Review docs/a1-migration.md and SQL first. "
            "After backup, shutdown and staging verification: --apply --target-index <new-index>."
        )
        return
    if not args.target_index:
        parser.error("--apply requires --target-index")
    asyncio.run(migrate(args.target_index))


if __name__ == "__main__":
    main()
