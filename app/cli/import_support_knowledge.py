"""Validate the support-knowledge manifest and open published documents.

Uploads reuse the existing document upload handler; parsing reuses create_run and
the normal worker/outbox path. A document is only opened to customers
(`documents.support_open = 1`) after its active generation is fully published.
Running --apply also closes any document that is open but no longer in the
manifest, which repairs stale flags left by earlier allowlist rows.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from starlette.datastructures import Headers, UploadFile

from app.api.routes import documents_v2
from app.business.errors import BusinessError
from app.business.models import BusinessUser
from app.core.config import settings
from app.db import mysql_repository as repository
from app.db.mysql_models import (
    Document,
    DocumentStatus,
    Generation,
    GenerationStatus,
    KnowledgeBase,
)
from app.db.mysql_session import AsyncSessionFactory
from app.rag.run_creation import create_run

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "samples" / "after-sales-business-v1" / "knowledge-manifest.json"
CORPUS = ROOT / "samples" / "rag-eval-after-sales-v1" / "corpus"


class KnowledgeImportError(Exception):
    pass


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeImportError("无法读取知识资料清单") from exc
    corpus = path.parents[2] / "samples" / "rag-eval-after-sales-v1" / "corpus"
    sources = {document["source"] for document in manifest["documents"]}
    excluded = set(manifest.get("excluded_sources", []))
    overlap = sources & excluded
    if overlap:
        raise KnowledgeImportError(f"开放清单与排除清单冲突：{', '.join(sorted(overlap))}")
    for document in manifest["documents"]:
        file_path = corpus / document["source"]
        if not file_path.exists():
            raise KnowledgeImportError(f"源文件不存在：{document['source']}")
        actual = hashlib.sha256(file_path.read_bytes()).hexdigest()
        if actual != document["sha256"]:
            raise KnowledgeImportError(
                f"源文件哈希已变化：{document['source']}；请审核后重新生成清单"
            )
    return {**manifest, "corpus_path": corpus}


async def _find_kb(session, name: str) -> KnowledgeBase | None:
    return (
        await session.execute(select(KnowledgeBase).where(KnowledgeBase.name == name))
    ).scalars().first()


async def ensure_kb(session, name: str, *, create: bool) -> KnowledgeBase:
    kb = await _find_kb(session, name)
    if kb is not None:
        return kb
    if not create:
        raise KnowledgeImportError(f"知识库不存在：{name}")
    kb = await repository.create_knowledge_base(session, name=name, description="售后开放资料（合成练习）")
    await session.commit()
    await session.refresh(kb)
    return kb


def _published(document: Document, generation: Generation | None) -> bool:
    if generation is None:
        return False
    status = generation.status.value if hasattr(generation.status, "value") else generation.status
    return (
        status == GenerationStatus.SUCCESS.value
        and generation.published_at is not None
        and bool(generation.artifacts_available)
    )


def _in_manifest(document: Document, manifest: dict) -> bool:
    return any(
        document.filename == item["source"] or item["source"] in document.object_key
        for item in manifest["documents"]
    )


async def _report_document(
    session, document: Document, manifest: dict, *, allow_write: bool = False
) -> None:
    run = await session.get(Generation, document.active_run_id) if document.active_run_id else None
    published = _published(document, run)
    state = (
        f"已发布 v{run.version_no}" if published else (f"未发布（{run.status if run else '无批次'}）")
    )
    print(f"  {document.filename}：{state}；{'已开放' if document.support_open else '未开放'}")
    if not published:
        if document.support_open and allow_write:
            document.support_open = False
            await session.commit()
            print("    未发布，已自动关闭开放")
        return
    if not _in_manifest(document, manifest):
        if document.support_open and allow_write:
            document.support_open = False
            await session.commit()
            print("    不在清单中，已自动关闭开放")
        return
    if document.support_open:
        print("    已在开放列表")
        return
    if not allow_write:
        print("    清单内但当前未开放（status 为只读）")
        return
    document.support_open = True
    await session.commit()
    print("    已开放给客户检索")


async def _apply(manifest: dict) -> None:
    async with AsyncSessionFactory() as session:
        kb = await ensure_kb(session, manifest["knowledge_base_name"], create=True)
        admin = (
            await session.execute(select(BusinessUser).where(BusinessUser.role == "admin").limit(1))
        ).scalar_one_or_none()
        if admin is None:
            raise KnowledgeImportError("请先创建管理员，再导入售后资料")

        for entry in manifest["documents"]:
            source = manifest["corpus_path"] / entry["source"]
            content = source.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            existing = await repository.find_document_by_hash(session, kb.id, digest)
            if existing is None:
                same_name = (
                    await session.execute(
                        select(Document).where(
                            Document.knowledge_base_id == kb.id,
                            Document.filename == entry["source"],
                            Document.status == DocumentStatus.ACTIVE,
                        )
                    )
                ).scalars().first()
                if same_name is not None:
                    raise KnowledgeImportError(
                        f"已存在同名但内容不同的文档：{entry['source']}；请先人工核对版本"
                    )
                upload = UploadFile(
                    file=io.BytesIO(content),
                    filename=entry["source"],
                    headers=Headers({"content-type": entry.get("mime_type", "text/markdown")}),
                )
                uploaded = await documents_v2.upload_document(kb.id, upload, session)
                existing = await session.get(Document, uploaded.id)
                print(f"已上传：{entry['source']}")
            else:
                print(f"已存在，跳过上传：{entry['source']}")
            if existing.active_run_id:
                print(f"  已有解析批次：{existing.active_run_id}")
            else:
                try:
                    run = await create_run(existing.id, session)
                    print(f"  已触发建库：{run['generation_id']}")
                except Exception as exc:
                    detail = getattr(exc, "detail", None) or str(exc)
                    print(f"  建库触发失败：{detail}")
        print("以下为当前发布与开放状态：")
        documents = (
            await session.execute(
                select(Document).where(
                    Document.knowledge_base_id == kb.id,
                    Document.status == DocumentStatus.ACTIVE,
                )
            )
        ).scalars()
        for document in documents:
            await _report_document(session, document, manifest, allow_write=True)
        stale = (
            await session.execute(select(Document).where(Document.support_open.is_(True)))
        ).scalars()
        for document in stale:
            if document.knowledge_base_id == kb.id:
                continue
            document.support_open = False
            await session.commit()
            print(f"  {document.filename}：不在目标知识库中，已关闭残留开放标记")
        print("开放标记已按清单重建，目标知识库以外的文档一律关闭。")


async def _status(manifest: dict) -> None:
    async with AsyncSessionFactory() as session:
        kb = await _find_kb(session, manifest["knowledge_base_name"])
        if kb is None:
            print(f"知识库不存在：{manifest['knowledge_base_name']}")
            return
        print(f"知识库：{kb.name}（{kb.id}）")
        documents = (
            await session.execute(
                select(Document).where(
                    Document.knowledge_base_id == kb.id,
                    Document.status == DocumentStatus.ACTIVE,
                )
            )
        ).scalars()
        for document in documents:
            await _report_document(session, document, manifest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="售后开放资料清单校验与导入")
    parser.add_argument("--dry-run", action="store_true", help="只校验清单和源文件哈希")
    parser.add_argument("--apply", action="store_true", help="上传、触发建库并开放已发布文档")
    parser.add_argument("--status", action="store_true", help="只读查看当前发布和开放状态")
    parser.add_argument("--yes", action="store_true", help="确认目标库并允许写入")
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest()
    except KnowledgeImportError as exc:
        print(f"校验失败：{exc}", file=sys.stderr)
        return 1
    print(f"目标库：{settings.mysql_host}:{settings.mysql_port}/{settings.mysql_database}（不含连接凭据）")
    print(
        f"开放清单：{len(manifest['documents'])} 份文档；"
        f"基准 {datetime.now(UTC).date().isoformat()}"
    )
    if args.dry_run or not (args.apply or args.status):
        for document in manifest["documents"]:
            print(f"  {document['source']} → {document['title']}")
        print("dry-run：清单与哈希校验通过，未连接数据库、未上传。")
        return 0
    if args.status:
        try:
            asyncio.run(_status(manifest))
        except Exception as exc:
            print(f"状态读取失败：{exc}", file=sys.stderr)
            return 1
        return 0
    if not args.yes:
        print("未执行。确认目标库后追加 --yes。", file=sys.stderr)
        return 2
    try:
        asyncio.run(_apply(manifest))
    except (KnowledgeImportError, BusinessError) as exc:
        message = exc.message if isinstance(exc, BusinessError) else str(exc)
        print(f"导入停止：{message}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"导入失败：{type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
