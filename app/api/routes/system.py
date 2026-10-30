from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text

from app.auth.dependencies import require_admin
from app.db.mysql_session import get_mysql_session

router = APIRouter(
    prefix="/api/v1/admin/system",
    tags=["admin-system"],
    dependencies=[Depends(require_admin)],
)


async def _database_ready(session) -> bool:
    try:
        await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@router.get("/status")
async def system_status(request: Request, session=Depends(get_mysql_session)) -> dict:
    status = getattr(request.app.state, "dependency_status", {}) or {}
    rag = status.get("rag") or {"ready": False, "message": "知识检索尚未初始化"}
    return {
        "business": {"ready": await _database_ready(session)},
        "rag": rag,
    }
