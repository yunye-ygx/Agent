"""Developer retrieval lab, independent of legacy /search and chat generation."""

import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from app.auth.dependencies import require_admin, require_csrf_for_write
from app.business.principal import Principal
from app.core.config import settings
from app.db.mysql_session import AsyncSessionFactory
from app.retrieval.access import local_owner
from app.retrieval.schemas import ExperimentRequest
from app.retrieval.scope import authorize_snapshot, resolve_clients, resolve_scope
from app.retrieval.search import HybridSearcher
from app.retrieval.service import ExperimentService
from app.retrieval.snapshots import SnapshotStore
from app.storage.elasticsearch_chunks import ChunkStore

router = APIRouter(
    prefix="/api/v1/retrieval-experiments",
    tags=["retrieval-experiments"],
    dependencies=[Depends(require_admin), Depends(require_csrf_for_write)],
)
snapshots = SnapshotStore()
# A local-process budget, not a distributed rate limiter. Reject instead of queueing unbounded work.
_slots = asyncio.Semaphore(2)


@router.get("/session")
async def session_info(
    principal: Principal = Depends(require_admin), owner=Depends(local_owner)
):
    return {
        "mode": "local-admin",
        "user_id": principal.user_id,
        "snapshot_ttl_seconds": 1800,
    }


def _compose_owner(principal: Principal, local_owner_value: str) -> str:
    # Switching accounts must not inherit another user's snapshots. SnapshotStore
    # expects a 64-hex owner, so bind both identities through a SHA-256 digest.
    import hashlib

    material = f"{principal.user_id}:{local_owner_value}".encode()
    return hashlib.sha256(material).hexdigest()


@router.post("")
async def create_experiment(
    payload: ExperimentRequest,
    principal: Principal = Depends(require_admin),
    local_owner_value=Depends(local_owner),
):
    owner = _compose_owner(principal, local_owner_value)
    if _slots.locked():
        raise HTTPException(429, "已有两个检索实验正在执行，请稍后重试")
    async with _slots:
        async with AsyncSessionFactory() as session:
            scope = await resolve_scope(session, payload)
            embedding, rerank, identity = await resolve_clients(session, payload, scope)
        store = ChunkStore()
        try:
            service = ExperimentService(
                snapshots,
                HybridSearcher(store.client, settings.elasticsearch_index),
                embedding.embed_texts,
            )
            report = await service.run(owner, payload, scope, rerank, identity)
        finally:
            await store.client.close()
        # Deletion during a slow provider call must not expose now-inaccessible text.
        async with AsyncSessionFactory() as session:
            await authorize_snapshot(session, scope)
        return report


async def _load(identity, owner):
    try:
        identity = str(UUID(identity))
    except ValueError as exc:
        raise HTTPException(404, "实验不存在或已过期") from exc
    try:
        value = await snapshots.get(owner, identity)
    except Exception as exc:
        raise HTTPException(503, "实验存储暂不可用") from exc
    if value is None:
        raise HTTPException(404, "实验不存在、已过期或不属于当前会话")
    async with AsyncSessionFactory() as session:
        await authorize_snapshot(session, value["report"]["scope"])
    return value


@router.get("/{identity}")
async def get_experiment(
    identity: str,
    principal: Principal = Depends(require_admin),
    local_owner_value=Depends(local_owner),
):
    return (await _load(identity, _compose_owner(principal, local_owner_value)))["report"]


@router.post("/{identity}/fallback")
async def fallback_experiment(
    identity: str,
    principal: Principal = Depends(require_admin),
    local_owner_value=Depends(local_owner),
):
    owner = _compose_owner(principal, local_owner_value)
    value = await _load(identity, owner)
    if not value["report"]["fallback_available"]:
        raise HTTPException(409, "此实验没有可用的 Rerank 失败候选快照")
    try:
        report = await ExperimentService(snapshots).fallback(
            owner, value["report"]["experiment_id"]
        )
    except ValueError as exc:
        raise HTTPException(409, "快照已过期或不可降级") from exc
    except Exception as exc:
        raise HTTPException(503, "降级结果保存失败，请稍后重试") from exc
    async with AsyncSessionFactory() as session:
        await authorize_snapshot(session, value["report"]["scope"])
    return report
